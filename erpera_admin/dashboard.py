# Copyright (c) 2026, The Tiny App Company and contributors
# For license information, please see license.txt

"""Everything the control page shows, in one round trip.

One call rather than six, because the page is read far more often than it is
changed and each extra request is a spinner the operator has to sit through.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, nowdate


def _guard():
	if "System Manager" not in frappe.get_roles():
		frappe.throw(_("Not permitted."), frappe.PermissionError)


@frappe.whitelist()
def summary(from_date=None, to_date=None):
	_guard()
	from_date = from_date or add_days(nowdate(), -30)
	to_date = to_date or nowdate()
	rng = {"f": from_date, "t": to_date}
	WHERE = "occurred_on >= %(f)s and occurred_on < date_add(%(t)s, interval 1 day)"

	totals = frappe.db.sql(f"""
		select
			count(*) calls,
			count(distinct coalesce(concat(site, ':', correlation_id), name)) turns,
			coalesce(sum(input_tokens), 0) input_tokens,
			coalesce(sum(output_tokens), 0) output_tokens,
			coalesce(sum(cost), 0) cost,
			sum(case when status = 'Failed' then 1 else 0 end) failed,
			sum(case when rated = 0 then 1 else 0 end) unrated,
			count(distinct site) sites,
			coalesce(avg(duration_ms), 0) avg_ms
		from `tabAI Call Record` where {WHERE}
	""", rng, as_dict=True)[0]

	by_site = frappe.db.sql(f"""
		select site, count(*) calls,
			count(distinct coalesce(concat(site, ':', correlation_id), name)) turns,
			coalesce(sum(cost), 0) cost, coalesce(sum(total_tokens), 0) tokens,
			sum(case when status = 'Failed' then 1 else 0 end) failed
		from `tabAI Call Record` where {WHERE}
		group by site order by cost desc limit 25
	""", rng, as_dict=True)

	by_model = frappe.db.sql(f"""
		select provider, model, count(*) calls,
			coalesce(sum(total_tokens), 0) tokens, coalesce(sum(cost), 0) cost,
			sum(case when rated = 0 then 1 else 0 end) unrated
		from `tabAI Call Record` where {WHERE}
		group by provider, model order by cost desc limit 25
	""", rng, as_dict=True)

	by_day = frappe.db.sql(f"""
		select date(occurred_on) d, count(*) calls, coalesce(sum(cost), 0) cost
		from `tabAI Call Record` where {WHERE}
		group by date(occurred_on) order by d
	""", rng, as_dict=True)

	recent = frappe.db.sql(f"""
		select name, site, occurred_on, user, channel, agent, provider, model,
			input_tokens, output_tokens, duration_ms, cost, currency, status, rated
		from `tabAI Call Record` where {WHERE}
		order by occurred_on desc limit 40
	""", rng, as_dict=True)

	sites = frappe.get_all(
		"Managed Site",
		fields=["name", "enabled", "plan", "last_seen", "calls_received"],
		order_by="last_seen desc",
		limit_page_length=50,
	)

	from erpera_admin.ttac_admin.doctype.ttac_ai_settings.ttac_ai_settings import resolved_config

	# The raw stored choices, so the form controls can be filled from this same
	# payload. resolved_config() reports what a site receives, which is not the
	# same thing: it resolves a blank model to the credential's default.
	st = frappe.get_cached_doc("TTAC AI Settings")
	settings = {
		"active_provider": st.active_provider or "",
		"active_model": st.active_model or "",
		"fallback_provider": st.fallback_provider or "",
		"fallback_model": st.fallback_model or "",
	}

	return {
		"range": {"from_date": from_date, "to_date": to_date},
		"settings": settings,
		"totals": totals,
		"by_site": by_site,
		"by_model": by_model,
		"by_day": by_day,
		"recent": recent,
		"sites": sites,
		"config": resolved_config(),
		"providers": frappe.get_all(
			"AI Provider Credential",
			fields=["name", "provider", "default_model", "enabled"],
			order_by="provider",
		),
	}


@frappe.whitelist()
def set_model(provider=None, model=None, fallback_provider=None, fallback_model=None):
	"""Change what every site answers with, from the control page."""
	_guard()
	s = frappe.get_doc("TTAC AI Settings")
	s.active_provider = provider or None
	s.active_model = model or None
	s.fallback_provider = fallback_provider or None
	s.fallback_model = fallback_model or None
	s.save()
	frappe.db.commit()

	from erpera_admin.ttac_admin.doctype.ttac_ai_settings.ttac_ai_settings import resolved_config

	return resolved_config()
