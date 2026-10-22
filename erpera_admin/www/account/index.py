# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

"""What a customer's sites are doing, and what they owe.

Getting the app is /download, and so is signing up, so a guest arriving here
is sent there rather than shown a second front door. Everything this page can
actually do goes through erpera_admin.portal, which re-derives the caller from
the session.
"""

import frappe
from frappe.sessions import get_csrf_token
from frappe.utils import cint

no_cache = 1


def get_context(context):
	if frappe.session.user in ("Guest", None, ""):
		# One door. /download signs people in and hands over the app; this page
		# is only worth anything once there is an account behind it.
		frappe.local.flags.redirect_location = "/download"
		raise frappe.Redirect

	context.no_header = 1
	context.no_breadcrumbs = 1
	context.no_sidebar = 1
	context.title = "Your AI Assist account"

	s = frappe.get_cached_doc("ERPERA AI Settings")
	signup = s.get("portal_signup_enabled")

	context.signup_allowed = True if signup is None else bool(cint(signup))
	context.trial_days = cint(s.get("trial_days")) or 15
	context.signed_in = frappe.session.user not in ("Guest", None, "")
	context.session_user = frappe.session.user

	if context.signed_in:
		# Website pages do not boot the Desk, so nothing has asked the session
		# for a CSRF token and it has none. Frappe's check passes a request
		# whose session holds no token, which would leave rotate_key reachable
		# from any other site the customer happens to have open, and one hidden
		# form and their assistant goes quiet. Asking for the token here both
		# puts it on the page and switches the check on for this session.
		get_csrf_token()

	return context
