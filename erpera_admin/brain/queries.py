# Copyright (c) 2026, The Tiny App Company and contributors
# For license information, please see license.txt

"""58 answers that need no model at all, now asked from here.

This registry is the cheapest thing the product does and the most valuable to
keep: it answers a question in under a second, for nothing, without a model
that could invent a number. It used to sit on the customer's site, which meant
the 58 definitions, their filters and their wording were theirs to read. So it
moved, and the executors underneath it now ask their site over REST instead of
querying a database in the same process.

Two things changed in the move and both are visible below. Row permissions
still apply because the request is signed with the asking user's own key, which
is the same guarantee frappe.get_list gave when this ran there. And money is
formatted with the customer's number format rather than ours, because a brain
serving many sites has no business imposing its own grouping: an Indian site
must read 14,82,650.00 whoever is doing the formatting.
"""

import calendar
from datetime import date, timedelta

import frappe
from frappe.utils import add_days, flt, fmt_money, getdate, nowdate

from erpera_admin import erp
from erpera_admin.brain import dashboard as dash


# ─── helpers ──────────────────────────────────────────────────────────────────

def _today():
	return nowdate()


def _month_start():
	d = getdate()
	return f"{d.year}-{d.month:02d}-01"


def _fy_start():
	d = getdate()
	y = d.year if d.month >= 4 else d.year - 1
	return f"{y}-04-01"


def _fy_name(site):
	"""Their fiscal year, read rather than assumed.

	erpnext.accounts.utils.get_fiscal_year is not whitelisted and should not be,
	so the year comes from the Fiscal Year record that covers today. The April
	guess stays as the fallback for a site that has none.
	"""
	try:
		today = nowdate()
		rows = site.get_list(
			"Fiscal Year",
			filters=[["year_start_date", "<=", today], ["year_end_date", ">=", today]],
			fields=["name"], limit=1)
		if rows:
			return rows[0].name
	except Exception:
		pass
	d = getdate()
	y = d.year if d.month >= 4 else d.year - 1
	return f"{y}-{y + 1}"


def _number_format():
	"""The customer's grouping, carried on the request rather than passed down.

	Every formatting call site would otherwise need an extra argument threaded
	through 58 query definitions to answer a question none of them asks. It is
	set once per request by execute() and read here.
	"""
	return getattr(frappe.local, "erpera_number_format", None)


def fmt_inr(v):
	"""Money, grouped the way this site is configured to group it.

	Python's `{:,.2f}` is always western grouping, so an Indian site read
	1,482,650.00 where it should say 14,82,650.00 -- in a function called
	fmt_inr. Defer to the site's Number Format instead of hardcoding either
	convention, so a site outside India keeps its own.
	"""
	try:
		return fmt_money(flt(v), precision=2, format=_number_format())
	except Exception:
		return f"{flt(v):,.2f}" if v is not None else "0.00"


def fmt_n(v):
	try:
		return fmt_money(int(flt(v)), precision=0, format=_number_format())
	except Exception:
		try:
			return f"{int(flt(v)):,}"
		except Exception:
			return "0"


def _with_company(filters, company):
	f = list(filters or [])
	if company:
		f.append(["company", "=", company])
	return f


def _link(doctype):
	return f"/app/{doctype.replace(' ', '-').lower()}"


def _agg(site, doctype, filters, sum_fields):
	"""(count, {field: sum}) in one request to their site."""
	try:
		rows = site.aggregate(doctype, filters=filters, sum_fields=sum_fields)
		row = rows[0] if rows else {}
		return int(row.get("_count") or 0), {f: flt(row.get(f)) for f in (sum_fields or [])}
	except Exception:
		return 0, {f: 0.0 for f in sum_fields}


# ─── generic executors ────────────────────────────────────────────────────────

def _run_list(site, q, company, extra_filters=None, order_by=None):
	filters = _with_company(q.get("filters", []), company if q.get("company_filter", True) else None)
	if extra_filters:
		filters += list(extra_filters)
	doctype = q["doctype"]
	rows = site.get_list(
		doctype,
		fields=[f[0] for f in q["cols"]],
		filters=filters,
		order_by=order_by or q.get("order_by") or "modified desc",
		limit=q.get("limit", 20),
	)
	sums = q.get("sums", [])
	count, totals = _agg(site, doctype, filters, sums)
	count = count or len(rows)
	columns = [c[1] for c in q["cols"]]
	out_rows = []
	for r in rows:
		row = {}
		for fname, label, *kind in q["cols"]:
			v = r.get(fname.split(" as ")[-1] if " as " in fname else fname)
			row[label] = fmt_inr(v) if (kind and kind[0] == "money") else (v if v is not None else "")
		out_rows.append(row)
	kpis = q["kpis"](count, totals) if callable(q.get("kpis")) else _default_kpis(q, count, totals)
	summary = q["summary"](count, totals) if callable(q.get("summary")) else None
	return {
		"title": q["label"],
		"summary": summary or f"{fmt_n(count)} records." + (f" Total ₹{fmt_inr(totals[sums[0]])}." if sums else ""),
		"kpis": kpis,
		"columns": columns,
		"rows": out_rows,
		"total_count": count,
		"link": _link(doctype),
		"followUps": q.get("follow_ups", []),
	}


def _default_kpis(q, count, totals):
	kpis = [{"label": "Count", "value": count}]
	for f, label in (q.get("kpi_sums") or []):
		kpis.append({"label": label, "value": f"₹{fmt_inr(totals.get(f))}"})
	return kpis


