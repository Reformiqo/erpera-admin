# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

"""The agent loop, run here instead of on the customer's site.

Everything in this file used to ship inside the app the customer installs,
which meant the customer could read the prompts, retune them, widen the tool
list, or delete the sentence that stops the model submitting a document. None
of that was a permission problem that permissions could fix: they are
Administrator on their own site, so code that lives there is theirs.

So the loop moved up here. The prompt text below is copied from the tenant
version word for word, because it is the part that took the longest to get
right and the part worth protecting. What changed is where its two halves
reach: the model is called through the relay, which already holds our provider
keys and charges the site; and the tools are called through erp.Site, which
reaches back into the customer's own data with the asking user's key.

The turn now spans two networks, so the deadline matters more than it did. A
local tool call was a few milliseconds and the only slow part was the provider.
Now every tool call is an HTTPS round trip to their site, so the 90-second
budget below is spent in larger pieces and the loop has to be able to stop
mid-way and still say something useful.
"""

import time

import frappe
from frappe.utils import nowdate

from erpera_admin import relay
from erpera_admin.brain.tools import NAV_TOOLS, READ_TOOLS, WRITE_TOOLS, execute_tool

MAX_ITERATIONS = 10
MAX_TOOL_RESULT = 3000
MAX_HISTORY = 24  # messages kept verbatim; older tool noise gets compacted
CONTEXT_TTL = 600  # their default company and currency, cached between turns


def site_context(site):
	"""Company, currency and number format, read once and remembered briefly."""
	# Versioned: a cached dict of the old shape outlives the code that wrote it.
	key = f"erpera:site-ctx:2:{site.site}"
	cached = frappe.cache().get_value(key)
	if cached:
		return cached
	company = site.single_value("Global Defaults", "default_company") or ""
	currency = (site.get_value("Company", {"name": company}, "default_currency")
	            if company else "") or "INR"
	# Their grouping, not ours. A brain serving many sites must not render an
	# Indian site's total as 1,482,650.00.
	number_format = site.single_value("System Settings", "number_format") or ""
	out = {"company": company, "currency": currency, "number_format": number_format}
	frappe.cache().set_value(key, out, expires_in_sec=CONTEXT_TTL)
	return out


def cowork_prompt(ctx):
	company = ctx.get("company") or ""
	currency = ctx.get("currency") or "INR"
	user = ctx.get("user") or ""
	return f"""You are AI Assist CoWork, an AI operator that creates and manages records in this ERPNext site by chat. You have full READ access and can create DRAFTS.

HOW YOU WORK
- READ freely to understand before acting: look up customers, items, suppliers, invoices, stock, field schemas. NEVER guess a value you can look up. Resolve any name the user mentions with search_link; if several match, ask which one.
- To create something, gather what's needed and call create_record. Do NOT look up accounts, cost centers or payment allocations yourself. create_record resolves all of that automatically. For a Payment Entry you only need: payment_type, party_type, party, paid_amount, mode_of_payment. It creates a DRAFT and resolves the accounting automatically. After creating a draft, STOP and tell the user to review it and reply "submit". You have NO submit tool. Finalizing is the user's decision, never yours.
- To change a draft before submission, use update_record.
- References like "the last customer" or "the first item created in 2025": resolve with list_documents ordered by creation, and say which record you picked.
- Master records (Customer, Supplier, Item, Lead) always show docstatus 0. That is NORMAL; they are live, usable records, not drafts. Only transactions (invoices, payments, orders, stock entries) have a draft→submit lifecycle.

CREATING NEW MASTER RECORDS
- If an Item / Customer / Supplier the user refers to does NOT exist, ASK first: "X isn't in your ERP yet. Should I create it?" Only create after they agree.
- New Items on GST sites usually need an HSN/SAC code (field gst_hsn_code).
  - If the user GAVE an HSN code, use it exactly as given. Do not search, question or replace it.
  - If you have NO code, find candidates with ONE list_documents call: doctype "GST HSN Code", fields ["name","description"], filters [["description","like","%<the product noun, e.g. "yarn" for "Linen Yarn 290D">%"]], limit 10. Do NOT fire many search_link probes. Suggest the 2-3 best matches (code + description); the user picks or types their own. Also consider the HSN of a similar existing Item.
  - If a given/chosen code doesn't exist in "GST HSN Code", tell the user and show the closest matches instead of failing.
  - Never invent an HSN, and never create the Item until the user has confirmed one.

STYLE
- Concise and conversational. Do multi-step lookups silently, then answer.
- Never use an em dash. Write two sentences, or use a comma or a colon.
- After a draft: summarize name, key fields, total, and end with the markdown link [View in Desk](<link>).
- If a create fails, translate the error into a plain question ("ERPNext needs a Bill No. What is it?"), don't dump tracebacks.

CONTEXT: Today {nowdate()} · Company: {company or "(site default)"} · Currency: {currency or "INR"} · User: {user}"""


