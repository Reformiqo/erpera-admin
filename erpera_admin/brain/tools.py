# Copyright (c) 2026, The Tiny App Company and contributors
# For license information, please see license.txt

"""The agent's hands, reaching across the network.

This is the tenant-side tool executor moved here and rewritten against their
REST API. The tool definitions below are unchanged on purpose: they are what
the model reads, they took a long time to word, and they are the part a customer
must not be able to edit. The executor underneath them is what changed, from
in-process `frappe.get_list` to a signed request back into their site.

Two consequences of that move are visible in this file.

A round trip now costs real time, so anything that used to be cheap because it
was local gets batched. `aggregate` asks their database to do the summing in one
request rather than pulling rows back to add up here, and `get_doctype_fields`
reads two small child tables instead of the meta bundle, which is a megabyte
for an invoice.

And a Payment Entry can no longer lean on erpnext's own helpers, because
`get_party_account` and `get_default_bank_cash_account` are not whitelisted and
never should be. `_enrich_payment_entry` works the same answers out of plain
reads: the mode of payment's account for the company, the party's account with
the company default behind it. It costs a handful of extra requests on a write,
which is the one moment the user expects to wait.

What did not move: nothing here submits. There is no submit tool, the same as
before. The key we hold could submit, which is exactly why the capability is
absent rather than merely gated.
"""

import json

import frappe
from frappe.utils import cint, flt, nowdate

from erpera_admin import erp

MAX_RESULT_CHARS = 3000
MASTERS = {"Item", "Customer", "Supplier", "Lead", "Contact", "Address"}


def _truncate(s, label="result too large"):
	if len(s) <= MAX_RESULT_CHARS:
		return s
	return s[:MAX_RESULT_CHARS] + f"\n... [TRUNCATED: {label}]"


def _err(msg):
	return json.dumps({"error": str(msg)[:400]})


def desk_link(doctype, name):
	return f"/app/{doctype.replace(' ', '-').lower()}/{name}"


def is_submittable(site, doctype):
	try:
		return bool(site.fields_of(doctype)["is_submittable"])
	except Exception:
		return doctype not in MASTERS


# ─── Tool definitions (OpenAI dialect) ────────────────────────────────────────

