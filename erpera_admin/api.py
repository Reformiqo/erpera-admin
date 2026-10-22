# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

"""The control plane's edge.

Tenant sites talk to exactly two endpoints: one to say what a model call cost,
one to ask which model to use. Both authenticate with a per-site token rather
than a Frappe login, because the caller is a background job on another site,
not a person.
"""

import hmac
import json

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from erpera_admin import credits

MAX_ROWS = 500          # one batch; a site with more simply sends again
MAX_CONTENT = 8000      # per field, matching what the sites store


def _authenticate(site, token):
	"""Return the Managed Site, or refuse. Never echoes the token."""
	if not site or not token:
		raise frappe.AuthenticationError(_("Site and token are required."))

	row = frappe.db.get_value("Managed Site", site, ["name", "enabled"], as_dict=True)
	if not row:
		# Same error and same cost whether the site is unknown or the token is
		# wrong, so this cannot be used to enumerate sites.
		frappe.local.response["http_status_code"] = 401
		raise frappe.AuthenticationError(_("Not authorised."))

	expected = frappe.utils.password.get_decrypted_password(
		"Managed Site", row.name, "token", raise_exception=False) or ""
	if not expected or not hmac.compare_digest(str(expected), str(token)):
		frappe.local.response["http_status_code"] = 401
		raise frappe.AuthenticationError(_("Not authorised."))

	if not row.enabled:
		frappe.local.response["http_status_code"] = 403
		raise frappe.PermissionError(_("This site is disabled."))

	return row.name


def _shorten(v):
	if v is None:
		return None
	s = v if isinstance(v, str) else json.dumps(v, default=str)
	return s[:MAX_CONTENT]


@frappe.whitelist(allow_guest=True, methods=["POST"])
def ingest(site=None, token=None, rows=None):
	"""Take a batch of call rows from a tenant site.

	Replaying a batch is harmless: a row is keyed by the site plus the row's
	name on that site, so a retry after a timeout updates nothing and
	duplicates nothing. That matters because the sender cannot tell a lost
	response from a lost request.
	"""
	site_name = _authenticate(site, token)

	if not cint(frappe.db.get_single_value("ERPERA AI Settings", "ingest_enabled")):
		frappe.local.response["http_status_code"] = 503
		return {"ok": False, "error": "ingest disabled"}

	if isinstance(rows, str):
		rows = json.loads(rows)
	rows = rows or []
	if len(rows) > MAX_ROWS:
		frappe.local.response["http_status_code"] = 413
		return {"ok": False, "error": f"at most {MAX_ROWS} rows per batch", "accepted": 0}

	keep_content = cint(frappe.db.get_single_value("ERPERA AI Settings", "log_content"))

	stored, skipped, spent = 0, 0, 0
	for r in rows:
		remote = (r or {}).get("name")
		if not remote:
			skipped += 1
			continue
		if frappe.db.exists("AI Call Record", {"site": site_name, "remote_name": remote}):
			skipped += 1
			continue

		doc = {
			"doctype": "AI Call Record",
			"site": site_name,
			"remote_name": remote,
			"occurred_on": r.get("occurred_on") or now_datetime(),
			"user": r.get("user"),
			"channel": r.get("channel") or "Desk",
			"kind": r.get("kind") or "chat",
			"provider": r.get("provider"),
			"model": r.get("model"),
			"fallback_used": cint(r.get("fallback_used")),
			"status": r.get("status") or "Success",
			"input_tokens": cint(r.get("input_tokens")),
			"output_tokens": cint(r.get("output_tokens")),
			"total_tokens": cint(r.get("total_tokens")),
			"duration_ms": cint(r.get("duration_ms")),
			"cost": flt(r.get("cost")),
			"currency": r.get("currency"),
			"rated": cint(r.get("rated")),
			"agent": r.get("agent"),
			"tool_calls": _shorten(r.get("tool_calls")),
			"correlation_id": r.get("correlation_id"),
			"error": _shorten(r.get("error")),
		}
		# The site may send content; this end decides whether to keep it.
		if keep_content:
			doc["prompt"] = _shorten(r.get("prompt"))
			doc["response"] = _shorten(r.get("response"))

		rec = frappe.get_doc(doc).insert(ignore_permissions=True)
		stored += 1

		# A failed call is not charged for. The customer got no answer, and a
		# provider outage is not something they should pay to sit through.
		if doc["status"] == "Success":
			cost = credits.cost_of(r)
			credits.consume(site_name, cost, call_record=rec.name)
			spent += cost

	frappe.db.set_value("Managed Site", site_name, {
		"last_seen": now_datetime(),
		"calls_received": cint(frappe.db.get_value("Managed Site", site_name, "calls_received")) + stored,
	}, update_modified=False)
	frappe.db.commit()

	return {"ok": True, "accepted": stored, "skipped": skipped,
	        "credits_spent": spent, "credits": credits.balance(site_name)["credits"]}


