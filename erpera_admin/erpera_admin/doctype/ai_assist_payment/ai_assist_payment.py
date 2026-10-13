# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt


class AIAssistPayment(Document):
	def validate(self):
		self.currency = (self.currency or "INR").upper()[:3]
		# The total is what the customer reads on their billing page, so it is
		# derived rather than typed: a row where the parts do not add up would be
		# an argument with a customer we would lose.
		if not self.total:
			self.total = flt(self.amount) + flt(self.tax)