def _run_group(site, q, company, extra_filters=None, order_by=None):
	filters = _with_company(q.get("filters", []), company if q.get("company_filter", True) else None)
	if extra_filters:
		filters += list(extra_filters)
	gf, af = q["group_field"], q.get("agg_field")
	rows = site.group(q["doctype"], filters, gf, af, limit=q.get("limit", 20))
	grand = sum(flt(r.get("total")) for r in rows)
	columns = [q.get("group_label", "Name"), "Count"] + (["Total"] if af else [])
	out = []
	for r in rows:
		row = {q.get("group_label", "Name"): r.get("label") or "-", "Count": r.get("_count")}
		if af:
			row["Total"] = fmt_inr(r.get("total"))
		out.append(row)
	kpis = [{"label": "Groups", "value": len(rows)}]
	if af:
		kpis.append({"label": "Grand Total", "value": f"₹{fmt_inr(grand)}"})
	return {
		"title": q["label"],
		"summary": f"{len(rows)} groups" + (f", grand total ₹{fmt_inr(grand)}." if af else "."),
		"kpis": kpis,
		"columns": columns,
		"rows": out,
		"total_count": len(rows),
		"link": _link(q["doctype"]),
		"followUps": q.get("follow_ups", []),
	}


def _run_report(site, q, company, extra_filters=None, order_by=None):
	filters = dict(q.get("report_filters") or {})
	if callable(q.get("report_filters_fn")):
		filters.update(q["report_filters_fn"](company, site))
	elif company:
		filters.setdefault("company", company)
	res = site.run_report(q["report"], filters=filters)
	cols = res.get("columns") or []
	labels = [c.get("label") or c.get("fieldname") for c in cols][: q.get("max_cols", 8)]
	keys = [c.get("fieldname") for c in cols][: q.get("max_cols", 8)]
	raw = [r for r in (res.get("result") or []) if isinstance(r, dict)][:20]
	rows = [{labels[i]: (r.get(keys[i]) if r.get(keys[i]) is not None else "") for i in range(len(labels))} for r in raw]
	return {
		"title": q["label"],
		"summary": f"{q['report']}, first {len(rows)} rows.",
		"kpis": [],
		"columns": labels,
		"rows": rows,
		"total_count": len(res.get("result") or []),
		"link": f"/app/query-report/{q['report'].replace(' ', '%20')}",
		"followUps": q.get("follow_ups", []),
	}


# ─── the registry ─────────────────────────────────────────────────────────────

QUERIES = {}


def _q(qid, label, department, kind="list", **kw):
	QUERIES[qid] = {"id": qid, "label": label, "department": department, "kind": kind, **kw}


# — dashboard —
_q("company_overview", "Business Overview", "dashboard", kind="special")
_q("today_summary", "Today's Activity", "dashboard", kind="special")

# — accounts —
_q(
	"outstanding_receivables", "Outstanding Receivables", "accounts",
	doctype="Sales Invoice",
	filters=[["outstanding_amount", ">", 0], ["docstatus", "=", 1]],
	cols=[("name", "Invoice"), ("customer_name", "Customer"), ("posting_date", "Date"),
		("due_date", "Due Date"), ("grand_total", "Total", "money"),
		("outstanding_amount", "Outstanding", "money"), ("status", "Status")],
	sums=["outstanding_amount"], kpi_sums=[("outstanding_amount", "Total Outstanding")],
	order_by="outstanding_amount desc",
	follow_ups=["recent_payments", "top_customers", "outstanding_payables"],
)
_q(
	"outstanding_payables", "Outstanding Payables", "accounts",
	doctype="Purchase Invoice",
	filters=[["outstanding_amount", ">", 0], ["docstatus", "=", 1]],
	cols=[("name", "Invoice"), ("supplier_name", "Supplier"), ("posting_date", "Date"),
		("due_date", "Due Date"), ("grand_total", "Total", "money"),
		("outstanding_amount", "Outstanding", "money"), ("status", "Status")],
	sums=["outstanding_amount"], kpi_sums=[("outstanding_amount", "Total Payable")],
	order_by="outstanding_amount desc",
	follow_ups=["supplier_payments_due", "outstanding_receivables"],
)
_q(
	"recent_payments", "Recent Payments", "accounts",
	doctype="Payment Entry",
	filters=[["docstatus", "=", 1]],
	cols=[("name", "Payment"), ("payment_type", "Type"), ("party_name", "Party"),
		("paid_amount", "Amount", "money"), ("mode_of_payment", "Mode"), ("posting_date", "Date")],
	sums=["paid_amount"], kpi_sums=[("paid_amount", "Total")],
	order_by="posting_date desc",
	follow_ups=["outstanding_receivables", "bank_balance"],
)
_q("profit_and_loss", "Profit & Loss (This FY)", "accounts", kind="report",
	report="Profit and Loss Statement",
	report_filters_fn=lambda c, site: {
		"company": c or site.single_value("Global Defaults", "default_company"),
		"filter_based_on": "Fiscal Year", "from_fiscal_year": _fy_name(site), "to_fiscal_year": _fy_name(site),
		"periodicity": "Yearly", "cost_center": [], "project": [],
	})
_q("trial_balance", "Trial Balance", "accounts", kind="report",
	report="Trial Balance",
	report_filters_fn=lambda c, site: {
		"company": c or site.single_value("Global Defaults", "default_company"),
		"fiscal_year": _fy_name(site), "from_date": _fy_start(), "to_date": _today(),
		"with_period_closing_entry_for_opening": 1, "with_period_closing_entry_for_current_period": 1,
	})
