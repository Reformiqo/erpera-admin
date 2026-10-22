# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class PaymentGatewayCredential(Document):
	"""A gateway's keys, encrypted at rest rather than sitting in a config file.

	The mode is validated against the key itself rather than trusted, because
	the single most expensive mistake available here is a live key believed to
	be a test one. Razorpay says which it is in the key's own prefix, so there
	is no reason to take anyone's word for it.
	"""

	def validate(self):
		self.key_id = (self.key_id or "").strip()

		if self.gateway == "Razorpay":
			if self.key_id.startswith("rzp_live_"):
				actual = "Live"
			elif self.key_id.startswith("rzp_test_"):
				actual = "Test"
			else:
				frappe.throw(_("That does not look like a Razorpay key. "
				               "It should start with rzp_test_ or rzp_live_."))

			if actual != self.mode:
				frappe.throw(_("This is a {0} key but the mode says {1}. "
				               "Fix the mode before saving.").format(actual, self.mode))

	def on_update(self):
		# One active row per gateway, so there is never a question about which
		# key a payment was taken with.
		if self.is_active:
			frappe.db.sql(
				"""UPDATE `tabPayment Gateway Credential` SET is_active = 0
				   WHERE gateway = %s AND name != %s""", (self.gateway, self.name))
		frappe.cache().delete_value("erpera:gateway:razorpay")
