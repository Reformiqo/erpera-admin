# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

"""The page the app is downloaded from.

There is nothing to sign in to. A sign-up form in front of a download is a
toll gate, not a security control: it collects an email from people who want
the software and stops nobody. What decides whether a site may ask a question
is the licence check it makes once it is installed, and the site arranges that
for itself.
"""

import frappe
from frappe.utils import cint

no_cache = 1


def get_context(context):
	context.no_header = 1
	context.no_breadcrumbs = 1
	context.no_sidebar = 1
	context.title = "Install AI Assist"

	s = frappe.get_cached_doc("ERPERA AI Settings")
	context.trial_days = cint(s.get("trial_days")) or 15

	return context