_q("bank_balance", "Bank & Cash Balance", "accounts", kind="special")
_q(
	"overdue_receivables", "Overdue Invoices Only", "accounts",
	doctype="Sales Invoice",
	filters=[["outstanding_amount", ">", 0], ["docstatus", "=", 1], ["due_date", "<", _today()]],
	cols=[("name", "Invoice"), ("customer_name", "Customer"), ("due_date", "Due Date"),
		("outstanding_amount", "Outstanding", "money"), ("status", "Status")],
	sums=["outstanding_amount"], kpi_sums=[("outstanding_amount", "Overdue Amount")],
	order_by="due_date asc",
	follow_ups=["outstanding_receivables", "recent_payments"],
)
_q("monthly_revenue", "Monthly Revenue Trend", "accounts", kind="special")
_q("receivable_aging_report", "Receivable Aging", "accounts", kind="report",
	report="Accounts Receivable Summary",
	report_filters_fn=lambda c, site: {
		"company": c or site.single_value("Global Defaults", "default_company"),
		"report_date": _today(), "ageing_based_on": "Due Date",
		"range": "30, 60, 90, 120", "party_type": "Customer",
	})
_q("payable_aging_report", "Payable Aging", "accounts", kind="report",
	report="Accounts Payable Summary",
	report_filters_fn=lambda c, site: {
		"company": c or site.single_value("Global Defaults", "default_company"),
		"report_date": _today(), "ageing_based_on": "Due Date",
		"range": "30, 60, 90, 120", "party_type": "Supplier",
	})
_q(
	"gl_entries", "General Ledger", "accounts",
	doctype="GL Entry",
	filters=[["is_cancelled", "=", 0]],
	cols=[("posting_date", "Date"), ("account", "Account"), ("debit", "Debit", "money"),
		("credit", "Credit", "money"), ("voucher_type", "Voucher"), ("voucher_no", "Voucher No")],
	order_by="posting_date desc, creation desc",
)
_q(
	"journal_entries", "Journal Entries", "accounts",
	doctype="Journal Entry",
	filters=[["docstatus", "=", 1]],
	cols=[("name", "Entry"), ("voucher_type", "Type"), ("posting_date", "Date"),
		("total_debit", "Debit", "money"), ("user_remark", "Remark")],
	sums=["total_debit"], kpi_sums=[("total_debit", "Total")],
	order_by="posting_date desc",
)
_q("tax_summary", "Tax Summary", "accounts", kind="special")
_q(
	"expense_claims", "Expense Claims", "accounts",
	doctype="Expense Claim",
	filters=[],
	cols=[("name", "Claim"), ("employee_name", "Employee"), ("posting_date", "Date"),
		("total_claimed_amount", "Claimed", "money"), ("status", "Status"), ("approval_status", "Approval")],
	sums=["total_claimed_amount"], kpi_sums=[("total_claimed_amount", "Total Claimed")],
	order_by="posting_date desc",
)

# — sales —
# — master data: people ask "how many customers do we have" constantly, and
# until these existed the matcher had nowhere sensible to send them.
_q(
	"customer_list", "Customers", "sales",
	doctype="Customer",
	filters=[["disabled", "=", 0]],
	cols=[("name", "Customer"), ("customer_group", "Group"), ("territory", "Territory"),
		("customer_type", "Type"), ("mobile_no", "Mobile")],
	order_by="modified desc",
	follow_ups=["top_customers", "outstanding_receivables"],
	company_filter=False,
)
_q(
	"supplier_list", "Suppliers", "purchase",
	doctype="Supplier",
	filters=[["disabled", "=", 0]],
	cols=[("name", "Supplier"), ("supplier_group", "Group"), ("supplier_type", "Type"),
		("mobile_no", "Mobile")],
	order_by="modified desc",
	follow_ups=["top_suppliers", "outstanding_payables"],
	company_filter=False,
)
_q(
	"item_list", "Items", "inventory",
	doctype="Item",
	filters=[["disabled", "=", 0]],
	cols=[("item_code", "Item"), ("item_name", "Name"), ("item_group", "Group"),
		("stock_uom", "UOM")],
	order_by="modified desc",
	follow_ups=["stock_balance", "top_selling_items"],
	company_filter=False,
)
_q(
	"open_sales_orders", "Open Sales Orders", "sales",
	doctype="Sales Order",
	filters=[["docstatus", "=", 1], ["status", "in", ["To Deliver and Bill", "To Bill", "To Deliver"]]],
	cols=[("name", "Order"), ("customer_name", "Customer"), ("transaction_date", "Date"),
		("grand_total", "Total", "money"), ("status", "Status"),
		("per_delivered", "Delivered %"), ("per_billed", "Billed %")],
	sums=["grand_total"], kpi_sums=[("grand_total", "Open Value")],
	order_by="transaction_date desc",
	follow_ups=["pending_deliveries", "sales_this_month"],
)
_q(
	"quotation_pipeline", "Quotation Pipeline", "sales",
	doctype="Quotation",
	filters=[["docstatus", "=", 1], ["status", "=", "Open"]],
	cols=[("name", "Quotation"), ("customer_name", "Customer"), ("transaction_date", "Date"),
		("valid_till", "Valid Till"), ("grand_total", "Value", "money")],
	sums=["grand_total"], kpi_sums=[("grand_total", "Pipeline Value")],
	order_by="grand_total desc",
	follow_ups=["quotation_conversion_rate", "open_sales_orders"],
)
_q("top_customers", "Top Customers (Month)", "sales", kind="group",
	doctype="Sales Invoice", group_field="customer", group_label="Customer",
	agg_field="grand_total",
	filters=[["docstatus", "=", 1], ["posting_date", ">=", _month_start()]],
	limit=10, follow_ups=["sales_this_month", "outstanding_receivables"])
