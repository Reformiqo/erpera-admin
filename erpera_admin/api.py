# Copyright (c) 2026, The Tiny App Company and contributors
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

	if not cint(frappe.db.get_single_value("TTAC AI Settings", "ingest_enabled")):
		frappe.local.response["http_status_code"] = 503
		return {"ok": False, "error": "ingest disabled"}

	if isinstance(rows, str):
		rows = json.loads(rows)
	rows = rows or []
	if len(rows) > MAX_ROWS:
		frappe.local.response["http_status_code"] = 413
		return {"ok": False, "error": f"at most {MAX_ROWS} rows per batch", "accepted": 0}

	keep_content = cint(frappe.db.get_single_value("TTAC AI Settings", "log_content"))

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

	from erpera_admin.ttac_admin.doctype.ttac_ai_settings.ttac_ai_settings import resolved_config

	from erpera_admin import relay as relay_mod

	cfg = resolved_config()
	# A relaying site must not be handed a provider or a model to store. It is
	# not calling one, and anything it stores its operator can read.
	cfg["relay"] = 1 if relay_mod.enabled() else 0
	if cfg["relay"]:
		cfg["primary"] = None
		cfg["fallback"] = None
	if not cfg["relay"] and cint(frappe.db.get_single_value("TTAC AI Settings", "share_keys")):
		# Opt-in, and only for sites we operate ourselves.
		s = frappe.get_cached_doc("TTAC AI Settings")
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
def enrol(site=None, base_url=None, email=None, company_name=None):
	"""Register a site and issue its key.

	Guest-reachable by necessity: a site with no key cannot authenticate, which
	is the whole point of calling this. What guards it is enrolment_mode, a
	per-address rate limit, and the fact that a new site starts with no credits
	and so cannot spend anything.
	"""
	from erpera_admin import enrolment

	ip = frappe.local.request_ip if hasattr(frappe.local, "request_ip") else None
	return enrolment.enrol(site=site, base_url=base_url, email=email,
	                       company_name=company_name, ip=ip)


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


# ------------------------------------------------------- the brain's own edge

def _parsed(value):
	"""A tenant posts JSON strings; Frappe hands them over untouched."""
	if isinstance(value, str):
		try:
			return json.loads(value)
		except ValueError:
			return None
	return value


@frappe.whitelist(allow_guest=True, methods=["POST"])
def connect_erp(site=None, token=None, base_url=None, api_key=None, api_secret=None):
	"""Take a site's ERPNext credentials and prove they work.

	This is the moment the product inverts. Until now their site called us; from
	here we call theirs, which is what lets the reasoning live on this side. The
	key is checked before it is trusted, because a key that silently does not
	work turns every later question into a mystery.
	"""
	from erpera_admin import erp

	site_name = _authenticate(site, token)
	if not (base_url and api_key and api_secret):
		frappe.local.response["http_status_code"] = 400
		return {"ok": False, "error": _("Site address, API key and API secret are all required.")}

	doc = frappe.get_doc("Managed Site", site_name)
	doc.base_url = (base_url or "").strip().rstrip("/")
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	erp.store_credential(site_name, api_key, api_secret, user=None, label="Site default")
	frappe.db.commit()

	try:
		who = erp.Site(site_name).whoami()
	except Exception as e:
		# The address they typed stays on the record even when it failed, so the
		# setup page can show them what did not work instead of silently reverting.
		frappe.db.set_value("Managed Site", site_name, {
			"erp_status": "Unreachable" if isinstance(e, erp.ErpUnreachable) else "Rejected",
			"erp_last_probe": now_datetime(),
			"erp_probe_error": str(e)[:500],
		}, update_modified=False)
		frappe.db.commit()
		frappe.local.response["http_status_code"] = 400
		return {"ok": False, "error": str(e)[:400]}

	frappe.db.set_value("Managed Site", site_name, {
		"erp_status": "Connected",
		"erp_last_probe": now_datetime(),
		"erp_probe_error": None,
	}, update_modified=False)
	frappe.db.commit()
	return {"ok": True, "acting_as": who}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def register_key(site=None, token=None, user=None, api_key=None, api_secret=None):
	"""Store one person's key, so their questions carry their permissions.

	The site token already proves the caller is that site's server, and that
	server's Administrator can read any of its users' secrets anyway, so this
	adds no exposure the customer did not already have. What it buys is that a
	question asked by a storekeeper is answered with a storekeeper's sight.
	"""
	from erpera_admin import erp

	site_name = _authenticate(site, token)
	user = (user or "").strip()
	if not (user and api_key and api_secret):
		frappe.local.response["http_status_code"] = 400
		return {"ok": False, "error": _("User, API key and API secret are all required.")}

	erp.store_credential(site_name, api_key, api_secret, user=user, label=user)
	frappe.db.commit()
	try:
		who = erp.Site(site_name, user=user).whoami()
	except Exception as e:
		frappe.db.commit()
		frappe.local.response["http_status_code"] = 400
		return {"ok": False, "error": str(e)[:400]}
	if who and who != user:
		# A mismatch means the pair belongs to someone else: storing it would
		# quietly answer one person's questions with another's permissions.
		frappe.db.set_value("TTAC Site Credential",
		                    {"parent": site_name, "user": user}, "disabled", 1,
		                    update_modified=False)
		frappe.db.commit()
		frappe.local.response["http_status_code"] = 400
		return {"ok": False, "error": _("That key belongs to a different user.")}
	return {"ok": True, "acting_as": who}


