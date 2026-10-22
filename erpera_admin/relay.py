# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

"""Answering on a customer's behalf, so their site never learns how.

The problem this solves is not access control, it is physics. A customer runs
their own Frappe site and is Administrator on it, so every permission in the
product is one they can bypass with a single console command. Anything their
site holds, they can read: the provider name, the model name, the key, the
cost of a call. Permlevels, encrypted fields and hidden form sections do not
change that and should never be sold as if they do.

So the only way to keep which model we use to ourselves is for their site to
stop calling the model. It sends us the conversation, we call the provider
with our key, and we send back the answer and nothing else. The reply carries
no provider, no model, no upstream cost. What they can see is what they are
entitled to see: their own question, their own answer, and what it cost them
in credits.

Two consequences worth stating plainly. Their prompts now pass through us, so
this module never writes them down unless log_content is explicitly on. And we
now carry their latency and their uptime, so a failure here has to read as a
sentence a person can act on rather than a traceback.
"""

import time
import uuid

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from erpera_admin import credits as credits_mod
from erpera_admin import llm

MAX_TOKENS_CAP = 8192          # a tenant cannot ask us to buy it a novel
MAX_MESSAGES = 200             # nor replay an unbounded history at our expense
MAX_CHARS = 400_000            # roughly 100k tokens of conversation


class RelayRefused(frappe.ValidationError):
	"""This site may not have an answer right now, and is told why."""


def enabled():
	return bool(cint(frappe.db.get_single_value("ERPERA AI Settings", "relay_enabled")))


def _settings():
	return frappe.get_cached_doc("ERPERA AI Settings")


def _slot(field_provider, field_model):
	"""One provider, with its key. Returns None when not configured."""
	s = _settings()
	name = s.get(field_provider)
	if not name:
		return None
	cred = frappe.get_cached_doc("AI Provider Credential", name)
	if not cint(cred.get("enabled")):
		return None
	key = frappe.utils.password.get_decrypted_password(
		"AI Provider Credential", name, "api_key", raise_exception=False)
	if not key:
		return None
	return {
		"provider": (cred.provider or "").lower(),
		"model": s.get(field_model) or cred.default_model or "",
		"base_url": cred.base_url or "",
		"api_key": key,
		"credential": name,
	}


def _order():
	"""Primary first, fallback second. Never empty without saying so."""
	chain = [s for s in (_slot("active_provider", "active_model"),
	                     _slot("fallback_provider", "fallback_model")) if s]
	if not chain:
		# Ours to fix, so it goes to our log; the customer gets a sentence.
		frappe.log_error(title="Relay has no usable provider",
		                 message="No enabled AI Provider Credential with a stored key.")
		frappe.throw(_("AI Assist is temporarily unavailable. Please try again shortly."),
		             RelayRefused)
	return chain


def _validate(messages, max_tokens):
	if not isinstance(messages, list) or not messages:
		frappe.throw(_("Nothing to answer."), RelayRefused)
	if len(messages) > MAX_MESSAGES:
		frappe.throw(_("That conversation is too long to answer. Start a new one."),
		             RelayRefused)
	size = sum(len(str(m.get("content") or "")) for m in messages)
	if size > MAX_CHARS:
		frappe.throw(_("That conversation is too long to answer. Start a new one."),
		             RelayRefused)
	return min(cint(max_tokens) or 4096, MAX_TOKENS_CAP)


def _gate(site):
	"""Refuse before spending anything, in the shape the site already handles."""
	wallet = credits_mod.check(site)
	if not wallet.get("ok"):
		frappe.throw(wallet.get("message") or _("This site cannot make AI calls right now."),
		             RelayRefused)
	return wallet


def _record(site, ctx, kind, provider, model, usage, duration_ms, status,
            cost, fallback_used, error=None, prompt=None, response=None,
            tool_calls=None):
	"""One AI Call Record on our side. The provider and model live here only."""
	s = _settings()
	doc = {
		"doctype": "AI Call Record",
		"site": site,
		# Nothing on the tenant to key against: we made this call, not them.
		"remote_name": f"relay-{uuid.uuid4().hex[:16]}",
		"occurred_on": now_datetime(),
		"user": (ctx or {}).get("user"),
		"channel": (ctx or {}).get("channel") or "Desk",
		"kind": kind,
		"provider": provider,
		"model": model,
		"fallback_used": 1 if fallback_used else 0,
		"status": status,
		"input_tokens": cint(usage.get("prompt_tokens") or usage.get("input_tokens")),
		"output_tokens": cint(usage.get("completion_tokens") or usage.get("output_tokens")),
		"duration_ms": cint(duration_ms),
		"cost": flt(cost, credits_mod.PRECISION),
		"currency": "INR",
		"rated": 1 if cost else 0,
		"agent": (ctx or {}).get("agent"),
		"correlation_id": (ctx or {}).get("correlation_id"),
		"error": (str(error)[:8000] if error else None),
	}
	doc["total_tokens"] = doc["input_tokens"] + doc["output_tokens"]
	if cint(s.get("log_content")):
		doc["prompt"] = (prompt or "")[:8000] or None
		doc["response"] = (response or "")[:8000] or None
		doc["tool_calls"] = (str(tool_calls)[:8000] if tool_calls else None)
	return frappe.get_doc(doc).insert(ignore_permissions=True)


