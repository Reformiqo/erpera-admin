# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

"""The page a customer downloads the app from.

This is the only thing an account is for now. Nothing else here gates
anything: once the app is on their bench, the site registers itself and
collects its own key without a person reading one.

Reachable by guests on purpose, because signing up is the first step.
"""

import frappe
from frappe.sessions import get_csrf_token
from frappe.utils import cint

no_cache = 1


def get_context(context):
	context.no_header = 1
	context.no_breadcrumbs = 1
	context.no_sidebar = 1
	context.title = "Install AI Assist"

	s = frappe.get_cached_doc("ERPERA AI Settings")
	signup = s.get("portal_signup_enabled")

	context.signup_allowed = True if signup is None else bool(cint(signup))
	context.trial_days = cint(s.get("trial_days")) or 15
	context.signed_in = frappe.session.user not in ("Guest", None, "")
	context.session_user = frappe.session.user

	if context.signed_in:
		# Website pages do not boot the Desk, so nothing has asked the session
		# for a CSRF token and it has none. Asking for it here both puts it on
		# the page and switches Frappe's check on for this session.
		get_csrf_token()

	return context