@frappe.whitelist(allow_guest=True)
def erp_state(site=None, token=None):
	"""What the setup page needs to draw itself, and nothing else."""
	from erpera_admin import erp

	site_name = _authenticate(site, token)
	row = frappe.db.get_value("Managed Site", site_name,
	                          ["base_url", "erp_status", "erp_last_probe", "erp_probe_error"],
	                          as_dict=True) or {}
	return {
		"ok": True,
		"base_url": row.get("base_url"),
		"status": row.get("erp_status") or "Not connected",
		"last_checked": row.get("erp_last_probe"),
		"error": row.get("erp_probe_error"),
		"users_registered": frappe.db.count("TTAC Site Credential", {
			"parent": site_name, "parenttype": "Managed Site", "disabled": 0}) ,
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def agents(site=None, token=None):
	"""The agents this site may choose from.

	Served rather than shipped, so retiring one or retuning how it thinks takes
	a deploy here and nothing at all on a customer's site.
	"""
	from erpera_admin.brain import agents as brain_agents

	_authenticate(site, token)
	return {"ok": True, "agents": brain_agents.catalogue()}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def ask(site=None, token=None, message=None, history=None, mode="desk",
        context=None, allow_instant=0, **_ignored):
	"""One whole conversational turn, reasoned here and read from there.

	The tenant sends a question and who is asking. Everything that decides the
	answer — the prompt, the tool list, the model, the loop — is on this side,
	and the data never leaves theirs: we read it with their user's key, use it
	to answer, and keep nothing but the usage record.
	"""
	from erpera_admin import erp, relay as relay_mod
	from erpera_admin.brain import agent as brain_agent, agents as brain_agents, instant

	site_name = _authenticate(site, token)
	if not relay_mod.enabled():
		frappe.local.response["http_status_code"] = 503
		return {"ok": False, "error": "relay disabled"}
	if not (message or "").strip():
		return {"ok": False, "error": _("Nothing to answer.")}

	history = _parsed(history) or []
	context = _parsed(context) or {}
	context.setdefault("channel", "Desk")
	user = (context.get("user") or "").strip() or None
	# Which agent is asking is the site's to say; what that agent may do is
	# ours. A config sent from a tenant is ignored outright, so a customer who
	# edits their own copy of the client cannot grant themselves draft rights
	# the Analyst was never given. `deny_drafts` is honoured because it only
	# ever takes something away.
	agent_id = (context.get("agent") or "").strip() or None
	config = brain_agents.config(
		agent_id,
		channel=context.get("channel"),
		deny_drafts=bool(cint(context.get("deny_drafts"))),
	) if agent_id else None
	# Their own notes from earlier sessions, which are theirs to send and ours
	# to append rather than invent. Capped so a tenant cannot rewrite the prompt.
	extra = (context.pop("extra_system", None) or "")[:4000] or None

	try:
		target = erp.for_user(site_name, user)
	except erp.ErpNotConnected as e:
		frappe.local.response["http_status_code"] = 409
		return {"ok": False, "error": str(e), "reconnect": True}

	# The free path first, when the caller says it is safe. Only the caller
	# knows whether this is a fresh question or the middle of a thread, and on
	# WhatsApp a one-off question mid-chat is still worth answering for nothing.
	if cint(allow_instant):
		ctx = brain_agent.site_context(target)
		hit = instant.answer(target, message, company=ctx.get("company"),
		                     number_format=ctx.get("number_format"))
		if hit:
			hit["ok"] = True
			hit["acting_as"] = target.acting_as
			return hit

	out = brain_agent.run_agent(target, message, history=history, mode=mode or "desk",
	                            extra_system=extra, config=config, context=context)
	out["ok"] = True
	out["acting_as"] = target.acting_as
	return out


def _target(site_name, context=None):
	"""The customer's site, bound to whoever is asking, plus their conventions."""
	from erpera_admin import erp
	from erpera_admin.brain import agent as brain_agent

	context = _parsed(context) or {}
	user = (context.get("user") or "").strip() or None
	target = erp.for_user(site_name, user)
	return target, brain_agent.site_context(target)


@frappe.whitelist(allow_guest=True)
def quick_catalog(site=None, token=None):
	"""The registry as the picker needs it: ids and labels, no filters.

	What a query actually does stays here. A customer's screen needs to offer
	"Outstanding Receivables"; it does not need the doctype, the filter set or
	the ordering that makes it work.
	"""
	from erpera_admin.brain import queries as qq

	_authenticate(site, token)
	return {"ok": True, "queries": [
		{"id": q["id"], "label": q["label"], "department": q["department"],
		 "kind": q["kind"], "follow_ups": q.get("follow_ups") or []}
		for q in qq.QUERIES.values()
	]}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def quick_run(site=None, token=None, query_id=None, company=None,
              extra_filters=None, order_by=None, context=None):
	"""Run one registry query against their site and return the rendered result."""
	from erpera_admin.brain import queries as qq

	site_name = _authenticate(site, token)
	target, ctx = _target(site_name, context)
	res = qq.execute(target, query_id, company or ctx.get("company"),
	                 extra_filters=_parsed(extra_filters), order_by=order_by,
	                 number_format=ctx.get("number_format"))
	return {"ok": True, "result": res}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def quick_ask(site=None, token=None, message=None, company=None, context=None):
	"""Basic mode, answered without a model: the result, a choice, or a miss."""
	from erpera_admin.brain import instant

	site_name = _authenticate(site, token)
	target, ctx = _target(site_name, context)
	return {"ok": True, **instant.basic(target, message,
	                                    company=company or ctx.get("company"),
	                                    number_format=ctx.get("number_format"))}


@frappe.whitelist(allow_guest=True)
def dashboard(site=None, token=None, company=None, days=None, context=None):
	"""Today's activity, or the trend behind it when days is given."""
	from erpera_admin.brain import dashboard as dash

	site_name = _authenticate(site, token)
	target, ctx = _target(site_name, context)
	company = company or ctx.get("company")
	if days:
		return {"ok": True, "trends": dash.get_dashboard_trends(target, company, cint(days))}
	return {"ok": True, "today": dash.today_summary(target, company)}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def read_document(site=None, token=None, image=None, context=None):
	"""Read a bill and say what to do with it.

	The image comes up, the fields come down. What the model was told to look
	for stays here, which is the whole reason this endpoint exists rather than
	the site calling a vision model itself.
	"""
	from erpera_admin import relay as relay_mod
	from erpera_admin.brain import vision

	site_name = _authenticate(site, token)
	if not relay_mod.enabled():
		frappe.local.response["http_status_code"] = 503
		return {"ok": False, "error": "relay disabled"}

	v = vision.extract(site_name, image, context=_parsed(context) or {})
	return {"ok": True, "extracted": v, "message": vision.to_message(v)}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def hear(site=None, token=None, audio=None, filename=None, mime=None, context=None):
	"""Turn a voice note into text, with our key rather than one of theirs."""
	from erpera_admin import relay as relay_mod

	site_name = _authenticate(site, token)
	if not relay_mod.enabled():
		frappe.local.response["http_status_code"] = 503
		return {"ok": False, "error": "relay disabled"}

	return {"ok": True, **relay_mod.hear(site_name, audio=audio,
	                                     filename=filename or "voice.ogg",
	                                     mime=mime or "audio/ogg",
	                                     context=_parsed(context) or {})}