_q(
	"pending_deliveries", "Pending Deliveries", "sales",
	doctype="Sales Order",
	filters=[["docstatus", "=", 1], ["per_delivered", "<", 100],
		["status", "not in", ["Closed", "Completed", "Cancelled"]]],
	cols=[("name", "Order"), ("customer_name", "Customer"), ("delivery_date", "Delivery Date"),
		("grand_total", "Total", "money"), ("per_delivered", "Delivered %")],
	sums=["grand_total"], kpi_sums=[("grand_total", "Value Pending")],
	order_by="delivery_date asc",
)
_q(
	"sales_this_month", "Sales This Month", "sales",
	doctype="Sales Invoice",
	filters=[["docstatus", "=", 1], ["posting_date", ">=", _month_start()]],
	cols=[("name", "Invoice"), ("customer_name", "Customer"), ("posting_date", "Date"),
		("grand_total", "Total", "money"), ("status", "Status")],
	sums=["grand_total"], kpi_sums=[("grand_total", "Month Revenue")],
	order_by="posting_date desc",
	follow_ups=["top_customers", "top_selling_items", "monthly_revenue"],
)
_q("top_selling_items", "Top Selling Items", "sales", kind="group",
	doctype="Sales Invoice Item", group_field="item_code", group_label="Item",
	agg_field="amount", company_filter=False,
	filters=[["docstatus", "=", 1], ["creation", ">=", _month_start()]],
	limit=10, follow_ups=["stock_balance", "sales_this_month"])
_q("sales_by_territory", "Sales by Territory", "sales", kind="group",
	doctype="Sales Invoice", group_field="territory", group_label="Territory",
	agg_field="grand_total",
	filters=[["docstatus", "=", 1], ["posting_date", ">=", _fy_start()]])
_q("sales_by_item_group", "Sales by Item Group", "sales", kind="group",
	doctype="Sales Invoice Item", group_field="item_group", group_label="Item Group",
	agg_field="amount", company_filter=False,
	filters=[["docstatus", "=", 1], ["creation", ">=", _fy_start()]])
_q(
	"delivery_notes", "Delivery Notes", "sales",
	doctype="Delivery Note",
	filters=[["docstatus", "=", 1]],
	cols=[("name", "Note"), ("customer_name", "Customer"), ("posting_date", "Date"),
		("grand_total", "Value", "money"), ("status", "Status")],
	order_by="posting_date desc",
)
_q(
	"credit_notes", "Credit Notes", "sales",
	doctype="Sales Invoice",
	filters=[["docstatus", "=", 1], ["is_return", "=", 1]],
	cols=[("name", "Credit Note"), ("customer_name", "Customer"), ("posting_date", "Date"),
		("grand_total", "Amount", "money"), ("return_against", "Against")],
	sums=["grand_total"], kpi_sums=[("grand_total", "Total Returns")],
	order_by="posting_date desc",
)
_q("quotation_conversion_rate", "Quotation Win Rate", "sales", kind="special")

# — purchase —
_q(
	"pending_purchase_orders", "Pending Purchase Orders", "purchase",
	doctype="Purchase Order",
	filters=[["docstatus", "=", 1], ["per_received", "<", 100],
		["status", "not in", ["Closed", "Completed", "Cancelled"]]],
	cols=[("name", "PO"), ("supplier_name", "Supplier"), ("transaction_date", "Date"),
		("grand_total", "Total", "money"), ("per_received", "Received %"), ("status", "Status")],
	sums=["grand_total"], kpi_sums=[("grand_total", "Open Value")],
	order_by="transaction_date desc",
	follow_ups=["purchase_receipts", "outstanding_payables"],
)
_q(
	"supplier_payments_due", "Supplier Payments Due", "purchase",
	doctype="Purchase Invoice",
	filters=[["docstatus", "=", 1], ["outstanding_amount", ">", 0]],
	cols=[("name", "Invoice"), ("supplier_name", "Supplier"), ("due_date", "Due Date"),
		("outstanding_amount", "Due", "money"), ("status", "Status")],
	sums=["outstanding_amount"], kpi_sums=[("outstanding_amount", "Total Due")],
	order_by="due_date asc",
	follow_ups=["outstanding_payables", "recent_payments"],
)
_q("top_suppliers", "Top Suppliers (Month)", "purchase", kind="group",
	doctype="Purchase Invoice", group_field="supplier", group_label="Supplier",
	agg_field="grand_total",
	filters=[["docstatus", "=", 1], ["posting_date", ">=", _month_start()]],
	limit=10)
_q(
	"purchase_this_month", "Purchases This Month", "purchase",
	doctype="Purchase Invoice",
	filters=[["docstatus", "=", 1], ["posting_date", ">=", _month_start()]],
	cols=[("name", "Invoice"), ("supplier_name", "Supplier"), ("posting_date", "Date"),
		("grand_total", "Total", "money"), ("status", "Status")],
	sums=["grand_total"], kpi_sums=[("grand_total", "Month Spend")],
	order_by="posting_date desc",
)
_q(
	"purchase_receipts", "Purchase Receipts", "purchase",
	doctype="Purchase Receipt",
	filters=[["docstatus", "=", 1]],
	cols=[("name", "Receipt"), ("supplier_name", "Supplier"), ("posting_date", "Date"),
		("grand_total", "Value", "money"), ("status", "Status")],
	order_by="posting_date desc",
)
_q(
	"debit_notes", "Debit Notes", "purchase",
	doctype="Purchase Invoice",
	filters=[["docstatus", "=", 1], ["is_return", "=", 1]],
	cols=[("name", "Debit Note"), ("supplier_name", "Supplier"), ("posting_date", "Date"),
		("grand_total", "Amount", "money"), ("return_against", "Against")],
	order_by="posting_date desc",
)
_q(
	"material_requests", "Material Requests", "purchase",
	doctype="Material Request",
	filters=[["docstatus", "=", 1], ["status", "in", ["Pending", "Partially Ordered"]]],
	cols=[("name", "Request"), ("material_request_type", "Purpose"), ("transaction_date", "Date"),
		("schedule_date", "Required By"), ("status", "Status")],
	order_by="schedule_date asc",
)

