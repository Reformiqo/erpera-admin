# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

"""What the estate costs, grouped the way the bill is argued about.

Cost per question rather than per call: one question can take several
roundtrips, and the per-call average makes the product look cheaper than it is.
"""

import frappe
from frappe import _

GROUP_SQL = {
	"Site": "site",
	"Model": "model",
	"Provider": "provider",
	"User": "concat(site, ' / ', coalesce(user, '?'))",
	"Channel": "channel",
	"Agent": "agent",
	"Day": "date(occurred_on)",
}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return get_columns(filters), get_data(filters)


def get_columns(filters):
	group = filters.get("group_by") or "Site"
	first = {
		"Site": {"fieldname": "grp", "label": _("Site"), "fieldtype": "Link", "options": "Managed Site", "width": 220},
		"Day": {"fieldname": "grp", "label": _("Day"), "fieldtype": "Date", "width": 120},
	}.get(group, {"fieldname": "grp", "label": _(group), "fieldtype": "Data", "width": 220})

	return [
		first,
		{"fieldname": "turns", "label": _("Questions"), "fieldtype": "Int", "width": 100},
		{"fieldname": "calls", "label": _("Calls"), "fieldtype": "Int", "width": 90},
		{"fieldname": "failed", "label": _("Failed"), "fieldtype": "Int", "width": 85},
		{"fieldname": "input_tokens", "label": _("Input Tokens"), "fieldtype": "Int", "width": 120},
		{"fieldname": "output_tokens", "label": _("Output Tokens"), "fieldtype": "Int", "width": 125},
		{"fieldname": "cost", "label": _("Cost"), "fieldtype": "Float", "precision": 6, "width": 110},
		{"fieldname": "cost_per_turn", "label": _("Cost / Question"), "fieldtype": "Float", "precision": 6, "width": 130},
		{"fieldname": "avg_ms", "label": _("Avg Call (ms)"), "fieldtype": "Int", "width": 115},
		{"fieldname": "unrated", "label": _("Unpriced Calls"), "fieldtype": "Int", "width": 125},
	]


def get_data(filters):
	group = GROUP_SQL[filters.get("group_by") or "Site"]
	where, values = ["1=1"], {}

	if filters.get("from_date"):
		where.append("occurred_on >= %(from_date)s")
		values["from_date"] = filters.from_date
	if filters.get("to_date"):
		where.append("occurred_on < date_add(%(to_date)s, interval 1 day)")
		values["to_date"] = filters.to_date
	for f in ("site", "provider", "model", "channel"):
		if filters.get(f):
			where.append(f"`{f}` = %({f})s")
			values[f] = filters.get(f)

	rows = frappe.db.sql(
		f"""
		select
			{group} as grp,
			count(distinct coalesce(concat(site, ':', correlation_id), name)) as turns,
			count(*) as calls,
			sum(case when status = 'Failed' then 1 else 0 end) as failed,
			sum(input_tokens) as input_tokens,
			sum(output_tokens) as output_tokens,
			sum(cost) as cost,
			avg(duration_ms) as avg_ms,
			sum(case when rated = 0 then 1 else 0 end) as unrated
		from `tabAI Call Record`
		where {' and '.join(where)}
		group by {group}
		order by cost desc, calls desc
		""",
		values,
		as_dict=True,
	)

	for r in rows:
		r["cost_per_turn"] = round((r.cost or 0) / r.turns, 6) if r.turns else 0
		r["avg_ms"] = int(r.avg_ms or 0)
	return rows