READ_TOOLS = [
	{
		"type": "function",
		"function": {
			"name": "list_documents",
			"description": "List documents of a DocType with filters. ALWAYS pass a fields array.",
			"parameters": {
				"type": "object",
				"properties": {
					"doctype": {"type": "string"},
					"fields": {"type": "array", "items": {"type": "string"}},
					"filters": {"description": "Frappe filters, e.g. [[\"status\",\"=\",\"Overdue\"]]. For a relative period use Timespan: [[\"creation\",\"Timespan\",\"this month\"]]. For a range use Between: [[\"posting_date\",\"Between\",[\"2026-04-01\",\"2026-06-30\"]]]."},
					"order_by": {"type": "string"},
					"limit": {"type": "number"},
				},
				"required": ["doctype"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "get_document",
			"description": "Fetch one document by name (full field values incl. child tables).",
			"parameters": {
				"type": "object",
				"properties": {"doctype": {"type": "string"}, "name": {"type": "string"}},
				"required": ["doctype", "name"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "count_documents",
			"description": "Count documents matching filters.",
			"parameters": {
				"type": "object",
				"properties": {"doctype": {"type": "string"}, "filters": {}},
				"required": ["doctype"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "aggregate",
			"description": (
				"Sum and count matching documents in ONE call. The only correct way to "
				"answer 'what is the total/how much/how many' questions. NEVER add up or "
				"count rows from list_documents: that list is capped and will give a wrong "
				"total. Optionally group_by a field to get a breakdown (e.g. per customer)."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"doctype": {"type": "string"},
					"filters": {"description": "Frappe filters, e.g. [[\"outstanding_amount\",\">\",0]]"},
					"sum_fields": {"type": "array", "items": {"type": "string"},
						"description": "Currency/number fields to total, e.g. [\"outstanding_amount\"]"},
					"group_by": {"type": "string", "description": "Optional field to break the total down by."},
					"limit": {"type": "number", "description": "Max groups to return (default 20)."},
				},
				"required": ["doctype"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "search_link",
			"description": "Fuzzy-search records by name/title (autocomplete). Use to resolve any name the user mentions to a real record.",
			"parameters": {
				"type": "object",
				"properties": {"doctype": {"type": "string"}, "text": {"type": "string"}},
				"required": ["doctype", "text"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "get_doctype_fields",
			"description": "Field schema of a DocType: names, types, required, options. Use before creating a document type you haven't created yet.",
			"parameters": {
				"type": "object",
				"properties": {"doctype": {"type": "string"}},
				"required": ["doctype"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "run_report",
			"description": "Run a standard report (Accounts Receivable, General Ledger, Stock Balance, Profit and Loss Statement, ...). Pass report filters like company / from_date / to_date.",
			"parameters": {
				"type": "object",
				"properties": {"report_name": {"type": "string"}, "filters": {"type": "object"}},
				"required": ["report_name"],
			},
		},
	},
]

NAV_TOOLS = [
	{
		"type": "function",
		"function": {
			"name": "navigate",
			"description": (
				"Navigate the user's ERPNext Desk. Use when the user asks to SHOW/OPEN/SEE records. "
				"Open the real list/form/report with filters applied. You can navigate AND answer "
				"with numbers in the same turn."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"view": {"type": "string", "enum": ["list", "form", "report", "query-report", "new"]},
					"doctype": {"type": "string", "description": "DocType for list/form/new"},
					"name": {"type": "string", "description": "Document name (form view)"},
					"report": {"type": "string", "description": "Report name (query-report view)"},
					"filters": {
						"type": "object",
						"description": 'List filters, e.g. {"outstanding_amount": [">", 0], "status": "Overdue"}. For a relative period use Timespan: {"creation": ["Timespan", "this month"]}, never a bare <= on today. For a range use Between: {"posting_date": ["Between", ["2026-04-01", "2026-06-30"]]}.',
					},
				},
				"required": ["view"],
			},
		},
	},
]

WRITE_TOOLS = [
	{
		"type": "function",
		"function": {
			"name": "create_record",
			"description": (
				"Create a document. Transactions (invoices, payments, orders, stock entries...) are created as "
				"DRAFTS the user must submit; you cannot submit. Masters (Item/Customer/Supplier/Lead) are live "
				"on create. For item documents pass items:[{item_code,qty,rate}]. For Payment Entry pass "
				"payment_type, party_type, party, paid_amount, mode_of_payment. Accounts and invoice "
				"allocation are resolved automatically."
			),
			"parameters": {
				"type": "object",
				"properties": {"doctype": {"type": "string"}, "data": {"type": "object"}},
				"required": ["doctype", "data"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "update_record",
			"description": "Update fields on an existing DRAFT document (docstatus 0). Cannot touch submitted documents.",
			"parameters": {
				"type": "object",
				"properties": {
					"doctype": {"type": "string"},
					"name": {"type": "string"},
					"data": {"type": "object"},
				},
				"required": ["doctype", "name", "data"],
			},
		},
	},
]


# ─── Payment Entry enrichment, from plain reads ───────────────────────────────

def _default_company(site, given=None):
	return given or site.single_value("Global Defaults", "default_company") or ""


def _mode_of_payment_account(site, mop, company):
	rows = site.get_list(
		"Mode of Payment Account", parent="Mode of Payment",
		filters=[["parent", "=", mop], ["company", "=", company]],
		fields=["default_account"], limit=1)
	return (rows[0].get("default_account") if rows else None)


def _party_account(site, party_type, party, company):
	"""What erpnext's get_party_account works out, without the import."""
	if party:
		rows = site.get_list(
			"Party Account", parent=party_type,
			filters=[["parent", "=", party], ["parenttype", "=", party_type],
			         ["company", "=", company]],
			fields=["account"], limit=1)
		if rows and rows[0].get("account"):
			return rows[0]["account"]
	field = "default_receivable_account" if party_type == "Customer" else "default_payable_account"
	return site.get_value("Company", {"name": company}, field)


def _enrich_payment_entry(site, doc_dict):
	company = _default_company(site, doc_dict.get("company"))
	doc_dict["company"] = company
	party_type = doc_dict.get("party_type") or "Customer"
	payment_type = doc_dict.get("payment_type") or ("Receive" if party_type == "Customer" else "Pay")
	doc_dict["party_type"], doc_dict["payment_type"] = party_type, payment_type
	amount = flt(doc_dict.get("paid_amount") or doc_dict.get("received_amount"))
	doc_dict.setdefault("posting_date", nowdate())

	mop = doc_dict.get("mode_of_payment") or "Cash"
	doc_dict["mode_of_payment"] = mop
	bank = _mode_of_payment_account(site, mop, company)
	party_account = _party_account(site, party_type, doc_dict.get("party"), company)
	if payment_type == "Receive":
		doc_dict["paid_from"] = party_account
		doc_dict["paid_to"] = bank
	else:
		doc_dict["paid_from"] = bank
		doc_dict["paid_to"] = party_account
	for f in ("paid_from", "paid_to"):
		if doc_dict.get(f):
			cur = site.get_value("Account", {"name": doc_dict[f]}, "account_currency")
			doc_dict[f + "_account_currency"] = cur
	doc_dict.setdefault("source_exchange_rate", 1)
	doc_dict.setdefault("target_exchange_rate", 1)
	doc_dict["paid_amount"] = amount
	doc_dict["received_amount"] = amount

	# FIFO allocation against outstanding invoices unless references already given.
	if not doc_dict.get("references") and not doc_dict.get("_on_account"):
		inv_dt = "Sales Invoice" if party_type == "Customer" else "Purchase Invoice"
		party_field = "customer" if party_type == "Customer" else "supplier"
		invoices = site.get_list(
			inv_dt,
			filters=[
				[party_field, "=", doc_dict.get("party")],
				["company", "=", company],
				["docstatus", "=", 1],
				["outstanding_amount", ">", 0],
			],
			fields=["name", "grand_total", "outstanding_amount"],
			order_by="due_date asc, posting_date asc",
			limit=20)
		remaining, refs = amount, []
		for inv in invoices:
			if remaining <= 0.005:
				break
			alloc = min(remaining, flt(inv.get("outstanding_amount")))
			refs.append({
				"reference_doctype": inv_dt,
				"reference_name": inv.get("name"),
				"total_amount": inv.get("grand_total"),
				"outstanding_amount": inv.get("outstanding_amount"),
				"allocated_amount": round(alloc, 2),
			})
			remaining -= alloc
		if refs:
			doc_dict["references"] = refs
	doc_dict.pop("_on_account", None)
	return doc_dict


def _first_leaf(site, doctype):
	rows = site.get_list(doctype, filters={"is_group": 0}, fields=["name"],
	                     order_by="creation asc", limit=1)
	return rows[0]["name"] if rows else None


def _enrich(site, doctype, data):
	"""Fill what a bare insert misses; their ERPNext validation handles the rest."""
	company = _default_company(site, data.get("company"))
	if doctype == "Payment Entry":
		return _enrich_payment_entry(site, data)
	if doctype in ("Sales Invoice", "Purchase Invoice", "Quotation", "Sales Order",
	               "Purchase Order", "Delivery Note", "Purchase Receipt", "Stock Entry",
	               "Material Request"):
		data.setdefault("company", company)
	if doctype in ("Sales Invoice", "Purchase Invoice") and company:
		cc = site.get_value("Company", {"name": company}, "cost_center")
		if cc:
			data.setdefault("cost_center", cc)
			for it in data.get("items") or []:
				it.setdefault("cost_center", cc)
	if doctype == "Item":
		data.setdefault("item_group", _first_leaf(site, "Item Group") or "All Item Groups")
		data.setdefault("stock_uom", "Nos")
		if "item_code" not in data and data.get("item_name"):
			data["item_code"] = data["item_name"]
	if doctype == "Customer":
		data.setdefault("customer_group", _first_leaf(site, "Customer Group"))
		data.setdefault("territory", _first_leaf(site, "Territory"))
	if doctype == "Supplier":
		data.setdefault("supplier_group", _first_leaf(site, "Supplier Group"))
	return data


# ─── Executor ─────────────────────────────────────────────────────────────────

def execute_tool(site, name, args, ctx):
	"""Run one tool call on their site, as whoever's key we are holding.

	`site` is an erp.Site bound to the asking user's credential, so Frappe's own
	permissions still decide what comes back. Returns a string for the model.
	"""
	try:
		if name == "list_documents":
			limit = min(cint(args.get("limit") or 20), 50)
			rows = site.get_list(
				args["doctype"],
				fields=args.get("fields") or ["name"],
				filters=args.get("filters"),
				order_by=args.get("order_by") or "modified desc",
				limit=limit)
			# The warning goes FIRST: long results get tail-truncated, so a note
			# after the rows would be cut off in exactly the case that needs it.
			out = {}
			if len(rows) == limit:
				# Without this the model reports the page size as the answer — "50
				# invoices are outstanding" when there are 300. It must be told the
				# list was cut off and where the real number comes from.
				out["truncated"] = True
				out["note"] = (
					f"Cut off at the {limit}-row limit. There are MORE matches. "
					"This is NOT a count. Call aggregate for totals or counts."
				)
			out["returned"] = len(rows)
			out["data"] = rows
			return _truncate(json.dumps(out, default=str, indent=1))

		if name == "get_document":
			doc = site.get_doc(args["doctype"], args["name"])
			return _truncate(json.dumps(doc, default=str, indent=1),
			                 "use list_documents with fields for less data")

		if name == "count_documents":
			return json.dumps({"count": site.get_count(args["doctype"], args.get("filters"))})

		if name == "aggregate":
			doctype = args["doctype"]
			filters = args.get("filters")
			sums = [f for f in (args.get("sum_fields") or []) if f]
			rows = site.aggregate(doctype, filters=filters, sum_fields=sums,
			                      group_by=args.get("group_by"),
			                      limit=min(cint(args.get("limit") or 20), 50))
			if args.get("group_by"):
				return _truncate(json.dumps(
					{"group_by": args["group_by"], "data": rows}, default=str, indent=1))
			row = rows[0] if rows else {}
			totals = {f: flt(row.get(f)) for f in sums}
			return json.dumps({"count": cint(row.get("_count")), "totals": totals}, default=str)

		if name == "search_link":
			return json.dumps(site.search_link(args["doctype"], args.get("text") or ""),
			                  default=str)

		if name == "get_doctype_fields":
			meta = site.fields_of(args["doctype"])
			fields = [
				{
					"fieldname": f.get("fieldname"),
					"fieldtype": f.get("fieldtype"),
					"label": f.get("label"),
					**({"reqd": 1} if f.get("reqd") else {}),
					**({"options": f.get("options")} if f.get("options") else {}),
				}
				for f in meta["fields"]
				if f.get("fieldtype") not in ("Section Break", "Column Break", "Tab Break",
				                              "HTML", "Fold")
			]
			return _truncate(
				json.dumps(
					{
						"doctype": args["doctype"],
						"is_submittable": bool(meta["is_submittable"]),
						"required": [f["fieldname"] for f in fields if f.get("reqd")],
						"fields": fields,
					},
					indent=1,
				),
				"focus on required",
			)

		if name == "run_report":
			res = site.run_report(args["report_name"], args.get("filters") or {})
			out = {"columns": [c.get("label") for c in res.get("columns", [])],
			       "rows": (res.get("result") or [])[:20]}
			return _truncate(json.dumps(out, default=str))

		if name == "create_record":
			doctype = args["doctype"]
			allowed = ctx.get("allowed_doctypes")
			if allowed and doctype not in allowed:
				return _err(
					f"This agent may only create: {', '.join(allowed)}. "
					f"Tell the user to switch to the Operator agent to create a {doctype}."
				)
			data = _enrich(site, doctype, dict(args.get("data") or {}))
			data["doctype"] = doctype
			doc = site.insert(data)      # permission-checked there; stays docstatus 0
			docname = doc.get("name")
			link = desk_link(doctype, docname)
			draft = is_submittable(site, doctype)
			if draft:
				ctx["last_draft"] = {"doctype": doctype, "docname": docname, "link": link}
			# Always take the user to what was just created (the Desk widget executes this).
			ctx.setdefault("actions", []).append({"type": "form", "doctype": doctype, "name": docname})
			total = doc.get("grand_total") or doc.get("paid_amount") or None
			return json.dumps(
				{
					"success": True,
					"doctype": doctype,
					"name": docname,
					"link": link,
					"is_draft": draft,
					**({"grand_total": flt(total)} if total else {}),
					"note": "DRAFT created. The user must review and submit. Do NOT claim it is submitted."
					if draft
					else "Master record created (live).",
				}
			)

		if name == "update_record":
			doctype, docname = args["doctype"], args["name"]
			status = cint(site.get_value(doctype, {"name": docname}, "docstatus"))
			if status != 0:
				return _err(f"{docname} is submitted. Submitted documents can't be edited.")
			site.update(doctype, docname, args.get("data") or {})
			return json.dumps({"success": True, "name": docname,
			                   "link": desk_link(doctype, docname)})

		if name == "navigate":
			action = {
				"type": args.get("view") or "list",
				"doctype": args.get("doctype"),
				"name": args.get("name"),
				"report": args.get("report"),
				"filters": args.get("filters") or {},
			}
			ctx.setdefault("actions", []).append(action)
			return json.dumps({"success": True, "note": "Navigation queued. The Desk will open it."})

		return _err(f"unknown tool {name}")
	except erp.ErpError as e:
		# Their site's own words, already stripped of anything of ours.
		return _err(e)
	except Exception as e:
		frappe.log_error(title=f"Brain tool {name} failed", message=frappe.get_traceback())
		return _err(str(e) or "that did not work")


# ─── The same tools, in MCP's dialect ─────────────────────────────────────────

# `navigate` exists to steer a Desk we are rendering next to. An MCP client has
# no Desk to steer and no widget of ours to receive an action, so the capability
# comes back as the only form that survives the channel: a link to hand over.
OPEN_TOOL = {
	"type": "function",
	"function": {
		"name": "open_in_erpnext",
		"description": (
			"Build a link into the user's own ERPNext: a filtered list, one document, "
			"or a report. Use it whenever an answer is easier to trust with the records "
			"in front of them, and show the URL you get back. It returns a link; it "
			"does not open anything by itself."
		),
		"parameters": {
			"type": "object",
			"properties": {
				"view": {"type": "string", "enum": ["list", "form", "new", "report", "query-report", "print"]},
				"doctype": {"type": "string", "description": "DocType for list/form/new/print"},
				"name": {"type": "string", "description": "Document name (form and print views)"},
				"report": {"type": "string", "description": "Report name (query-report view)"},
				"print_format": {"type": "string", "description": "Optional print format name for the print view"},
				"filters": {
					"type": "object",
					"description": 'List filters, e.g. {"outstanding_amount": [">", 0], "status": "Overdue"}. For a relative period use Timespan: {"creation": ["Timespan", "this month"]}, never a bare <= on today. For a range use Between: {"posting_date": ["Between", ["2026-04-01", "2026-06-30"]]}.',
				},
			},
			"required": ["view"],
		},
	},
}


def _as_mcp(fn, read_only):
	"""One tool, re-dressed for a client that reads JSON Schema under inputSchema.

	The description is copied, not rewritten. A customer who reaches their site
	through Claude is asking the same questions as one who reaches it through the
	widget, and the wording that stops a model adding up a truncated list is the
	same wording either way.

	The hints are not decoration: a client shows a read-only tool differently
	from one that writes, and can run one without asking. Nothing here deletes
	or submits, so `destructiveHint` is false on the writes too, and a draft
	created twice is two drafts, so none of them is idempotent.
	"""
	return {
		"name": fn["name"],
		"description": fn["description"],
		"inputSchema": fn.get("parameters") or {"type": "object", "properties": {}},
		"annotations": {
			"title": fn["name"].replace("_", " ").title(),
			"readOnlyHint": bool(read_only),
			"destructiveHint": False,
			"idempotentHint": False,
			"openWorldHint": True,
		},
	}




# Tools the connector has and the agent does not. Not an oversight: the agent is
# handed its company, its user and today's date in a system prompt we write, and
# it answers inside a Desk that already knows what this site contains. A client
# in somebody else's product knows none of that, and the two mistakes it makes
# without being told are guessing a company name on a multi-company site and
# reading "last quarter" as a calendar quarter on a site whose year starts in
# April. Each of these closes one of those.
MCP_ONLY_TOOLS = [
	{
		"type": "function",
		"function": {
			"name": "get_context",
			"description": (
				"Who you are talking to and what this site contains: the user, their "
				"timezone, today's date, the default company and every company they can "
				"see, the open fiscal year, the currency and how it is written. CALL THIS "
				"FIRST, before any other tool, and use the company and dates it gives you "
				"instead of guessing. Takes no arguments."
			),
			"parameters": {"type": "object", "properties": {}},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "resolve_period",
			"description": (
				"Turn a period someone said out loud into the two dates this site means by "
				"it: 'last month', 'this quarter', 'last 30 days', 'this fiscal year', "
				"'last fiscal year', 'year to date'. Use it for any question with a period "
				"in it and pass the dates it returns to your filters. A financial year here "
				"usually runs April to March, so a fiscal period is NOT the calendar one "
				"and working it out yourself will be wrong."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"period": {"type": "string", "description": "e.g. last month, this quarter, last fiscal year, last 90 days, year to date"},
					"company": {"type": "string", "description": "Optional; a fiscal period can differ by company."},
				},
				"required": ["period"],
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "list_doctypes",
			"description": (
				"Find out what kinds of document this site actually keeps, including the "
				"ones added for this customer's own trade, which you cannot know from "
				"ERPNext alone. Search by word or list a module. Use it before guessing a "
				"DocType name, and before telling someone this site cannot do something."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"search": {"type": "string", "description": "Word to look for in the name, e.g. 'invoice', 'waste', 'vehicle'"},
					"module": {"type": "string", "description": "Optional module to list, e.g. Accounts, Stock"},
					"limit": {"type": "number", "description": "Max rows (default 40)"},
				},
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "list_reports",
			"description": (
				"The reports this site has and the filters each one wants, so run_report "
				"can be called with the right name and arguments instead of a guess. "
				"Search by word, e.g. 'receivable', 'stock', 'gst'."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"search": {"type": "string", "description": "Word to look for in the report name"},
					"doctype": {"type": "string", "description": "Optional: only reports on this DocType"},
					"limit": {"type": "number", "description": "Max rows (default 30)"},
				},
			},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "add_comment",
			"description": (
				"Leave a note on a document's timeline, where the next person to open it "
				"will read it: what was agreed on the phone, why a price was changed, when "
				"a customer promised to pay. It writes a comment and nothing else; no field "
				"on the document is touched."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"doctype": {"type": "string"},
					"name": {"type": "string"},
					"comment": {"type": "string"},
				},
				"required": ["doctype", "name", "comment"],
			},
		},
	},
]

# Starting points a client shows as its own menu, so the first thing someone sees
# is four jobs this does rather than an empty box and a connector they have to
# invent questions for. `arguments` are filled in by whoever picks the prompt.
MCP_PROMPTS = [
	{
		"name": "who_owes_me",
		"title": "Who owes me money",
		"description": "Outstanding receivables, oldest first, with the invoices behind the total.",
		"arguments": [
			{"name": "company", "description": "Leave empty for the default company.",
			 "required": False, "default": "my default company"},
		],
		"template": (
			"Call get_context first. Then tell me what my customers owe me at {company}: "
			"the total outstanding, how much of it is overdue, and the ten biggest debtors "
			"with their oldest unpaid invoice and how many days late it is. Use aggregate "
			"for every total, never a row count. Finish with a link to the Accounts "
			"Receivable report."
		),
	},
	{
		"name": "record_a_payment",
		"title": "Record a payment received",
		"description": "Drafts a Payment Entry against the right invoices. You submit it.",
		"arguments": [
			{"name": "details", "description": "e.g. received 1,00,000 from Sunrise Traders by cheque",
			 "required": True, "default": ""},
		],
		"template": (
			"Call get_context first. A payment came in: {details}. Find the party, check "
			"what they owe, and draft the Payment Entry allocated against their oldest "
			"outstanding invoices. Show me the allocation before you create it, then give "
			"me the link to the draft. Do not say it is submitted; you cannot submit."
		),
	},
	{
		"name": "month_end_check",
		"title": "Month-end check",
		"description": "What is still draft, unpaid or odd before the books close.",
		"arguments": [
			{"name": "period", "description": "e.g. last month, this quarter, last fiscal year",
			 "required": False, "default": "last month"},
		],
		"template": (
			"Call get_context, then resolve_period for \"{period}\". For that period tell "
			"me: how many Sales Invoices and Purchase Invoices are still draft, total sales "
			"and total purchases submitted, what was outstanding receivable and payable at "
			"the end of it, and anything that looks wrong enough to check by hand. Totals "
			"from aggregate only. Link to every list you mention."
		),
	},
	{
		"name": "customer_history",
		"title": "Everything about one customer",
		"description": "Orders, invoices, payments and what is still open, on one page.",
		"arguments": [
			{"name": "customer", "description": "Customer name, or part of it",
			 "required": True, "default": ""},
		],
		"template": (
			"Call get_context first, then search_link to find the customer matching "
			"\"{customer}\". Give me what they have bought this fiscal year, what they owe "
			"now, their oldest unpaid invoice, their last payment, and any open Sales "
			"Order. Link to their customer page at the end."
		),
	},
	{
		"name": "stock_position",
		"title": "Stock position of an item",
		"description": "What is on hand, where, and what is already committed.",
		"arguments": [
			{"name": "item", "description": "Item name or code, as a person would say it",
			 "required": True, "default": ""},
		],
		"template": (
			"Call get_context first, then search_link to resolve \"{item}\" to a real Item. "
			"Tell me the quantity on hand per warehouse with its unit, the total, how much "
			"is reserved against Sales Orders, and the reorder level if one is set. Link to "
			"the Stock Balance report filtered to that item."
		),
	},
]


def mcp_prompts():
	"""The prompt menu a connector offers.

	Served from here, like the tools, because these are the product talking: each
	one is a worked instruction for a job this does well, including the order to
	call things in. A customer who picks one gets the good version of a question
	they would otherwise ask badly.
	"""
	return [dict(p) for p in MCP_PROMPTS]



def mcp_catalogue():
	"""The tool list an MCP client is shown.

	Served from here for the same reason the agents are: what the tools are, how
	they are worded and which ones exist at all is the product, and a customer is
	Administrator on the site the connector runs on. There is no submit tool in
	this list, and more to the point there is no submit branch in the executor
	that reads it, so the list is a description of the guarantee rather than the
	thing enforcing it.
	"""
	writes = {"add_comment"}
	out = [_as_mcp(t["function"], read_only=True)
	       for t in MCP_ONLY_TOOLS if t["function"]["name"] not in writes]
	out += [_as_mcp(t["function"], read_only=True) for t in READ_TOOLS]
	out.append(_as_mcp(OPEN_TOOL["function"], read_only=True))
	out += [_as_mcp(t["function"], read_only=False) for t in WRITE_TOOLS]
	out += [_as_mcp(t["function"], read_only=False)
	        for t in MCP_ONLY_TOOLS if t["function"]["name"] in writes]
	return out
