# Copyright (c) 2026, The Tiny App Company and contributors
# For license information, please see license.txt

"""Sites enrolling themselves.

The old way round was that someone installed the app, then went to a portal,
signed up, generated a key and pasted 43 characters into a settings field.
Every one of those steps is a place to lose a customer, and the last one is a
place to lose a key.

Here the site asks us for its own key and stores it without a human reading
it. What stops that being an open door is `enrolment_mode`, and more to the
point, credits: a freshly enrolled site has a zero balance and cannot make a
single model call until it buys some. Giving away an account costs nothing
when the account cannot spend anything.

A key is returned exactly once, at enrolment. Asking again for a site that
already has one is refused rather than rotated, because a second caller
claiming to be an existing site is either a mistake or an attack, and quietly
handing over its key would be the worst answer to both.
"""

import secrets

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, now_datetime, today


class EnrolmentRefused(frappe.ValidationError):
	"""This site may not enrol."""


def _settings():
	return frappe.get_cached_doc("TTAC AI Settings")


def _mode():
	return (_settings().get("enrolment_mode") or "Off").strip()


def _check_rate(ip):
	"""A cheap brake, counted in the cache rather than a table."""
	limit = cint(_settings().get("enrol_rate_limit")) or 20
	key = f"erpera:enrol:{ip or 'unknown'}"
	cache = frappe.cache()
	count = cint(cache.get_value(key))
	if count >= limit:
		frappe.throw(_("Too many enrolment attempts. Try again later."), EnrolmentRefused)
	cache.set_value(key, count + 1, expires_in_sec=3600)


def _normalise(site):
	site = (site or "").strip().lower()
	if not site:
		frappe.throw(_("Which site is enrolling?"), EnrolmentRefused)
	# A site name is a hostname, not a URL. Accept either and store one.
	for prefix in ("https://", "http://"):
		if site.startswith(prefix):
			site = site[len(prefix):]
	return site.rstrip("/").split("/")[0]


def enrol(site=None, base_url=None, email=None, company_name=None, enrol_key=None,
          ip=None):
	"""Register a site and hand back its key, once."""
	mode = _mode()
	if mode == "Off":
		frappe.throw(_("This licence server is not accepting new sites."), EnrolmentRefused)

	_check_rate(ip)
	site = _normalise(site)

	if mode == "Key":
		expected = _settings().get_password("enrol_key", raise_exception=False) or ""
		if not expected:
			frappe.throw(_("This licence server has no enrolment code set."), EnrolmentRefused)
		if not secrets.compare_digest(str(expected), str(enrol_key or "")):
			frappe.throw(_("That enrolment code is not right."), EnrolmentRefused)

	existing = frappe.db.get_value("Managed Site", site, ["name", "enabled"], as_dict=True)
	if existing:
		# Never re-issue. A site that has lost its key gets a rotation from the
		# portal by someone who can prove they own the account.
		frappe.throw(
			_("{0} is already registered. Ask your supplier to re-issue its key.").format(site),
			EnrolmentRefused)

	s = _settings()
	trial_days = cint(s.get("trial_days")) or 15
	token = secrets.token_urlsafe(32)

	doc = frappe.get_doc({
		"doctype": "Managed Site",
		"site_name": site,
		"base_url": (base_url or "").strip() or None,
		"company_name": (company_name or "").strip() or None,
		"token": token,
		"plan": "Trial",
		"seats": cint(s.get("default_seats")) or 1,
		"valid_until": add_days(today(), trial_days),
		"signed_up_on": now_datetime(),
		# Approval mode lets a site register but not work until someone looks.
		"enabled": 0 if mode == "Approval" else 1,
		"on_credits": 1,
		"notes": _("Self enrolled on {0}. Contact: {1}").format(today(), email or "-"),
	}).insert(ignore_permissions=True)

	trial = flt(s.get("trial_credits"))
	if trial > 0 and doc.enabled:
		from erpera_admin import credits

		credits.grant(doc.name, trial, remarks=_("Trial credits on enrolment."))

	frappe.db.commit()

	return {
		"ok": True,
		"site": doc.name,
		"token": token,
		"enabled": bool(doc.enabled),
		"plan": doc.plan,
		"valid_until": str(doc.valid_until),
		"trial_credits": trial if doc.enabled else 0,
		"pending_approval": mode == "Approval",
		"message": (
			_("Registered. Your supplier still has to approve this site before it will answer.")
			if mode == "Approval" else
			_("Registered. This site is connected.")
		),
	}
