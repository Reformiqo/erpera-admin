# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

"""Prepaid credits for AI Assist, priced at cost plus a markup.

One credit is one rupee of provider cost. The customer buys credits at a
markup, and every call takes from the wallet exactly what the provider charged
us for it. That single choice is what makes "cost plus 50%" true of every call
rather than true on average: a Haiku question and an Opus question cost the
customer what they really cost, and the margin is identical on both.

Pricing a call rather than a question is the trade. The customer can no longer
be promised "10,000 questions", because a one-line question and a long report
are not the same purchase. The calculator answers that by quoting an estimate
against the default model, which is honest about being an estimate.

Provider rates are quoted in USD and we sell in INR, so a conversion sits in
the middle. It is a pinned number in settings, not a live rate: a wallet whose
value moves with the dollar is not a wallet anyone can budget against. Pin it
above spot and revisit it, rather than letting FX quietly eat the markup.

Two invariants hold everything else up. The price is computed here and never
accepted from a browser, and AI Credit Ledger is the record while the balance
on Managed Site is only a cache of it.
"""

import frappe
from frappe import _
from frappe.utils import add_months, cint, flt, getdate, now_datetime, today

PRECISION = 6  # a cheap call can cost a small fraction of a rupee


class OutOfCredits(frappe.ValidationError):
	"""Raised when a site has nothing left to spend."""


# ----------------------------------------------------------------- the price

def pricing():
	"""What the calculator needs to draw itself."""
	s = frappe.get_cached_doc("ERPERA AI Settings")
	bands = [
		{"upto": cint(r.upto_credits), "markup": flt(r.markup_percent)}
		for r in (s.get("credit_slabs") or [])
	]
	# No bands configured means one flat markup, which is the common case.
	if not bands:
		bands = [{"upto": 0, "markup": flt(s.get("credit_markup_percent")) or 50.0}]

	for b in bands:
		b["rate"] = flt(1.0 + b["markup"] / 100.0, 4)

	return {
		"enabled": bool(cint(s.get("credits_enabled"))),
		"multiplier": cint(s.get("credit_multiplier")) or 100,
		"validity_months": cint(s.get("credit_validity_months")) or 12,
		"tax_percent": flt(s.get("credit_tax_percent")),
		"markup_percent": flt(s.get("credit_markup_percent")) or 50.0,
		"low_threshold": flt(s.get("low_credit_threshold")),
		"usd_inr": flt(s.get("usd_inr_rate")) or 90.0,
		"currency": "INR",
		"bands": bands,
	}


def public_pricing():
	"""Pricing as the customer is allowed to see it.

	What a credit sells for is their business. What it costs us, and therefore
	what we make on it, is not. The margin is stripped here rather than hidden
	in the page, because a field left in the response is a field anyone can
	read out of a network tab.
	"""
	p = pricing()
	return {
		"enabled": p["enabled"],
		"multiplier": p["multiplier"],
		"validity_months": p["validity_months"],
		"tax_percent": p["tax_percent"],
		"low_threshold": p["low_threshold"],
		"currency": p["currency"],
		"bands": [{"upto": b["upto"], "rate": b["rate"]} for b in p["bands"]],
	}


def public_quote(credits):
	"""A quote with the margin taken out of it."""
	q = quote(credits)
	q["lines"] = [{"credits": ln["credits"], "rate": ln["rate"],
	               "amount": ln["amount"]} for ln in q["lines"]]
	return q