# — inventory —
_q("stock_balance", "Stock Balance", "inventory", kind="special")
_q("low_stock_items", "Low Stock Alert", "inventory", kind="special")
_q(
	"recent_stock_movement", "Recent Stock Entries", "inventory",
	doctype="Stock Entry",
	filters=[["docstatus", "=", 1]],
	cols=[("name", "Entry"), ("stock_entry_type", "Type"), ("posting_date", "Date"),
		("from_warehouse", "From"), ("to_warehouse", "To")],
	order_by="posting_date desc",
)
_q("stock_valuation", "Stock Valuation", "inventory", kind="special")
_q("warehouse_summary", "Warehouse Summary", "inventory", kind="group",
	doctype="Bin", group_field="warehouse", group_label="Warehouse",
	agg_field="stock_value", company_filter=False, filters=[["actual_qty", "!=", 0]])
_q(
	"item_price_list", "Item Prices", "inventory",
	doctype="Item Price", company_filter=False,
	filters=[],
	cols=[("item_code", "Item"), ("price_list", "Price List"),
		("price_list_rate", "Rate", "money"), ("currency", "Currency"), ("valid_from", "Valid From")],
	order_by="modified desc",
)
_q(
	"stock_ledger", "Stock Ledger", "inventory",
	doctype="Stock Ledger Entry",
	filters=[["is_cancelled", "=", 0]],
	cols=[("posting_date", "Date"), ("item_code", "Item"), ("warehouse", "Warehouse"),
		("actual_qty", "Qty Change"), ("qty_after_transaction", "Balance"),
		("voucher_type", "Voucher"), ("voucher_no", "Voucher No")],
	order_by="posting_date desc, creation desc",
)

# — hr —
_q(
	"employee_directory", "Employee Directory", "hr",
	doctype="Employee",
	filters=[["status", "=", "Active"]],
	cols=[("employee_name", "Name"), ("designation", "Designation"), ("department", "Department"),
		("cell_number", "Phone"), ("company_email", "Email")],
	order_by="employee_name asc", limit=50,
)
_q(
	"attendance_today", "Today's Attendance", "hr",
	doctype="Attendance",
	filters=[["attendance_date", "=", _today()], ["docstatus", "=", 1]],
	cols=[("employee_name", "Employee"), ("status", "Status"), ("in_time", "In"), ("out_time", "Out")],
	order_by="employee_name asc", limit=50,
)
_q(
	"pending_leaves", "Pending Leave Requests", "hr",
	doctype="Leave Application",
	filters=[["status", "=", "Open"]],
	cols=[("employee_name", "Employee"), ("leave_type", "Type"), ("from_date", "From"),
		("to_date", "To"), ("total_leave_days", "Days")],
	order_by="from_date asc",
)
_q("birthdays_this_month", "Birthdays This Month", "hr", kind="special")
_q("department_headcount", "Dept Headcount", "hr", kind="group",
	doctype="Employee", group_field="department", group_label="Department",
	filters=[["status", "=", "Active"]])
_q(
	"leave_balance", "Leave Balance", "hr",
	doctype="Leave Allocation",
	filters=[["docstatus", "=", 1], ["to_date", ">=", _today()]],
	cols=[("employee_name", "Employee"), ("leave_type", "Type"),
		("total_leaves_allocated", "Allocated"), ("to_date", "Valid Till")],
	order_by="employee_name asc", limit=50,
)
_q("salary_register", "Salary Register", "hr", kind="group",
	doctype="Salary Slip", group_field="department", group_label="Department",
	agg_field="net_pay",
	filters=[["docstatus", "=", 1], ["start_date", ">=", _month_start()]])

# — manufacturing —
_q(
	"open_work_orders", "Open Work Orders", "manufacturing",
	doctype="Work Order",
	filters=[["status", "in", ["Not Started", "In Process"]]],
	cols=[("name", "Work Order"), ("production_item", "Item"), ("qty", "Planned Qty"),
		("produced_qty", "Produced"), ("status", "Status"), ("planned_end_date", "Planned End")],
	order_by="planned_end_date asc",
)
_q("production_progress", "Production Progress", "manufacturing", kind="special")
_q(
	"overdue_work_orders", "Overdue Work Orders", "manufacturing",
	doctype="Work Order",
	filters=[["status", "in", ["Not Started", "In Process"]], ["planned_end_date", "<", _today()]],
	cols=[("name", "Work Order"), ("production_item", "Item"), ("qty", "Qty"),
		("produced_qty", "Produced"), ("planned_end_date", "Was Due")],
	order_by="planned_end_date asc",
)
_q("quality_summary", "Quality Summary", "manufacturing", kind="group",
	doctype="Quality Inspection", group_field="status", group_label="Status",
	filters=[["docstatus", "=", 1]], company_filter=False)
_q(
	"bom_cost_analysis", "BOM Cost Analysis", "manufacturing",
	doctype="BOM",
	filters=[["is_active", "=", 1], ["is_default", "=", 1]],
	cols=[("name", "BOM"), ("item", "Item"), ("quantity", "Qty"),
		("total_cost", "Total Cost", "money"), ("currency", "Currency")],
	sums=["total_cost"], order_by="total_cost desc",
)

# — projects —
_q(
	"active_projects", "Active Projects", "projects",
	doctype="Project",
	filters=[["status", "=", "Open"]],
	cols=[("name", "Project"), ("project_name", "Name"), ("percent_complete", "Progress %"),
		("expected_end_date", "Deadline"), ("priority", "Priority")],
	order_by="expected_end_date asc",
)
_q(
	"project_tasks", "Open Tasks", "projects",
	doctype="Task", company_filter=False,
	filters=[["status", "in", ["Open", "Working", "Pending Review"]]],
	cols=[("name", "Task"), ("subject", "Subject"), ("project", "Project"),
		("priority", "Priority"), ("exp_end_date", "Due"), ("status", "Status")],
	order_by="exp_end_date asc",
)


