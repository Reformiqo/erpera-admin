# Copyright (c) 2026, The Tiny App Company and contributors
# For license information, please see license.txt

"""Raising the GST invoice for a credit purchase.

A tax invoice is issued at purchase for the whole amount, and spending credits
afterwards is an untaxed drawdown. That is a deliberate reading and it is worth
saying why, because the alternative is defensible too.

Prepaid credits are money taken before the service is given, which makes them
an advance. Read strictly, GST on an advance for services falls due in the
month it is received, against a receipt voucher, with the tax invoice following
when the service is actually supplied. Run that way, every wallet becomes a
running reconciliation between vouchers and invoices.

The common treatment, and the one here, is that selling the credit is itself
the supply: the customer has bought a right to use the service, the invoice
covers it in full, and later consumption raises no further tax event. Credits
that expire unused stay taxed, which is both simpler and correct. Have the
company's accountant confirm this before the first live customer, because it
decides whether step one is a receipt voucher or a tax invoice, and that is not
a thing anyone wants to restate afterwards.

Place of supply comes from the buyer's state, so an intra-state sale carries
CGST plus SGST and anything else carries IGST. india_compliance does the split
once the right tax template is on the invoice; choosing that template is this
module's job.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, today

SAC_CODE = "997331"  # Licensing services for the right to use computer software
ITEM_CODE = "AI-ASSIST-CREDITS"


def _settings():
	return frappe.get_cached_doc("TTAC AI Settings")


def enabled():
	return bool(cint(_settings().get("invoicing_enabled")))


def _company():
	name = _settings().get("invoice_company")
	if not name:
		frappe.throw(_("No invoicing company is set in TTAC AI Settings."))
	return name


def state_of(gstin, fallback=None):
	"""The state a GSTIN belongs to. Its first two digits say so."""
	from india_compliance.gst_india.constants import STATE_NUMBERS

	gstin = (gstin or "").strip().upper()
	if len(gstin) >= 2 and gstin[:2].isdigit():
		for state, number in STATE_NUMBERS.items():
			if number == gstin[:2]:
				return state
	return fallback


def ensure_item():
	"""The one service line every credit invoice uses."""
	configured = _settings().get("invoice_item")
	if configured and frappe.db.exists("Item", configured):
		return configured
	if frappe.db.exists("Item", ITEM_CODE):
		return ITEM_CODE

	group = (frappe.db.get_value("Item Group", {"item_group_name": "Services"}, "name")
	         or frappe.db.get_value("Item Group", {"is_group": 0}, "name"))
	frappe.get_doc({
		"doctype": "Item",
		"item_code": ITEM_CODE,
		"item_name": "AI Assist Credits",
		"description": "Prepaid credits for AI Assist.",
		"item_group": group,
		"stock_uom": "Nos",
		# A service, so it carries a SAC rather than an HSN, and must never
		# look like something that moves through a warehouse.
		"is_stock_item": 0,
		"is_sales_item": 1,
		"is_purchase_item": 0,
		"gst_hsn_code": SAC_CODE,
	}).insert(ignore_permissions=True)
	return ITEM_CODE


def ensure_customer(site):
	"""A Customer and its billing Address, kept in step with the Managed Site."""
	row = frappe.db.get_value(
		"Managed Site", site,
		["name", "company_name", "billing_name", "billing_gstin", "billing_email",
		 "billing_address_line1", "billing_address_line2", "billing_city",
		 "billing_state", "billing_pincode", "billing_country", "account"],
		as_dict=True)
	if not row:
		frappe.throw(_("Unknown site {0}.").format(site))

	title = (row.billing_name or row.company_name or row.name).strip()
	gstin = (row.billing_gstin or "").strip().upper()
	state = state_of(gstin, row.billing_state)

	customer = frappe.db.get_value("Customer", {"ttac_site": site}, "name") \
		if frappe.get_meta("Customer").has_field("ttac_site") else None
	customer = customer or frappe.db.get_value("Customer", {"customer_name": title}, "name")

	if not customer:
		doc = frappe.get_doc({
			"doctype": "Customer",
			"customer_name": title,
			"customer_type": "Company",
			"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
			"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
			"gstin": gstin or None,
			"gst_category": "Registered Regular" if gstin else "Unregistered",
		}).insert(ignore_permissions=True)
		customer = doc.name
	else:
		frappe.db.set_value("Customer", customer, {
			"gstin": gstin or None,
			"gst_category": "Registered Regular" if gstin else "Unregistered",
		}, update_modified=False)

	_ensure_address(customer, title, row, state, gstin)
	return customer


def _ensure_address(customer, title, row, state, gstin):
	"""One billing address per customer, because place of supply reads it."""
	name = f"{title}-Billing"[:140]
	values = {
		"address_title": title,
		"address_type": "Billing",
		"address_line1": row.billing_address_line1 or title,
		"address_line2": row.billing_address_line2,
		"city": row.billing_city or state or "-",
		"state": state,
		"pincode": row.billing_pincode,
		"country": row.billing_country or "India",
		"gstin": gstin or None,
		"gst_category": "Registered Regular" if gstin else "Unregistered",
		"email_id": row.billing_email,
		"is_primary_address": 1,
	}

	existing = frappe.db.get_value("Address", {"address_title": title,
	                                           "address_type": "Billing"}, "name")
	if existing:
		frappe.db.set_value("Address", existing, values, update_modified=False)
		return existing

	doc = frappe.get_doc(dict(doctype="Address", **values))
	doc.append("links", {"link_doctype": "Customer", "link_name": customer})
	doc.insert(ignore_permissions=True)
	return doc.name


def _tax_template(company, buyer_state):
	"""In-state or out-of-state, decided by comparing the two GSTINs' states."""
	seller_state = state_of(frappe.db.get_value("Company", company, "gstin"))
	intra = bool(seller_state and buyer_state and seller_state == buyer_state)
	wanted = "In-state" if intra else "Out-state"

	# Reverse charge templates carry the same In-state / Out-state words and
	# would match first, so they are excluded by name. A forward-charge sale
	# must never pick one up.
	candidates = frappe.get_all(
		"Sales Taxes and Charges Template",
		filters={"company": company, "disabled": 0},
		pluck="name")
	for candidate in candidates:
		if wanted.lower() in candidate.lower() and "rcm" not in candidate.lower():
			return candidate, intra

	frappe.throw(_("No {0} GST tax template found for {1}.").format(wanted, company))


