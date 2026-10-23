# Copyright (c) 2026, The Tiny App Company and contributors
# For license information, please see license.txt

"""Reaching into a customer's ERPNext site from here.

The product used to run inside the customer's site, so a tool call was an
in-process `frappe.get_list` and permissions were whatever the logged-in user
had. Now the reasoning lives here and the data stays there, so every tool call
becomes an HTTPS request back into their site, signed with a key they gave us.

Three things follow from that, and all three are handled in this module rather
than left to the caller.

Whose key. A Frappe API key identifies one user and there is no impersonation
header, so one key for the whole site would mean every person chatting sees
whatever that key's owner can see. Instead each site holds a small set of
credentials, one per user, and `for_user` picks the asking user's own. The row
with no user is the site default: it is what the setup page stores, what proves
the connection works, and what answers for channels with no Desk session.

Where we are allowed to connect. The base URL is typed in by a customer, and we
make the request, so an unchecked URL turns this server into a proxy for
anything it can reach: a cloud metadata endpoint, or another tenant's site on
the same box. `_assert_reachable` resolves the host and refuses private,
loopback, link-local and reserved addresses, and we never follow redirects, so
a public URL cannot bounce us inward. A hostname that re-resolves between our
check and the connection would still get through; that is a known limit, and
the reason the allowlist for private targets lives in site config where a
customer cannot reach it.

What an error looks like. Their site answers in Frappe's dialect, with the
readable part buried in `_server_messages`. `_readable` digs it out so the agent
can act on a sentence instead of a traceback, and so nothing we log carries
their key.
"""

import ipaddress
import json
import socket
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import cint, flt

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 45          # a report on a big site is slow but not unbounded
REPORT_TIMEOUT = 120
MAX_PAGE = 100


class ErpError(frappe.ValidationError):
	"""Their site answered, and said no."""


class ErpUnreachable(ErpError):
	"""We could not get a reply at all."""


class ErpForbidden(ErpError):
	"""The key works; this user may not read that.

	Kept apart from a rejected key because the two need opposite replies: one
	is an administrator's job to fix, the other is simply the answer, and
	telling someone to reconnect their key when they merely lack a permission
	sends them to the wrong place entirely.
	"""


class ErpNotConnected(ErpError):
	"""This site has not given us a key yet."""


# ─── Where we may connect ─────────────────────────────────────────────────────

def _allow_private():
	"""Dev and self-hosted benches, switched on in site config only."""
	return bool(frappe.conf.get("erpera_allow_private_erp"))


def _assert_reachable(url):
	"""Refuse a URL that points back inside our own network."""
	p = urlparse(url)
	if p.scheme not in ("http", "https"):
		raise ErpError(_("The site address must start with https://"))
	if p.scheme == "http" and not _allow_private():
		# The key travels in a header on every call; plaintext is not an option.
		raise ErpError(_("The site address must use https:// so the key is not sent in the clear."))
	if not p.hostname:
		raise ErpError(_("That site address has no host name."))
	if _allow_private():
		return
	try:
		infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80),
		                           proto=socket.IPPROTO_TCP)
	except socket.gaierror:
		raise ErpUnreachable(_("That site address does not resolve. Check the spelling."))
	for info in infos:
		ip = ipaddress.ip_address(info[4][0])
		if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
				or ip.is_multicast or ip.is_unspecified):
			raise ErpError(_("That site address points to a private network, which we cannot reach."))


# ─── Reading their answer ─────────────────────────────────────────────────────

def _readable(response):
	"""The one sentence their site meant for a person."""
	try:
		data = response.json()
	except ValueError:
		return (response.text or "")[:300].strip() or f"HTTP {response.status_code}"
	for raw in json.loads(data.get("_server_messages") or "[]"):
		try:
			msg = json.loads(raw)
			text = msg.get("message") if isinstance(msg, dict) else raw
		except Exception:
			text = raw
		if text:
			return frappe.utils.strip_html(str(text))[:400]
	exc_type = data.get("exc_type") or ""
	if exc_type in ("PermissionError", "PermissionError "):
		return "Not permitted on that site."
	if exc_type:
		return f"{exc_type}: {str(data.get('exception') or '')[:200]}"
	return f"HTTP {response.status_code}"