def quote(credits):
	"""Price a purchase. The only place a total is allowed to come from."""
	credits = cint(credits)
	p = pricing()

	if credits <= 0:
		frappe.throw(_("Enter how many credits to buy."))

	multiple = p["multiplier"]
	if multiple > 1 and credits % multiple:
		frappe.throw(_("Credits are sold in multiples of {0}.").format(multiple))

	lines, remaining, taken = [], credits, 0
	for band in p["bands"]:
		if remaining <= 0:
			break
		ceiling = band["upto"]
		# A zero ceiling is the last band and carries whatever is left.
		size = remaining if ceiling <= 0 else min(remaining, max(0, ceiling - taken))
		if size <= 0:
			continue
		lines.append({
			"credits": size,
			"markup": band["markup"],
			"rate": band["rate"],
			"amount": flt(size * band["rate"], 2),
		})
		remaining -= size
		taken += size

	if remaining > 0:
		# Better to refuse than to quietly sell the tail at the wrong price.
		frappe.throw(_("Credit pricing does not cover a purchase this large. "
		               "Add a final band with no ceiling."))

	net = flt(sum(ln["amount"] for ln in lines), 2)
	tax = flt(net * p["tax_percent"] / 100.0, 2)
	return {
		"credits": credits,
		"lines": lines,
		"net_total": net,
		"tax_percent": p["tax_percent"],
		"tax": tax,
		"grand_total": flt(net + tax, 2),
		"currency": p["currency"],
		"valid_upto": str(add_months(getdate(today()), p["validity_months"])),
		"estimated_calls": estimate_calls(credits),
	}


# ------------------------------------------------------------ what a call costs

def cost_of(row):
	"""What one call took from us, in rupees.

	Worked out here from AI Model Rate rather than trusted from the site. The
	site reports tokens, which it observed; the price of a token is ours to
	know. A model we have no rate for costs nothing, because inventing a number
	would be worse than under-charging while someone notices the gap.
	"""
	provider, model = row.get("provider"), row.get("model")
	if not model:
		return 0.0

	rate = frappe.db.get_value(
		"AI Model Rate", {"provider": provider, "model": model},
		["input_rate_per_million", "output_rate_per_million", "currency"],
		as_dict=True)
	if not rate:
		return 0.0

	usd = (
		flt(row.get("input_tokens")) / 1_000_000.0 * flt(rate.input_rate_per_million)
		+ flt(row.get("output_tokens")) / 1_000_000.0 * flt(rate.output_rate_per_million)
	)
	if (rate.currency or "USD").upper() == "INR":
		return flt(usd, PRECISION)
	return flt(usd * pricing()["usd_inr"], PRECISION)


def estimate_calls(credits, model=None):
	"""Roughly how many questions a balance is worth.

	Presented to the customer as an estimate and nothing firmer, because the
	answer genuinely depends on how long their questions are.
	"""
	s = frappe.get_cached_doc("ERPERA AI Settings")
	model = model or s.get("active_model")
	if not model:
		return None

	# A question and its answer, sized from what the logs actually look like.
	sample = {"provider": None, "model": model,
	          "input_tokens": 2000, "output_tokens": 500}
	rate = frappe.db.get_value("AI Model Rate", {"model": model},
	                           ["provider"], as_dict=True)
	if not rate:
		return None
	sample["provider"] = rate.provider

	each = cost_of(sample)
	if each <= 0:
		return None
	return int(flt(credits) / each)


# ---------------------------------------------------------------- the ledger

def _lock(site):
	"""Take the site's row so two movements cannot read the same balance."""
	row = frappe.db.sql(
		"""SELECT name, credit_balance, credits_valid_upto
		   FROM `tabManaged Site` WHERE name = %s FOR UPDATE""",
		site, as_dict=True)
	if not row:
		frappe.throw(_("Unknown site {0}.").format(site))
	return row[0]


def _post(site, entry_type, credits, valid_upto=None, call_record=None,
          payment=None, remarks=None):
	"""Write one movement and leave the cache agreeing with it."""
	row = _lock(site)
	balance_after = flt(flt(row.credit_balance) + flt(credits), PRECISION)

	frappe.get_doc({
		"doctype": "AI Credit Ledger",
		"site": site,
		"entry_type": entry_type,
		"occurred_on": now_datetime(),
		"credits": flt(credits, PRECISION),
		"balance_after": balance_after,
		"valid_upto": valid_upto,
		"call_record": call_record,
		"payment": payment,
		"remarks": remarks,
	}).insert(ignore_permissions=True)

	update = {"credit_balance": balance_after}
	if valid_upto:
		update["credits_valid_upto"] = valid_upto
	frappe.db.set_value("Managed Site", site, update, update_modified=False)
	return balance_after