def max_prompt(ctx):
	company = ctx.get("company") or ""
	user = ctx.get("user") or ""
	return f"""You are AI Assist, an AI analyst for this ERPNext site. You have READ access via tools; you cannot create or change anything.

RULES
- ALWAYS pass a fields array to list_documents. Use filters and order_by; keep limits small.
- Use run_report for accounting questions (Accounts Receivable, General Ledger, Profit and Loss Statement, Stock Balance...).
- Answer with real numbers from the tools. Never invent figures. Use markdown tables for lists.
- Never use an em dash. Write two sentences, or use a comma or a colon.
- When helpful, end with a desk link like [Open list](/app/sales-invoice).
- Keep responses concise.

CONTEXT: Today {nowdate()} · Company: {company or "(site default)"} · User: {user}"""


def _compact(history):
	"""Trim old tool results so long chats stay under context limits."""
	if len(history) <= MAX_HISTORY:
		return history
	old, keep = history[:-MAX_HISTORY], history[-MAX_HISTORY:]
	compacted = []
	for m in old:
		if m.get("role") == "tool":
			c = str(m.get("content") or "")
			m = {**m, "content": c[:400] + ("…" if len(c) > 400 else "")}
		compacted.append(m)
	return compacted + keep

DESK_EXTRA = """

DESK NAVIGATION
- You are embedded in the user's ERPNext Desk. When they ask to SHOW / OPEN / SEE records
  ("show outstanding invoices", "open ACME's file"), call navigate with the right view and
  filters, and the Desk will open it for them. Combine it with a short factual answer
  (e.g. count/total) when that helps.
- Prefer list views with precise filters over dumping tables in chat.
- BE FAST: make the fewest tool calls that answer the question. Never repeat a call
  with the same arguments, and don't cross-check a report with extra queries unless
  it actually failed. Keep the written answer to a few lines, because the list/report you
  opened carries the detail.
- An EMPTY result is a real answer ("no sales invoices in that period"). Report it
  instead of re-running the same question with different filters.
- Whenever your answer is ABOUT a set of records (outstanding invoices, low stock,
  missing codes, overdue anything), call navigate to the filtered list/report in the
  SAME turn, alongside your short summary. NEVER ask "would you like me to open the
  list?". Opening it IS part of the answer. Ask only when the target is ambiguous.
- The Desk OPENS every document you create automatically. Never add "View in Desk"
  links or tell the user where to find it; just confirm what was created/drafted."""


NO_UI_EXTRA = """

NO SCREEN
- The user is NOT in the Desk. They are reading this in a chat app. There is no
  page, list, report or link they can open, and no "bottom of the page" to look at.
- So put the actual answer in your words: the number, the names, the amounts. If a
  total is what they asked for, compute it from the tool results and state it.
- Never say you opened, filtered or displayed anything, and never offer a link.
- Keep it short enough to read on a phone: a few lines, or a compact list of at
  most 10 items with the key figure on each. No markdown tables."""


def _nav_reply(actions):
	"""One-line confirmation for a navigation-only turn (no extra LLM roundtrip)."""
	a = (actions or [{}])[-1]
	what = a.get("report") or a.get("doctype") or "it"
	kind = {"list": " list", "form": "", "report": " report", "query-report": " report", "new": " form"}.get(a.get("type"), "")
	name = (" " + a["name"]) if a.get("type") == "form" and a.get("name") else ""
	return "Opened the " + what + name + kind + "."


