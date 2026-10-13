# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import add_days, cint, nowdate


def purge_old_records():
	"""Drop call records past the retention window.

	Default is 0, meaning keep everything: tokens and cost are what billing and
	capacity planning run on, and they are cheap to store. An operator who sets
	a window usually wants the prompts gone, so content goes first and the row
	only disappears once the window itself is exceeded.
	"""
	days = cint(frappe.db.get_single_value("ERPERA AI Settings", "keep_records_days"))
	if days <= 0:
		return 0
	cutoff = add_days(nowdate(), -days)
	frappe.db.sql("delete from `tabAI Call Record` where occurred_on < %s", (cutoff,))
	frappe.db.commit()
	return 1