# ─── specials ─────────────────────────────────────────────────────────────────

def _sp_company_overview(site, company):
	f = [["docstatus", "=", 1]]
	mtd = [["docstatus", "=", 1], ["posting_date", ">=", _month_start()]]
	_, s_mtd = _agg(site, "Sales Invoice", _with_company(mtd, company), ["grand_total"])
	_, p_mtd = _agg(site, "Purchase Invoice", _with_company(mtd, company), ["grand_total"])
	_, ar = _agg(site, "Sales Invoice", _with_company(f + [["outstanding_amount", ">", 0]], company), ["outstanding_amount"])
	_, ap = _agg(site, "Purchase Invoice", _with_company(f + [["outstanding_amount", ">", 0]], company), ["outstanding_amount"])
	customers = site.get_count("Customer", {"disabled": 0})
	items = site.get_count("Item", {"disabled": 0})
	rows = [
		{"Metric": "Sales (this month)", "Value": f"₹{fmt_inr(s_mtd['grand_total'])}"},
		{"Metric": "Purchases (this month)", "Value": f"₹{fmt_inr(p_mtd['grand_total'])}"},
		{"Metric": "Receivables outstanding", "Value": f"₹{fmt_inr(ar['outstanding_amount'])}"},
		{"Metric": "Payables outstanding", "Value": f"₹{fmt_inr(ap['outstanding_amount'])}"},
		{"Metric": "Active customers", "Value": fmt_n(customers)},
		{"Metric": "Active items", "Value": fmt_n(items)},
	]
	return {
		"title": "Business Overview",
		"summary": f"Sales this month ₹{fmt_inr(s_mtd['grand_total'])} · receivables ₹{fmt_inr(ar['outstanding_amount'])}.",
		"kpis": [
			{"label": "Sales MTD", "value": f"₹{fmt_inr(s_mtd['grand_total'])}"},
			{"label": "Receivables", "value": f"₹{fmt_inr(ar['outstanding_amount'])}", "color": "#f59e0b"},
			{"label": "Payables", "value": f"₹{fmt_inr(ap['outstanding_amount'])}", "color": "#ef4444"},
		],
		"columns": ["Metric", "Value"], "rows": rows, "total_count": len(rows),
		"link": "/app", "followUps": ["sales_this_month", "outstanding_receivables", "stock_balance"],
	}


def _sp_today_summary(site, company):
	d = dash.today_summary(site, company)
	rows = [
		{"Activity": "Sales invoices", "Count": d["sales_invoices"]["count"], "Value": f"₹{fmt_inr(d['sales_invoices']['total'])}"},
		{"Activity": "Payments received", "Count": d["payments_received"]["count"], "Value": f"₹{fmt_inr(d['payments_received']['total'])}"},
		{"Activity": "Purchase orders", "Count": d["purchase_orders"]["count"], "Value": f"₹{fmt_inr(d['purchase_orders']['total'])}"},
		{"Activity": "Stock entries", "Count": d["stock_entries"]["count"], "Value": ""},
		{"Activity": "New leads", "Count": d["new_leads"]["count"], "Value": ""},
	]
	return {
		"title": "Today's Activity",
		"summary": f"{d['sales_invoices']['count']} invoices, {d['payments_received']['count']} payments today.",
		"kpis": [
			{"label": "Sales Today", "value": f"₹{fmt_inr(d['sales_invoices']['total'])}"},
			{"label": "Collected", "value": f"₹{fmt_inr(d['payments_received']['total'])}", "color": "#17a34a"},
		],
		"columns": ["Activity", "Count", "Value"], "rows": rows, "total_count": len(rows),
		"link": "/app", "followUps": ["company_overview", "sales_this_month"],
	}


def _sp_bank_balance(site, company):
	company = company or site.single_value("Global Defaults", "default_company")
	accounts = site.get_list(
		"Account",
		filters={"account_type": ["in", ["Bank", "Cash"]], "is_group": 0, "company": company, "disabled": 0},
		fields=["name", "account_type"],
		limit=25,
	)
	# erpnext.accounts.utils.get_balance_on is not whitelisted and must not be,
	# so the balance is the ledger itself: debits less credits, every account in
	# one grouped request rather than one request per account.
	names = [a.name for a in accounts]
	ledger = {}
	if names:
		try:
			for r in site.aggregate(
				"GL Entry",
				filters=[["account", "in", names], ["is_cancelled", "=", 0]],
				sum_fields=["debit", "credit"], group_by="account", limit=50):
				ledger[r.get("account")] = flt(r.get("debit")) - flt(r.get("credit"))
		except Exception:
			ledger = {}
	rows, total = [], 0.0
	for a in accounts:
		bal = flt(ledger.get(a.name))
		total += bal
		rows.append({"Account": a.name, "Type": a.account_type, "Balance": fmt_inr(bal)})
	rows.sort(key=lambda r: r["Account"])
	return {
		"title": "Bank & Cash Balance",
		"summary": f"Total across {len(rows)} accounts: ₹{fmt_inr(total)}.",
		"kpis": [{"label": "Total Balance", "value": f"₹{fmt_inr(total)}", "color": "#17a34a"}],
		"columns": ["Account", "Type", "Balance"], "rows": rows, "total_count": len(rows),
		"link": "/app/account", "followUps": ["recent_payments", "monthly_revenue"],
	}


