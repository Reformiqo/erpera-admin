# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

"""Buying credits, independent of who is asking.

Two doors lead here. A person signed into the account portal, and a tenant
site calling with its own token on behalf of the Desk page its user is looking
at. Both want the same three things done, and doing them in one place is what
keeps the token path and the session path from drifting into two different
sets of rules about what a payment is worth.

Whoever is asking, the amount is quoted here and the callback is verified
here. The caller supplies a site and a number of credits, never a price.
"""

import frappe
from frappe import _

from erpera_admin import credits as credits_mod
from erpera_admin import gateway


def account(site):
	"""Everything the purchase page draws itself from."""
	bal = credits_mod.balance(site)
	row = frappe.db.get_value("Managed Site", site,
	                          ["company_name", "on_credits"], as_dict=True) or {}
	return {
		"site": site,
		"company_name": row.get("company_name"),
		"on_credits": bool(row.get("on_credits")),
		"balance": bal["credits"],
		"valid_upto": bal["valid_upto"],
		"expired": bal["expired"],
		"pricing": credits_mod.public_pricing(),
		# Whether we can take a card today is ours to answer, not something the
		# page should discover by trying and failing.
		"payments_ok": gateway.available(),
		"history": frappe.get_all(
			"AI Assist Payment",
			filters={"site": site, "status": "Paid"},
			fields=["name", "paid_on", "credits", "amount", "tax", "total",
			        "currency", "method", "invoice_no", "invoice_url"],
			order_by="paid_on desc, creation desc", limit_page_length=20,
			ignore_permissions=True),
	}


def quote(credits):
	"""A price, with nothing written down. Safe to call on every keystroke."""
	return credits_mod.public_quote(credits)


def place_order(site, credits, account_user=None):
	"""Open an order and leave a Pending payment behind it."""
	q = credits_mod.quote(credits)

	payment = frappe.get_doc({
		"doctype": "AI Assist Payment",
		"account": account_user or frappe.db.get_value("Managed Site", site, "account"),
		"site": site,
		"credits": q["credits"],
		"status": "Pending",
		"amount": q["net_total"],
		"tax": q["tax"],
		"total": q["grand_total"],
		"currency": "INR",
		"method": "Razorpay",
	}).insert(ignore_permissions=True)

	order = gateway.create_order(
		q["grand_total"], receipt=payment.name,
		notes={"site": site, "credits": str(q["credits"]), "payment": payment.name})

	payment.db_set("reference", order["order_id"], update_modified=False)
	frappe.db.commit()

	# The browser gets the price it must pay, never how it was arrived at.
	public = dict(q)
	public["lines"] = [{"credits": ln["credits"], "rate": ln["rate"],
	                    "amount": ln["amount"]} for ln in q["lines"]]
	return {"order": order, "quote": public, "payment": payment.name}


def settle(razorpay_order_id, razorpay_payment_id, razorpay_signature, site=None):
	"""Turn a finished payment into credits.

	Three things are checked before a single credit is granted: the signature,
	which only Razorpay could have produced; the payment's status fetched from
	Razorpay rather than reported by the browser; and the amount, against what
	we asked for. Then Pending flips to Paid under a row lock, so a repeated
	callback cannot grant twice.
	"""
	if not gateway.verify_signature(razorpay_order_id, razorpay_payment_id,
	                                razorpay_signature):
		frappe.throw(_("This payment could not be verified."))

	row = frappe.db.get_value("AI Assist Payment", {"reference": razorpay_order_id},
	                          ["name", "site"], as_dict=True)
	if not row:
		frappe.throw(_("Unknown order."))
	if site and row.site != site:
		# A site's token may only settle that site's own orders.
		frappe.throw(_("This order belongs to another site."))

	paid = gateway.fetch_payment(razorpay_payment_id)
	if paid["status"] not in ("captured", "authorized"):
		frappe.throw(_("This payment has not gone through."))
	if paid["order_id"] != razorpay_order_id:
		frappe.throw(_("This payment belongs to a different order."))

	locked = frappe.db.sql(
		"""SELECT name, status, credits, total FROM `tabAI Assist Payment`
		   WHERE name = %s FOR UPDATE""", row.name, as_dict=True)[0]
	if locked.status == "Paid":
		return {"ok": True, "already": True,
		        "credits": credits_mod.balance(row.site)["credits"]}

	if paid["amount"] != gateway.to_paise(locked.total):
		frappe.throw(_("The amount paid does not match the order."))

	credits_mod.grant(row.site, locked.credits, payment=row.name,
	                  remarks=_("Razorpay {0}").format(razorpay_payment_id))
	frappe.db.set_value("AI Assist Payment", row.name, {
		"status": "Paid",
		"paid_on": frappe.utils.today(),
		"method": paid.get("method") or "Razorpay",
	}, update_modified=False)
	frappe.db.commit()

	# The invoice is raised after the credits, never before, and a failure here
	# is logged rather than raised. The customer has paid and the wallet must
	# reflect that; an invoice we owe them is a problem for us to chase, not a
	# reason to swallow their money.
	invoice = None
	try:
		from erpera_admin import invoicing

		invoice = invoicing.create_for_payment(row.name)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title=f"Invoice failed for {row.name}")

	return {"ok": True, "already": False, "credits_added": locked.credits,
	        "credits": credits_mod.balance(row.site)["credits"],
	        "invoice": invoice}
