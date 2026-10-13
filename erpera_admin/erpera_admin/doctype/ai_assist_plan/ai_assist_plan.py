# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class AIAssistPlan(Document):
	def validate(self):
		self.currency = (self.currency or "INR").upper()[:3]