def grant(site, credits, payment=None, remarks=None):
	"""Add bought credits, and push the expiry out for the whole balance.

	Extending everything rather than tracking each block separately is a
	deliberate simplification. It is what the customer is told on the pricing
	card, it always favours them, and it keeps the balance a single number
	instead of a queue of expiring lots.
	"""
	credits = flt(credits)
	if credits <= 0:
		frappe.throw(_("A purchase has to add credits."))

	months = pricing()["validity_months"]
	valid_upto = add_months(getdate(today()), months)
	return _post(site, "Purchase", credits, valid_upto=valid_upto,
	             payment=payment, remarks=remarks)


def consume(site, credits, call_record=None):
	"""Spend what a call cost, after it has happened.

	Deliberately allows the balance to go negative. The call was made and
	answered; refusing to record it would lose the money rather than save it.
	The gate that stops the next call is `check`, which runs before the model.
	"""
	credits = flt(credits, PRECISION)
	if credits <= 0:
		return None
	return _post(site, "Consumption", -credits, call_record=call_record)


def expire(site):
	"""Zero a balance whose date has passed. Safe to run repeatedly."""
	row = _lock(site)
	balance = flt(row.credit_balance)
	if balance <= 0 or not row.credits_valid_upto:
		return balance
	if getdate(row.credits_valid_upto) >= getdate(today()):
		return balance
	return _post(site, "Expiry", -balance,
	             remarks=_("Expired on {0}.").format(row.credits_valid_upto))


def adjust(site, credits, remarks=None):
	"""A correction made by hand. Always leaves a row saying who and why."""
	return _post(site, "Adjustment", flt(credits),
	             remarks=remarks or _("Manual adjustment by {0}.").format(frappe.session.user))


# ------------------------------------------------------------------ the gate

def balance(site):
	"""What this site has left, with the expiry already applied."""
	row = frappe.db.get_value(
		"Managed Site", site,
		["credit_balance", "credits_valid_upto"], as_dict=True)
	if not row:
		return {"credits": 0.0, "valid_upto": None, "expired": False}

	expired = bool(row.credits_valid_upto
	               and getdate(row.credits_valid_upto) < getdate(today()))
	return {
		"credits": 0.0 if expired else flt(row.credit_balance, PRECISION),
		"valid_upto": str(row.credits_valid_upto) if row.credits_valid_upto else None,
		"expired": expired,
	}


def check(site):
	"""May this site make a model call right now?

	Returns the same shape as the licence check so the caller can treat a flat
	wallet and an unpaid subscription the same way.
	"""
	# Two switches, both of which have to be on. The global one turns the
	# feature on at all; the per-site one says this particular customer has
	# moved onto credits. Without the second, switching the feature on would
	# gate every existing customer at a zero balance at the same moment.
	if not cint(frappe.db.get_single_value("ERPERA AI Settings", "credits_enabled")):
		return {"ok": True, "reason": "credits_off"}
	if not cint(frappe.db.get_value("Managed Site", site, "on_credits")):
		return {"ok": True, "reason": "not_on_credits"}

	b = balance(site)
	if b["expired"]:
		return {"ok": False, "reason": "credits_expired",
		        "message": _("This site's AI Assist credits expired on {0}. "
		                     "Buy more to carry on.").format(b["valid_upto"]),
		        "credits": 0}
	if b["credits"] <= 0:
		return {"ok": False, "reason": "no_credits",
		        "message": _("This site has run out of AI Assist credits. "
		                     "Buy more to carry on."),
		        "credits": 0}

	low = flt(frappe.db.get_single_value("ERPERA AI Settings", "low_credit_threshold"))
	return {"ok": True, "reason": "ok", "credits": b["credits"],
	        "valid_upto": b["valid_upto"], "low": bool(low and b["credits"] < low)}


def expire_all():
	"""Nightly sweep. Scheduled, so a site that never calls still expires."""
	rows = frappe.get_all(
		"Managed Site",
		filters={"credit_balance": [">", 0], "credits_valid_upto": ["<", today()]},
		pluck="name")
	for site in rows:
		try:
			expire(site)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Credit expiry failed for {site}")
	return len(rows)
