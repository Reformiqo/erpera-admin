"""The enrolment code is gone; carry installations off it.

A site that was in Key mode is moved to Open rather than left on a value the
field no longer offers, which would read as Open anyway but show as blank in
the form. The stored code is deleted too: `__Auth` keeps password values in a
table of its own, so dropping the field alone would leave the secret behind
with nothing pointing at it.
"""

import frappe

DOCTYPE = "TTAC AI Settings"


def execute():
	if frappe.db.exists("DocType", DOCTYPE):
		mode = frappe.db.get_single_value(DOCTYPE, "enrolment_mode")
		if (mode or "").strip() == "Key":
			frappe.db.set_single_value(DOCTYPE, "enrolment_mode", "Open")

	frappe.db.delete("__Auth", {"doctype": DOCTYPE, "fieldname": "enrol_key"})
	frappe.db.delete("Singles", {"doctype": DOCTYPE, "field": "enrol_key"})
	frappe.db.commit()
