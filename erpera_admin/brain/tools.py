# Copyright (c) 2026, Reformiqo and contributors
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
