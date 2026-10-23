# Copyright (c) 2026, The Tiny App Company and contributors
# For license information, please see license.txt

import frappe


def has_app_permission():
	"""The control plane shows every site's spend, so it is admin-only."""
	return "System Manager" in frappe.get_roles()