def _prompt_text(messages, system):
	parts = ([f"system: {system}"] if system else [])
	for m in messages[-6:]:
		parts.append(f"{m.get('role')}: {str(m.get('content') or '')[:1000]}")
	return "\n".join(parts)[:8000]


def _cost(provider, model, usage):
	"""Rupees, worked out from our own rate card."""
	return credits_mod.cost_of({
		"provider": provider,
		"model": model,
		"input_tokens": cint(usage.get("prompt_tokens") or usage.get("input_tokens")),
		"output_tokens": cint(usage.get("completion_tokens") or usage.get("output_tokens")),
	})


def _sanitise(out):
	"""What goes back down the wire.

	Everything identifying the model is dropped here rather than at the tenant,
	because a value removed after it has crossed the network has already been
	read by anyone watching.
	"""
	raw = llm.clean_msg(out.get("raw_message") or {})
	usage = out.get("usage") or {}
	return {
		"text": out.get("text") or "",
		"tool_calls": out.get("tool_calls") or [],
		"raw_message": raw,
		"usage": {
			"prompt_tokens": cint(usage.get("prompt_tokens") or usage.get("input_tokens")),
			"completion_tokens": cint(usage.get("completion_tokens") or usage.get("output_tokens")),
		},
	}


def chat(site, messages=None, system=None, tools=None, max_tokens=4096, context=None):
	"""One completion turn, made by us, charged to them."""
	max_tokens = _validate(messages, max_tokens)
	wallet = _gate(site)
	billable = wallet.get("reason") == "ok"
	ctx = context or {}

	chain = _order()
	first_error = None

	for i, slot in enumerate(chain):
		t0 = time.perf_counter()
		try:
			out = llm.dispatch(slot["provider"], messages, system, tools,
			                   slot["api_key"], slot["model"], max_tokens,
			                   base_url=slot["base_url"],
			                   # Another provider untried: switching beats sleeping.
			                   backoff_429=(len(chain) == 1))
		except llm.ProviderError as e:
			first_error = first_error or e
			ms = int((time.perf_counter() - t0) * 1000)
			_record(site, ctx, "chat", slot["provider"], slot["model"], {}, ms,
			        "Failed", 0, i > 0, error=e,
			        prompt=_prompt_text(messages, system))
			frappe.db.commit()
			continue

		ms = int((time.perf_counter() - t0) * 1000)
		usage = out.get("usage") or {}
		cost = _cost(slot["provider"], out.get("model") or slot["model"], usage)

		rec = _record(site, ctx, "chat", slot["provider"],
		              out.get("model") or slot["model"], usage, ms, "Success",
		              cost, i > 0, prompt=_prompt_text(messages, system),
		              response=out.get("text"),
		              tool_calls=[c.get("name") for c in out.get("tool_calls") or []])
		if billable:
			credits_mod.consume(site, cost, call_record=rec.name)
		frappe.db.commit()

		payload = _sanitise(out)
		payload["fallback_used"] = i > 0
		payload["credits"] = credits_mod.balance(site)["credits"]
		payload["credits_spent"] = cost
		return payload

	# Both providers refused. Which one and why is ours to read, not theirs.
	frappe.log_error(title=f"Relay chat failed for {site}",
	                 message=str(first_error)[:2000])
	frappe.throw(_("AI Assist could not get an answer just now. Please try again."),
	             RelayRefused)