def _sp_monthly_revenue(site, company):
	rows = []
	d = getdate().replace(day=1)
	for _ in range(6):
		start = d.strftime("%Y-%m-01")
		end_day = calendar.monthrange(d.year, d.month)[1]
		end = d.strftime(f"%Y-%m-{end_day:02d}")
		filters = _with_company(
			[["docstatus", "=", 1], ["posting_date", ">=", start], ["posting_date", "<=", end]], company
		)
		c, s = _agg(site, "Sales Invoice", filters, ["grand_total"])
		rows.append({"Month": d.strftime("%b %Y"), "Invoices": c, "Revenue": fmt_inr(s["grand_total"])})
		d = (d - timedelta(days=1)).replace(day=1)
	rows.reverse()
	return {
		"title": "Monthly Revenue Trend",
		"summary": "Sales invoice revenue, last 6 months.",
		"kpis": [{"label": "This Month", "value": f"₹{rows[-1]['Revenue']}"}],
		"columns": ["Month", "Invoices", "Revenue"], "rows": rows, "total_count": len(rows),
		"link": _link("Sales Invoice"), "followUps": ["sales_this_month", "top_customers"],
	}


def _sp_tax_summary(site, company):
	fy = _fy_start()
	out = site.group("Sales Taxes and Charges", [["docstatus", "=", 1], ["creation", ">=", fy]],
		"account_head", "base_tax_amount", limit=10)
	inp = site.group("Purchase Taxes and Charges",
		[["docstatus", "=", 1], ["creation", ">=", fy], ["category", "!=", "Valuation"]],
		"account_head", "base_tax_amount", limit=10)
	rows = [{"Direction": "Collected (sales)", "Account": r.label, "Amount": fmt_inr(r.total)} for r in out]
	rows += [{"Direction": "Paid (purchase)", "Account": r.label, "Amount": fmt_inr(r.total)} for r in inp]
	tc, tp = sum(flt(r.total) for r in out), sum(flt(r.total) for r in inp)
	return {
		"title": "Tax Summary (FY)",
		"summary": f"Collected ₹{fmt_inr(tc)} · paid ₹{fmt_inr(tp)} · net ₹{fmt_inr(tc - tp)}.",
		"kpis": [
			{"label": "Tax Collected", "value": f"₹{fmt_inr(tc)}"},
			{"label": "Tax Paid", "value": f"₹{fmt_inr(tp)}"},
			{"label": "Net", "value": f"₹{fmt_inr(tc - tp)}", "color": "#0070f3"},
		],
		"columns": ["Direction", "Account", "Amount"], "rows": rows, "total_count": len(rows),
		"link": "/app/query-report/Sales%20Register", "followUps": ["profit_and_loss"],
	}


def _sp_quotation_conversion(site, company):
	fy = _fy_start()
	base = _with_company([["docstatus", "=", 1], ["transaction_date", ">=", fy]], company)
	total = site.get_count("Quotation", base)
	won = site.get_count("Quotation", base + [["status", "=", "Ordered"]])
	rate = (won / total * 100) if total else 0
	return {
		"title": "Quotation Win Rate",
		"summary": f"{won} of {total} quotations converted this FY ({rate:.1f}%).",
		"kpis": [
			{"label": "Quotations", "value": total},
			{"label": "Won", "value": won, "color": "#17a34a"},
			{"label": "Win Rate", "value": f"{rate:.1f}%", "color": "#0070f3"},
		],
		"columns": ["Metric", "Value"],
		"rows": [
			{"Metric": "Total quotations (FY)", "Value": total},
			{"Metric": "Converted to orders", "Value": won},
			{"Metric": "Win rate", "Value": f"{rate:.1f}%"},
		],
		"total_count": 3, "link": _link("Quotation"), "followUps": ["quotation_pipeline"],
	}


def _bin_rows(site, filters, order_by, limit=20):
	return site.get_list(
		"Bin",
		filters=filters,
		fields=["item_code", "warehouse", "actual_qty", "reserved_qty", "valuation_rate", "stock_value"],
		order_by=order_by,
		limit=limit,
	)


def _sp_stock_balance(site, company):
	rows = _bin_rows(site, [["actual_qty", "!=", 0]], "actual_qty desc")
	_, _sv = _agg(site, "Bin", [], ["stock_value"])
	tv = _sv["stock_value"]
	return {
		"title": "Stock Balance",
		"summary": f"Top items by quantity. Total stock value ₹{fmt_inr(tv)}.",
		"kpis": [{"label": "Stock Value", "value": f"₹{fmt_inr(tv)}"}],
		"columns": ["Item", "Warehouse", "Qty", "Reserved", "Value"],
		"rows": [
			{"Item": r.item_code, "Warehouse": r.warehouse, "Qty": r.actual_qty,
				"Reserved": r.reserved_qty, "Value": fmt_inr(r.stock_value)}
			for r in rows
		],
		"total_count": len(rows), "link": "/app/query-report/Stock%20Balance",
		"followUps": ["low_stock_items", "stock_valuation"],
	}


def _sp_low_stock(site, company):
	rows = site.get_list(
		"Bin",
		filters=[["actual_qty", "<=", 0]],
		fields=["item_code", "warehouse", "actual_qty", "reserved_qty"],
		order_by="actual_qty asc", limit=20,
	)
	reorder = site.get_list(
		"Item Reorder", parent="Item",
		fields=["parent as item_code", "warehouse", "warehouse_reorder_level"], limit=100
	)
	rl = {(r.item_code, r.warehouse): r.warehouse_reorder_level for r in reorder}
	out = [
		{"Item": r.item_code, "Warehouse": r.warehouse, "Qty": r.actual_qty,
			"Reorder Level": rl.get((r.item_code, r.warehouse), "")}
		for r in rows
	]
	return {
		"title": "Low Stock Alert",
		"summary": f"{len(out)} item/warehouse rows at or below zero.",
		"kpis": [{"label": "Low/Out of Stock", "value": len(out), "color": "#ef4444"}],
		"columns": ["Item", "Warehouse", "Qty", "Reorder Level"], "rows": out,
		"total_count": len(out), "link": _link("Bin"), "followUps": ["stock_balance", "material_requests"],
	}