@frappe.whitelist(allow_guest=True)
def get_config(site=None, token=None):
	"""Which provider and model this site should answer with."""
	site_name = _authenticate(site, token)

	from erpera_admin.erpera_admin.doctype.erpera_ai_settings.erpera_ai_settings import resolved_config

	from erpera_admin import relay as relay_mod

	cfg = resolved_config()
	# A relaying site must not be handed a provider or a model to store. It is
	# not calling one, and anything it stores its operator can read.
	cfg["relay"] = 1 if relay_mod.enabled() else 0
	if cfg["relay"]:
		cfg["primary"] = None
		cfg["fallback"] = None
	if not cfg["relay"] and cint(frappe.db.get_single_value("ERPERA AI Settings", "share_keys")):
		# Opt-in, and only for sites we operate ourselves.
		s = frappe.get_cached_doc("ERPERA AI Settings")
		for slot, field in (("primary", "active_provider"), ("fallback", "fallback_provider")):
			if cfg.get(slot) and s.get(field):
				cfg[slot]["api_key"] = frappe.utils.password.get_decrypted_password(
					"AI Provider Credential", s.get(field), "api_key", raise_exception=False) or ""

	frappe.db.set_value("Managed Site", site_name, "last_seen", now_datetime(), update_modified=False)
	frappe.db.commit()
	return cfg


# --------------------------------------------------------------------- licence

GRACE_HOURS = 72  # how long a site may keep working if it cannot reach us


@frappe.whitelist(allow_guest=True)
def verify(site=None, token=None):
	"""Is this site allowed to make a model call right now?

	Returns a structured answer rather than raising, because the caller has to
	show the operator a reason they can act on. A wrong key and an unknown site
	give the same answer, so this cannot be used to enumerate customers.
	"""
	denied = {"ok": False, "reason": "invalid_key",
	          "message": _("This site's AI Assist API key is not valid.")}

	if not site or not token:
		return denied

	row = frappe.db.get_value(
		"Managed Site", site,
		["name", "enabled", "plan", "seats", "valid_until", "last_verified"], as_dict=True)
	if not row:
		return denied

	expected = frappe.utils.password.get_decrypted_password(
		"Managed Site", row.name, "token", raise_exception=False) or ""
	if not expected or not hmac.compare_digest(str(expected), str(token)):
		return denied

	if not row.enabled:
		# A site that has never had a yes from us was never switched on in the
		# first place, which is a different sentence from one we have turned
		# off. last_verified is only ever written on success, so its absence is
		# the cleanest evidence of which of the two this is.
		if not row.last_verified:
			return {"ok": False, "reason": "pending_approval",
			        "message": _("This site is registered and waiting for approval.")}
		return {"ok": False, "reason": "suspended",
		        "message": _("This site's AI Assist subscription is suspended.")}

	if row.valid_until and frappe.utils.getdate(row.valid_until) < frappe.utils.getdate():
		return {"ok": False, "reason": "expired",
		        "message": _("This site's AI Assist subscription expired on {0}.").format(
			        frappe.utils.formatdate(row.valid_until))}

	# An empty wallet refuses in exactly the same shape as an unpaid
	# subscription, so the site needs no new branch to handle it and the
	# customer reads one sentence telling them what to do about it.
	wallet = credits.check(row.name)
	if not wallet.get("ok"):
		return {"ok": False, "reason": wallet["reason"], "message": wallet["message"],
		        "credits": 0}

	frappe.db.set_value("Managed Site", row.name, "last_verified", now_datetime(),
	                    update_modified=False)
	frappe.db.commit()

	from erpera_admin import relay as relay_mod

	return {
		"ok": True,
		"plan": row.plan,
		"seats": cint(row.seats),
		# When this is on the site sends us the conversation instead of calling
		# a provider itself, and never learns which model answered.
		"relay": 1 if relay_mod.enabled() else 0,
		"valid_until": str(row.valid_until) if row.valid_until else None,
		"grace_hours": GRACE_HOURS,
		"credits": wallet.get("credits"),
		"credits_valid_upto": wallet.get("valid_upto"),
		"credits_low": wallet.get("low", False),
	}


