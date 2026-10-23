# Copyright (c) 2026, The Tiny App Company and contributors
# For license information, please see license.txt

"""Today's activity and the trend behind it, read from the customer's site.

Ported from the tenant app unchanged in shape, because the widget that renders
it is unchanged. Only the two functions that touch data moved: a count-and-sum
is now one aggregate request to their site, and a daily series is one grouped
request in date order.
"""

import frappe
from frappe.utils import add_days, flt, nowdate


def _base(filters, company):
	f = list(filters)
	if company:
		f.append(["company", "=", company])
	return f


def _count_sum(site, doctype, filters, sum_field=None):
	try:
		rows = site.aggregate(doctype, filters=filters,
		                      sum_fields=[sum_field] if sum_field else [])
		row = rows[0] if rows else {}
		return {"count": int(row.get("_count") or 0),
		        "total": flt(row.get(sum_field)) if sum_field else 0.0}
	except Exception:
		# One unreadable doctype must not blank the whole dashboard; a zero row
		# reads as "nothing today", which is what the user would conclude anyway.
		return {"count": 0, "total": 0.0}


def today_summary(site, company=None):
	t = nowdate()
	return {
		"sales_invoices": _count_sum(
			site, "Sales Invoice", _base([["docstatus", "=", 1], ["posting_date", "=", t]], company), "grand_total"
		),
		"payments_received": _count_sum(
			site, "Payment Entry",
			_base([["docstatus", "=", 1], ["posting_date", "=", t], ["payment_type", "=", "Receive"]], company),
			"paid_amount",
		),
		"purchase_orders": _count_sum(
			site, "Purchase Order", _base([["docstatus", "=", 1], ["transaction_date", "=", t]], company), "grand_total"
		),
		"stock_entries": _count_sum(
			site, "Stock Entry", _base([["docstatus", "=", 1], ["posting_date", "=", t]], company)
		),
		"new_leads": _count_sum(site, "Lead", [["creation", ">=", f"{t} 00:00:00"]]),
	}


def _daily_trend(site, doctype, date_field, amt_field, start, company):
	try:
		rows = site.daily(doctype, _base([["docstatus", "=", 1], [date_field, ">=", start]], company),
		                  date_field, amt_field)
	except Exception:
		return {}
	return {str(r.get(date_field)): flt(r.get("total")) for r in rows}


def get_dashboard_trends(site, company=None, days=30):
	from datetime import timedelta

	from frappe.utils import getdate

	days = max(7, min(int(days or 30), 90))
	start = add_days(nowdate(), -days)

	sales_by_day = _daily_trend(site, "Sales Invoice", "posting_date", "grand_total", start, company)
	purch_by_day = _daily_trend(site, "Purchase Invoice", "posting_date", "grand_total", start, company)

	labels, sales, purchase = [], [], []
	d = getdate(start)
	end = getdate(nowdate())
	while d <= end:
		key = str(d)
		labels.append(d.strftime("%d %b"))
		sales.append(sales_by_day.get(key, 0))
		purchase.append(purch_by_day.get(key, 0))
		d = d + timedelta(days=1)

	sales_total = sum(sales)
	purchase_total = sum(purchase)

	# AR aging buckets on outstanding invoices. Four aggregates rather than one
	# grouped call, because the buckets are date arithmetic, not a field.
	t = getdate(nowdate())
	buckets = [("0–30", 0, 30), ("31–60", 31, 60), ("61–90", 61, 90), ("90+", 91, 100000)]
	aging_totals = []
	for _, lo, hi in buckets:
		f = _base(
			[["docstatus", "=", 1], ["outstanding_amount", ">", 0],
				["due_date", "<=", str(t - timedelta(days=lo))],
				["due_date", ">", str(t - timedelta(days=hi + 1))]],
			company,
		)
		aging_totals.append(_count_sum(site, "Sales Invoice", f, "outstanding_amount")["total"])

	try:
		top = site.group("Sales Invoice",
		                 _base([["docstatus", "=", 1], ["posting_date", ">=", start]], company),
		                 "customer", "grand_total", limit=5)
	except Exception:
		top = []

	return {
		"window": days,
		"sales_trend": {"labels": labels, "sales": sales, "purchase": purchase},
		"totals": {
			"sales": sales_total,
			"purchase": purchase_total,
			"net": sales_total - purchase_total,
			"receivables": sum(aging_totals),
		},
		"ar_aging": {"labels": [b[0] for b in buckets], "totals": aging_totals},
		"top_customers": [{"label": r["label"], "total": flt(r["total"])} for r in top],
	}