class Site:
	"""One customer's site, as a set of calls we are allowed to make."""

	def __init__(self, managed_site, user=None):
		row = frappe.db.get_value("Managed Site", managed_site,
		                          ["name", "base_url", "enabled"], as_dict=True)
		if not row:
			raise ErpNotConnected(_("Unknown site."))
		if not row.enabled:
			raise ErpError(_("This site is disabled."))
		self.site = row.name
		self.base_url = (row.base_url or "").strip().rstrip("/")
		if not self.base_url:
			raise ErpNotConnected(_("This site has not told us its address yet."))
		self.user = user
		self.key, self.secret, self.acting_as = _credential(row.name, user)
		self._meta_cache = {}
		_assert_reachable(self.base_url)

	# ── the wire ──────────────────────────────────────────────────────────

	def _request(self, verb, path, params=None, body=None, timeout=None):
		import requests

		try:
			r = requests.request(
				verb,
				f"{self.base_url}{path}",
				params=params,
				json=body,
				headers={
					"Authorization": f"token {self.key}:{self.secret}",
					"Accept": "application/json",
					"User-Agent": "ERPERA-AI/1.0",
				},
				timeout=(CONNECT_TIMEOUT, timeout or READ_TIMEOUT),
				# A public address that redirects inward is the whole point of
				# the check above, so we never follow one.
				allow_redirects=False,
			)
		except Exception:
			# Never the exception text: it carries the URL and sometimes the header.
			raise ErpUnreachable(_("Your ERPNext site did not answer. Check that it is online."))

		if r.status_code in (301, 302, 303, 307, 308):
			raise ErpError(_("Your site address redirects elsewhere. Use the address it redirects to."))
		if r.status_code == 401:
			raise ErpError(_("Your ERPNext site rejected our key. Reconnect it in AI Assist setup."))
		if r.status_code == 403:
			raise ErpForbidden(_("You don't have permission for that."))
		if not r.ok:
			raise ErpError(_readable(r))
		try:
			return r.json().get("message")
		except ValueError:
			raise ErpError(_("Your ERPNext site sent something we could not read."))

	def _get(self, method, params=None, timeout=None):
		return self._request("GET", f"/api/method/{method}", params=_encode(params), timeout=timeout)

	def _post(self, method, body=None, timeout=None):
		return self._request("POST", f"/api/method/{method}", body=body, timeout=timeout)

	# ── reads ─────────────────────────────────────────────────────────────

	def get_list(self, doctype, fields=None, filters=None, order_by=None, limit=20,
	             group_by=None, parent=None):
		# limit=0 means every row, which is how Frappe reads it too. It is for
		# our own small reads, like a doctype's fields; everything the model can
		# reach is clamped by the executor before it gets here.
		limit = cint(limit)
		rows = self._get("frappe.client.get_list", {
			"doctype": doctype,
			"fields": fields or ["name"],
			"filters": filters,
			"order_by": order_by,
			"group_by": group_by,
			"parent": parent,
			"limit_page_length": 0 if limit == 0 else min(limit, MAX_PAGE),
		})
		# frappe._dict, not plain dict: the query registry reads rows as r.item_code
		# throughout, the way it did when it ran inside their site, and nothing
		# about moving the call across the network should change that.
		return [frappe._dict(r) if isinstance(r, dict) else r for r in (rows or [])]

	def get_count(self, doctype, filters=None):
		return cint(self._get("frappe.client.get_count", {"doctype": doctype, "filters": filters}))

	def get_doc(self, doctype, name):
		return self._request("GET", f"/api/resource/{_seg(doctype)}/{_seg(name)}") or {}

	def get_value(self, doctype, filters, fieldname):
		"""One field of one record, as a value rather than a row.

		frappe.client.get_value answers with {fieldname: value} even for a
		single field, and a dict that should have been a string is the kind of
		bug that surfaces three layers away as a prompt saying the currency is
		{'default_currency': 'INR'}.
		"""
		out = self._get("frappe.client.get_value", {
			"doctype": doctype, "filters": filters, "fieldname": fieldname})
		if isinstance(out, dict) and isinstance(fieldname, str):
			return out.get(fieldname)
		return out

	def single_value(self, doctype, field):
		return self._get("frappe.client.get_single_value", {"doctype": doctype, "field": field})

	def search_link(self, doctype, txt, page_length=10):
		return self._get("frappe.desk.search.search_link", {
			"doctype": doctype, "txt": txt or "", "page_length": page_length}) or []

	def run_report(self, report_name, filters=None):
		return self._get("frappe.desk.query_report.run", {
			"report_name": report_name,
			"filters": filters or {},
			"ignore_prepared_report": 1,
		}, timeout=REPORT_TIMEOUT) or {}

	def aggregate(self, doctype, filters=None, sum_fields=None, group_by=None, limit=20):
		"""One call for a total, a count, or a breakdown.

		v16 rejects "sum(x)" as a string in SELECT, so the functions go up in
		dict form. Everything an answer needs comes back in a single request,
		which keeps a cross-network tool call to one round trip. Grouped rows
		come back unordered and get sorted here, because order_by rejects a
		function the way SELECT does.
		"""
		sums = [f for f in (sum_fields or []) if f]
		fields = ([group_by] if group_by else []) + [{"COUNT": "*", "as": "_count"}]
		fields += [{"SUM": f, "as": f} for f in sums]
		rows = self.get_list(doctype, fields=fields, filters=filters, group_by=group_by,
		                     limit=(limit if group_by else 1))
		if group_by and rows:
			key = sums[0] if sums else "_count"
			rows.sort(key=lambda r: abs(float(r.get(key) or 0)), reverse=True)
		return rows or []

	def group(self, doctype, filters, group_field, agg_field=None, limit=20):
		"""Grouped rows as {label, _count, total}, biggest first.

		The shape the query registry has always consumed, so the registry does
		not have to know the grouping now happens on their database rather than
		ours.
		"""
		rows = self.aggregate(doctype, filters=filters,
		                      sum_fields=[agg_field] if agg_field else [],
		                      group_by=group_field, limit=limit)
		return [frappe._dict({
			"label": r.get(group_field),
			"_count": cint(r.get("_count")),
			"total": flt(r.get(agg_field)) if agg_field else 0.0,
		}) for r in rows]

	def daily(self, doctype, filters, date_field, amount_field, limit=400):
		"""One row per day, in date order."""
		return self.get_list(
			doctype,
			fields=[date_field, {"SUM": amount_field, "as": "total"}],
			filters=filters, group_by=date_field, order_by=f"{date_field} asc",
			limit=limit)

	def fields_of(self, doctype):
		"""Field schema, standard plus customised, small enough to send to a model.

		The meta bundle their site would serve is over a megabyte for an
		invoice. The two child-table reads below are under two kilobytes.
		"""
		if doctype in self._meta_cache:
			return self._meta_cache[doctype]
		std = self.get_list(
			"DocField", parent="DocType",
			filters=[["parent", "=", doctype]],
			fields=["fieldname", "fieldtype", "label", "reqd", "options"],
			limit=0) or []
		custom = self.get_list(
			"Custom Field",
			filters=[["dt", "=", doctype]],
			fields=["fieldname", "fieldtype", "label", "reqd", "options"],
			limit=0) or []
		out = {
			"fields": std + custom,
			"is_submittable": cint(self.get_value("DocType", {"name": doctype}, "is_submittable")),
		}
		self._meta_cache[doctype] = out
		return out

	# ── writes ────────────────────────────────────────────────────────────

	def insert(self, doc):
		return self._request("POST", f"/api/resource/{_seg(doc['doctype'])}", body=doc) or {}

	def update(self, doctype, name, data):
		return self._request("PUT", f"/api/resource/{_seg(doctype)}/{_seg(name)}", body=data) or {}

	# ── proof of life ─────────────────────────────────────────────────────

	def whoami(self):
		"""Who their site thinks we are. The connection test, and nothing more."""
		return self._get("frappe.auth.get_logged_user")