def create_for_payment(payment):
	"""Raise the invoice for a paid credit purchase. Idempotent."""
	if not enabled():
		return None

	pay = frappe.get_doc("AI Assist Payment", payment)
	if pay.status != "Paid":
		frappe.throw(_("Only a paid order gets an invoice."))
	if pay.invoice_no:
		return pay.invoice_no

	company = _company()
	customer = ensure_customer(pay.site)
	item = ensure_item()

	buyer_state = state_of(frappe.db.get_value("Customer", customer, "gstin"))
	template, intra = _tax_template(company, buyer_state)

	# GST caps an invoice number at 16 characters and ERPNext's default series
	# is 19, so the series is configured rather than left to the default.
	series = (_settings().get("invoice_naming_series") or "SINV-.YY.-").strip()

	si = frappe.get_doc({
		"doctype": "Sales Invoice",
		"naming_series": series,
		"company": company,
		"customer": customer,
		"posting_date": pay.paid_on or today(),
		"due_date": pay.paid_on or today(),
		"currency": pay.currency or "INR",
		"taxes_and_charges": template,
		"place_of_supply": None,  # india_compliance fills this from the address
		"items": [{
			"item_code": item,
			"qty": 1,
			"uom": "Nos",
			"rate": flt(pay.amount),
			"description": _("AI Assist credits: {0}").format(flt(pay.credits, 2)),
			"gst_hsn_code": SAC_CODE,
		}],
		"remarks": _("Credit purchase {0} for site {1}.").format(pay.name, pay.site),
	})
	si.set_missing_values()
	si.insert(ignore_permissions=True)

	if cint(_settings().get("invoice_submit")):
		si.submit()

	frappe.db.set_value("AI Assist Payment", pay.name, {
		"invoice_no": si.name,
		"invoice_url": f"/app/sales-invoice/{si.name}",
	}, update_modified=False)
	frappe.db.commit()
	return si.name