# ------------------------------------------------------------------ credits

@frappe.whitelist(allow_guest=True)
def credit_account(site=None, token=None):
	"""What the tenant's purchase page needs. Authenticated by site token."""
	from erpera_admin import billing

	site_name = _authenticate(site, token)
	return billing.account(site_name)


@frappe.whitelist(allow_guest=True)
def credit_quote(site=None, token=None, credits=None):
	"""Price a basket without opening an order."""
	from erpera_admin import billing

	_authenticate(site, token)
	return billing.quote(credits)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def credit_order(site=None, token=None, credits=None):
	"""Open a Razorpay order for this site."""
	from erpera_admin import billing

	site_name = _authenticate(site, token)
	return billing.place_order(site_name, credits)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def credit_confirm(site=None, token=None, razorpay_order_id=None,
                   razorpay_payment_id=None, razorpay_signature=None):
	"""Settle a finished payment into credits."""
	from erpera_admin import billing

	site_name = _authenticate(site, token)
	return billing.settle(razorpay_order_id, razorpay_payment_id,
	                      razorpay_signature, site=site_name)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def credit_simulate(site=None, token=None, order_id=None, fail=0):
	"""Stand in for the browser's checkout while there is no working key.

	Guarded inside the gateway: a live key or a server not in developer mode
	refuses outright, so this cannot mint credits anywhere real.
	"""
	from erpera_admin import gateway

	_authenticate(site, token)
	return gateway.simulate_checkout(order_id, fail=cint(fail))


@frappe.whitelist(allow_guest=True, methods=["POST"])
def enrol(site=None, base_url=None, email=None, company_name=None, enrol_key=None):
	"""Register a site and issue its key.

	Guest-reachable by necessity: a site with no key cannot authenticate, which
	is the whole point of calling this. What guards it is enrolment_mode, a
	per-address rate limit, and the fact that a new site starts with no credits
	and so cannot spend anything.
	"""
	from erpera_admin import enrolment

	ip = frappe.local.request_ip if hasattr(frappe.local, "request_ip") else None
	return enrolment.enrol(site=site, base_url=base_url, email=email,
	                       company_name=company_name, enrol_key=enrol_key, ip=ip)


# -------------------------------------------------------------------- relay

@frappe.whitelist(allow_guest=True, methods=["POST"])
def chat(site=None, token=None, messages=None, system=None, tools=None,
         max_tokens=4096, context=None):
	"""Answer on a site's behalf.

	The conversation comes up, the answer goes down, and which model produced
	it stays here. Authenticated by site token like everything else on this
	edge, because the caller is that site's server, not a person.
	"""
	from erpera_admin import relay as relay_mod

	site_name = _authenticate(site, token)
	if not relay_mod.enabled():
		frappe.local.response["http_status_code"] = 503
		return {"ok": False, "error": "relay disabled"}

	if isinstance(messages, str):
		messages = json.loads(messages)
	if isinstance(tools, str):
		tools = json.loads(tools)
	if isinstance(context, str):
		context = json.loads(context)

	return relay_mod.chat(site_name, messages=messages, system=system, tools=tools,
	                      max_tokens=max_tokens, context=context)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def see(site=None, token=None, image=None, prompt=None, max_tokens=2048, context=None):
	"""Read one image on a site's behalf. Named for what it does, not for a vendor."""
	from erpera_admin import relay as relay_mod

	site_name = _authenticate(site, token)
	if not relay_mod.enabled():
		frappe.local.response["http_status_code"] = 503
		return {"ok": False, "error": "relay disabled"}

	if isinstance(context, str):
		context = json.loads(context)

	return relay_mod.vision(site_name, image_data_url=image, prompt=prompt,
	                        max_tokens=max_tokens, context=context)