def vision(site, image_data_url=None, prompt=None, max_tokens=2048, context=None):
	"""One image, one question. Same rules as chat."""
	if not image_data_url or not prompt:
		frappe.throw(_("Nothing to look at."), RelayRefused)
	wallet = _gate(site)
	billable = wallet.get("reason") == "ok"
	ctx = context or {}
	max_tokens = min(cint(max_tokens) or 2048, MAX_TOKENS_CAP)

	first_error = None
	for i, slot in enumerate(_order()):
		model = llm.VISION_MODELS.get(slot["provider"])
		if not model:
			continue
		t0 = time.perf_counter()
		try:
			text, usage = llm.vision(slot["provider"], slot["api_key"], model,
			                         image_data_url, prompt, max_tokens,
			                         base_url=slot["base_url"])
		except llm.ProviderError as e:
			first_error = first_error or e
			ms = int((time.perf_counter() - t0) * 1000)
			_record(site, ctx, "vision", slot["provider"], model, {}, ms,
			        "Failed", 0, i > 0, error=e, prompt=prompt)
			frappe.db.commit()
			continue

		ms = int((time.perf_counter() - t0) * 1000)
		cost = _cost(slot["provider"], model, usage or {})
		rec = _record(site, ctx, "vision", slot["provider"], model, usage or {},
		              ms, "Success", cost, i > 0, prompt=prompt, response=text)
		if billable:
			credits_mod.consume(site, cost, call_record=rec.name)
		frappe.db.commit()
		return {"text": text or "", "credits_spent": cost,
		        "credits": credits_mod.balance(site)["credits"]}

	frappe.log_error(title=f"Relay vision failed for {site}",
	                 message=str(first_error)[:2000])
	frappe.throw(_("AI Assist could not read that image just now."), RelayRefused)


# ─── speech ───────────────────────────────────────────────────────────────────

# Whisper invents fluent text from silence or noise: a plain tone comes back as
# "Продолжение следует...", a caption it over-learned. Pinning the language
# removes most of it, and a result with almost no letters left is treated as
# "nothing was said" rather than passed downstream as an instruction.
MIN_MEANINGFUL_CHARS = 2


def _provider_slot(name):
	"""The first enabled credential for one named provider, with its key.

	Transcription is not the active chat provider's job: a site can be answering
	through Claude and still need Groq's Whisper. So this looks the provider up
	by name rather than by which slot is currently in use.
	"""
	for cred_name in frappe.get_all("AI Provider Credential",
	                                filters={"provider": name, "enabled": 1},
	                                order_by="modified desc", pluck="name"):
		key = frappe.utils.password.get_decrypted_password(
			"AI Provider Credential", cred_name, "api_key", raise_exception=False)
		if key:
			cred = frappe.get_cached_doc("AI Provider Credential", cred_name)
			return {"provider": name, "api_key": key, "base_url": cred.base_url or "",
			        "credential": cred_name}
	return None


def hear(site, audio=None, filename="voice.ogg", mime="audio/ogg", context=None):
	"""One voice note, turned into text by us.

	This used to happen on the customer's site, which meant a Groq key had to
	sit there for it, in plain reach of the person whose key it was not. Moving
	it here is the same argument as the rest of the relay: the only way to keep
	a key is to be the one holding it.
	"""
	import base64

	if not audio:
		frappe.throw(_("Nothing to listen to."), RelayRefused)
	wallet = _gate(site)
	billable = wallet.get("reason") == "ok"
	ctx = context or {}

	slot = _provider_slot("groq")
	if not slot:
		frappe.log_error(title="Relay cannot transcribe",
		                 message="No enabled Groq credential with a stored key.")
		frappe.throw(_("Voice notes are not available right now."), RelayRefused)

	try:
		data = base64.b64decode(audio)
	except Exception:
		frappe.throw(_("That voice note could not be read."), RelayRefused)

	model = llm.TRANSCRIBE_MODEL
	t0 = time.perf_counter()
	try:
		text, seconds = llm.transcribe(slot["api_key"], data, filename=filename,
		                               mime=mime, base_url=slot["base_url"] or None)
	except llm.ProviderError as e:
		ms = int((time.perf_counter() - t0) * 1000)
		_record(site, ctx, "transcription", "groq", model, {}, ms, "Failed", 0, False,
		        error=e)
		frappe.db.commit()
		frappe.log_error(title=f"Relay transcription failed for {site}",
		                 message=str(e)[:2000])
		frappe.throw(_("That voice note could not be transcribed. Please try again."),
		             RelayRefused)

	ms = int((time.perf_counter() - t0) * 1000)
	# Billed by audio duration rather than tokens, so the seconds go in the
	# token column that rating already reads.
	usage = {"prompt_tokens": seconds, "completion_tokens": 0}
	cost = _cost("groq", model, usage)
	rec = _record(site, ctx, "transcription", "groq", model, usage, ms, "Success",
	              cost, False, prompt=f"[voice note, {seconds}s]", response=text)
	if billable:
		credits_mod.consume(site, cost, call_record=rec.name)
	frappe.db.commit()

	if sum(c.isalnum() for c in text) < MIN_MEANINGFUL_CHARS:
		text = ""
	return {"text": text, "seconds": seconds, "credits_spent": cost,
	        "credits": credits_mod.balance(site)["credits"]}
