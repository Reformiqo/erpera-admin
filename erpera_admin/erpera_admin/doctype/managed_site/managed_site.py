# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

import secrets

import frappe
from frappe.model.document import Document


class ManagedSite(Document):
	def before_insert(self):
		# A site with no token could never authenticate, and a blank token must
		# never be allowed to match a blank one sent by a stranger.
		if not self.get("token"):
			self.token = secrets.token_urlsafe(32)

	@frappe.whitelist()
	def rotate_token(self):
		"""Issue a new token. The old one stops working the moment this saves."""
		self.token = secrets.token_urlsafe(32)
		self.save()
		return self.get_password("token")