def _sp_stock_valuation(site, company):
	rows = _bin_rows(site, [["stock_value", ">", 0]], "stock_value desc")
	tv = sum(flt(r.stock_value) for r in rows)
	return {
		"title": "Stock Valuation",
		"summary": f"Top {len(rows)} items by stock value.",
		"kpis": [{"label": "Shown Value", "value": f"₹{fmt_inr(tv)}"}],
		"columns": ["Item", "Warehouse", "Qty", "Rate", "Value"],
		"rows": [
			{"Item": r.item_code, "Warehouse": r.warehouse, "Qty": r.actual_qty,
				"Rate": fmt_inr(r.valuation_rate), "Value": fmt_inr(r.stock_value)}
			for r in rows
		],
		"total_count": len(rows), "link": "/app/query-report/Stock%20Balance",
		"followUps": ["stock_balance", "warehouse_summary"],
	}


def _sp_birthdays(site, company):
	m = getdate().month
	rows = site.get_list(
		"Employee",
		filters={"status": "Active"},
		fields=["employee_name", "date_of_birth", "department"],
		limit=0,
	)
	out = [
		{"Employee": r.employee_name, "Birthday": str(r.date_of_birth), "Department": r.department or ""}
		for r in rows
		if r.date_of_birth and getdate(r.date_of_birth).month == m
	]
	out.sort(key=lambda r: r["Birthday"][-2:])
	return {
		"title": "Birthdays This Month",
		"summary": f"{len(out)} birthdays this month.",
		"kpis": [{"label": "Birthdays", "value": len(out)}],
		"columns": ["Employee", "Birthday", "Department"], "rows": out[:20],
		"total_count": len(out), "link": _link("Employee"), "followUps": ["employee_directory"],
	}


def _sp_production_progress(site, company):
	rows = site.get_list(
		"Work Order",
		filters=_with_company([["status", "in", ["Not Started", "In Process", "Completed"]],
			["creation", ">=", _month_start()]], company),
		fields=["name", "production_item", "qty", "produced_qty", "status"],
		order_by="creation desc", limit=20,
	)
	planned = sum(flt(r.qty) for r in rows)
	produced = sum(flt(r.produced_qty) for r in rows)
	pct = (produced / planned * 100) if planned else 0
	return {
		"title": "Production Progress (Month)",
		"summary": f"{fmt_n(produced)} of {fmt_n(planned)} units produced ({pct:.0f}%).",
		"kpis": [
			{"label": "Planned", "value": fmt_n(planned)},
			{"label": "Produced", "value": fmt_n(produced), "color": "#17a34a"},
			{"label": "Progress", "value": f"{pct:.0f}%"},
		],
		"columns": ["Work Order", "Item", "Planned", "Produced", "Status"],
		"rows": [
			{"Work Order": r.name, "Item": r.production_item, "Planned": r.qty,
				"Produced": r.produced_qty, "Status": r.status}
			for r in rows
		],
		"total_count": len(rows), "link": _link("Work Order"), "followUps": ["open_work_orders"],
	}


SPECIALS = {
	"company_overview": _sp_company_overview,
	"today_summary": _sp_today_summary,
	"bank_balance": _sp_bank_balance,
	"monthly_revenue": _sp_monthly_revenue,
	"tax_summary": _sp_tax_summary,
	"quotation_conversion_rate": _sp_quotation_conversion,
	"stock_balance": _sp_stock_balance,
	"low_stock_items": _sp_low_stock,
	"stock_valuation": _sp_stock_valuation,
	"birthdays_this_month": _sp_birthdays,
	"production_progress": _sp_production_progress,
}


# ─── public API ───────────────────────────────────────────────────────────────

def execute(site, query_id, company=None, extra_filters=None, order_by=None,
            number_format=None):
	q = QUERIES.get(query_id)
	if not q:
		frappe.throw(f"Unknown query: {query_id}")
	frappe.local.erpera_number_format = number_format
	try:
		if q["kind"] == "special":
			return SPECIALS[query_id](site, company)
		if q["kind"] == "group":
			return _run_group(site, q, company, extra_filters, order_by)
		if q["kind"] == "report":
			return _run_report(site, q, company, extra_filters, order_by)
		return _run_list(site, q, company, extra_filters, order_by)
	except erp.ErpForbidden:
		return {
			"title": q["label"], "summary": "You don't have permission to view this data.",
			"kpis": [], "columns": [], "rows": [], "total_count": 0, "link": "", "followUps": [],
		}
	except Exception as e:
		frappe.log_error(title=f"AI Assist quick query {query_id}",
		                 message=frappe.get_traceback())
		return {
			"title": q["label"],
			"summary": f"Couldn't run this query: {str(e)[:160]}",
			"kpis": [], "columns": [], "rows": [], "total_count": 0, "link": "", "followUps": [],
		}


def suggestion_meta(ids):
	out = []
	for qid in ids[:4]:
		q = QUERIES.get(qid)
		if q:
			out.append({"id": qid, "label": q["label"], "icon": ""})
	return out


def match_free_text(message):
	"""Cheap keyword match for Basic free-text: score registry entries."""
	words = {w for w in "".join(c if c.isalnum() else " " for c in (message or "").lower()).split() if len(w) > 2}
	best, best_score = None, 0
	for qid, q in QUERIES.items():
		hay = f"{q['label']} {qid.replace('_', ' ')} {q['department']}".lower()
		score = sum(1 for w in words if w in hay)
		if score > best_score:
			best, best_score = qid, score
	return best if best_score >= 2 else None