def _seg(value):
	from urllib.parse import quote

	return quote(str(value), safe="")


def _encode(params):
	"""Frappe wants JSON in the query string for anything that is not a scalar."""
	if not params:
		return None
	out = {}
	for k, v in params.items():
		if v is None:
			continue
		out[k] = v if isinstance(v, (str, int, float)) else json.dumps(v)
	return out


# ─── Credentials ──────────────────────────────────────────────────────────────

def _credential(site, user=None):
	"""The key to use for this person, falling back to the site's own.

	Returns (api_key, api_secret, acting_as). `acting_as` is the user whose key
	we ended up using, which the call record keeps so a surprising answer can
	be traced to the permissions that produced it.
	"""
	rows = frappe.get_all(
		"TTAC Site Credential",
		filters={"parent": site, "parenttype": "Managed Site"},
		fields=["name", "user", "api_key", "disabled"],
		order_by="idx asc",
	)
	chosen = None
	if user:
		chosen = next((r for r in rows if (r.user or "") == user and not r.disabled), None)
	if not chosen:
		chosen = next((r for r in rows if not (r.user or "") and not r.disabled), None)
	if not chosen or not chosen.api_key:
		raise ErpNotConnected(_("This site has not connected its ERPNext credentials yet."))
	secret = frappe.utils.password.get_decrypted_password(
		"TTAC Site Credential", chosen.name, "api_secret", raise_exception=False)
	if not secret:
		raise ErpNotConnected(_("This site's stored key is incomplete. Reconnect it in AI Assist setup."))
	return chosen.api_key, secret, (chosen.user or None)


def has_user_key(site, user):
	return bool(frappe.db.exists("TTAC Site Credential",
	                             {"parent": site, "parenttype": "Managed Site",
	                              "user": user, "disabled": 0}))


def store_credential(site, api_key, api_secret, user=None, label=None):
	"""Add or replace one credential on a Managed Site."""
	doc = frappe.get_doc("Managed Site", site)
	target = (user or "").strip()
	row = next((r for r in doc.erp_credentials if (r.user or "") == target), None)
	if not row:
		row = doc.append("erp_credentials", {})
	row.user = target or None
	row.api_key = api_key
	row.api_secret = api_secret
	row.label = label or ("Site default" if not target else target)
	row.disabled = 0
	doc.save(ignore_permissions=True)
	return row.name


def forget_credentials(site):
	doc = frappe.get_doc("Managed Site", site)
	doc.erp_credentials = []
	doc.erp_status = "Not connected"
	doc.save(ignore_permissions=True)


def for_user(site, user=None):
	"""The entry point every tool call goes through."""
	return Site(site, user=user)
