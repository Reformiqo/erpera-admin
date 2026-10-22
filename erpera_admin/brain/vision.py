# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

"""Reading a bill, and turning it into something the agent can act on.

The prompt below is the whole of this feature. It is what tells a model that
the company at the top of a received bill is the supplier and not the
recipient, that a proforma is not an invoice, and that a line's rate is the
unit rate and not the line total. Those rules are what took the time, and they
used to ship inside the customer's app where a competitor could read them off
disk.

The PDF stays on their side: rendering page one to an image is plain work with
no product in it, and sending a whole PDF up to render it here would cost
bandwidth for nothing.
"""

import json
import re

import frappe
from frappe import _

from erpera_admin import relay


PROMPT = """You extract a business document to record in the RECIPIENT's ERPNext.
Return ONLY JSON:
{"doc_kind":"purchase_invoice|sales_invoice|quotation|purchase_order|receipt|unknown","party":"the OTHER company",
 "party_role":"supplier|customer","bill_no":"their invoice/bill number","bill_date":"YYYY-MM-DD",
 "currency":"INR","lines":[{"item":"description","qty":1,"rate":0,"hsn":"HSN/SAC if shown"}],
 "grand_total":0,"notes":""}
Rules:
- A bill the recipient RECEIVED from a vendor (vendor name/GSTIN at the TOP, "Bill To" = the
  recipient) is "purchase_invoice"; party = the VENDOR (the issuer), party_role = "supplier".
- An invoice the recipient ISSUED to a customer is "sales_invoice"; party = that customer.
- A QUOTATION / PROFORMA / ESTIMATE (says "Quotation", "Quote", "Valid Until", "Quote For")
  is doc_kind "quotation" — it is NOT an invoice; never label it purchase_invoice or
  sales_invoice. A PURCHASE ORDER is "purchase_order".
- "party" is ALWAYS the other company — never the recipient.
- Transcribe EVERY line with quantity and UNIT rate (NOT the line total). null if unknown.
- Never invent values."""


def extract(site, image_data_url, context=None):
	"""An image data URL in, structured fields out."""
	if not re.match(r"^data:image/[a-zA-Z+]+;base64,", image_data_url or ""):
		frappe.throw(_("Send an image of the invoice or receipt."))

	out = relay.vision(site, image_data_url=image_data_url, prompt=PROMPT,
	                   context=context)
	text = out.get("text") or ""
	jm = re.search(r"\{[\s\S]*\}", text or "")
	if not jm:
		frappe.throw(_("Couldn't read the document."))
	raw = json.loads(jm.group(0))
	lines = []
	for l in raw.get("lines") or []:
		item = str(l.get("item") or l.get("description") or "").strip()[:140]
		if not item:
			continue
		hsn = re.sub(r"[^0-9]", "", str(l.get("hsn") or ""))[:8]
		lines.append(
			{
				"item": item,
				"qty": float(l.get("qty") or 1),
				"rate": float(l.get("rate") or 0),
				**({"hsn": hsn} if hsn else {}),
			}
		)
	bill_date = str(raw.get("bill_date") or "")
	return {
		"doc_kind": str(raw.get("doc_kind") or "unknown"),
		"party": raw.get("party") or None,
		"party_role": raw.get("party_role") if raw.get("party_role") in ("supplier", "customer") else None,
		"bill_no": raw.get("bill_no") or None,
		"bill_date": bill_date if re.match(r"^\d{4}-\d{2}-\d{2}$", bill_date) else None,
		"currency": raw.get("currency") or None,
		"grand_total": float(raw.get("grand_total") or 0) or None,
		"notes": raw.get("notes") or None,
		"lines": lines,
	}


def to_message(v):
	"""Extraction → the chat message that drives the normal draft flow."""
	if v.get("doc_kind") in ("quotation", "purchase_order", "unknown"):
		label = {"quotation": "QUOTATION", "purchase_order": "PURCHASE ORDER"}.get(v.get("doc_kind"), "document")
		parts = [f"I uploaded a {label}"]
		if v.get("bill_no"):
			parts.append(f"number {v['bill_no']}")
		if v.get("party"):
			parts.append(f"from {v['party']}")
		if v.get("bill_date"):
			parts.append(f"dated {v['bill_date']}")
		lines = "; ".join(
			f"{l['item']} qty {l['qty']:g} rate {l['rate']:g}" for l in v.get("lines") or []
		)
		if lines:
			parts.append(f"with lines: {lines}")
		parts.append(
			"— this is NOT an invoice, so do not create one. Summarize it and ask me what "
			"I want to do with it (e.g. record a Supplier Quotation draft, or nothing)."
		)
		return " ".join(parts)
	sales = v.get("doc_kind") == "sales_invoice" or v.get("party_role") == "customer"
	kind = "sales invoice" if sales else "purchase invoice"
	party_word = "customer" if sales else "supplier"
	parts = [f"Create a {kind}"]
	if v.get("party"):
		parts.append(f"for {party_word} {v['party']}")
	if v.get("bill_no"):
		parts.append(f"bill no {v['bill_no']}")
	if v.get("bill_date"):
		parts.append(f"dated {v['bill_date']}")
	lines = "; ".join(
		f"{l['item']} qty {l['qty']:g} rate {l['rate']:g}" + (f" hsn {l['hsn']}" if l.get("hsn") else "")
		for l in v.get("lines") or []
	)
	if lines:
		parts.append(f"with lines: {lines}")
	return " ".join(parts)