def run_agent(site, message, history=None, mode="cowork", extra_system=None,
              config=None, context=None):
	"""One conversational turn for one customer site.

	site: an erp.Site already bound to the asking user's credential.
	Returns {reply, history, last_draft?, actions?, credits?, credits_spent}.

	config (data-driven agents): {label, instructions, allow_reads,
	allow_navigation, allow_drafts} — overrides mode-based tool selection."""
	context = dict(context or {})
	prompt_ctx = dict(site_context(site))
	prompt_ctx["user"] = context.get("user") or site.acting_as or ""

	if config:
		tools = []
		if config.get("allow_reads", 1):
			tools += READ_TOOLS
		if config.get("allow_navigation", 1):
			tools += NAV_TOOLS
		drafts = bool(config.get("allow_drafts"))
		if drafts:
			tools += WRITE_TOOLS
		base = cowork_prompt(prompt_ctx) if drafts else max_prompt(prompt_ctx)
		if config.get("allow_navigation", 1):
			base += DESK_EXTRA
		else:
			# Off-Desk surfaces (WhatsApp): there is no page to open and no link to
			# click, so the whole answer has to be the text itself.
			base += NO_UI_EXTRA
		scope = config.get("write_doctypes") or []
		if drafts and scope:
			base += ("\n\nCREATE SCOPE\n- You may ONLY create these document types: " + ", ".join(scope)
				+ ". If the user asks you to create anything else, do NOT try. Tell them to switch to the Operator agent for that.")
		base = base.replace(
			"and end with the markdown link [View in Desk](<link>)",
			"and do not add a link; the Desk opens it automatically")
		if config.get("label"):
			# Both product names collapse to a sentinel first, then expand once.
			# Chaining the replacements directly would rematch the text the
			# previous one just wrote ("AI Assist Operator Operator").
			base = (
				base.replace("AI Assist CoWork", "\x00AGENT\x00")
				.replace("AI Assist", "\x00AGENT\x00")
				.replace("\x00AGENT\x00", "AI Assist " + config["label"])
			)
		if config.get("instructions"):
			base += "\n\nYOUR SPECIALTY\n" + config["instructions"]
	elif mode == "desk":                     # Analyst: read + navigate
		tools = READ_TOOLS + NAV_TOOLS
		base = max_prompt(prompt_ctx) + DESK_EXTRA
	elif mode == "desk_operator":            # Operator: read + navigate + drafts
		tools = READ_TOOLS + NAV_TOOLS + WRITE_TOOLS
		base = cowork_prompt(prompt_ctx) + DESK_EXTRA
	elif mode == "cowork":
		tools = READ_TOOLS + WRITE_TOOLS
		base = cowork_prompt(prompt_ctx)
	else:
		tools = READ_TOOLS
		base = max_prompt(prompt_ctx)
	system = base + (extra_system or "")
	messages = _compact(list(history or [])) + [{"role": "user", "content": message}]
	ctx = {}
	if config and config.get("allow_drafts") and config.get("write_doctypes"):
		ctx["allowed_doctypes"] = list(config["write_doctypes"])
	reply = ""
	used_reads = False
	spent = 0.0
	credits_left = None
	# Their site's latency is now inside our budget, so the loop stops earlier
	# than the web worker would.
	deadline = time.monotonic() + 90

	for _ in range(MAX_ITERATIONS):
		if time.monotonic() > deadline:
			reply = (reply or "") + "\n\n(That took longer than I can hold a turn open, so I had to stop early. Say \"continue\" to pick up where I left off.)"
			break
		# Capped: generation time scales with output length, and a desk reply is a few
		# lines plus tool arguments — 4k tokens only buys long tables nobody reads.
		out = relay.chat(site.site, messages=messages, system=system, tools=tools,
		                 max_tokens=1500, context=context)
		spent += float(out.get("credits_spent") or 0)
		if out.get("credits") is not None:
			credits_left = out["credits"]
		msg = out["raw_message"]
		messages.append(msg)
		if not out["tool_calls"]:
			reply = out["text"] or reply
			break
		reply = out["text"] or reply
		for call in out["tool_calls"]:
			if call["name"] != "navigate":
				used_reads = True
			result = execute_tool(site, call["name"], call["arguments"] or {}, ctx)
			if len(result) > MAX_TOOL_RESULT:
				result = result[:MAX_TOOL_RESULT] + "\n[TRUNCATED]"
			messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
		if not used_reads and all(c["name"] == "navigate" for c in out["tool_calls"]):
			# Pure "open X" request: the Desk is already navigating, so another
			# roundtrip would only narrate it. Confirm locally and finish.
			reply = reply or _nav_reply(ctx.get("actions"))
			break
	else:
		reply = reply or "I ran out of steps. Try breaking the request into smaller parts."

	res = {"reply": reply or "Done.", "history": messages, "credits_spent": spent}
	if credits_left is not None:
		res["credits"] = credits_left
	if ctx.get("last_draft"):
		res["last_draft"] = ctx["last_draft"]
	if ctx.get("actions"):
		res["actions"] = ctx["actions"]
	return res
