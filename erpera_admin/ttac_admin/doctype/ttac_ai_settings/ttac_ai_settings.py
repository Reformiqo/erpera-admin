# Copyright (c) 2026, The Tiny App Company and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class TTACAISettings(Document):
	def validate(self):
		# Pointing every site at a disabled credential would take the whole
		# estate down at once, so refuse it here rather than at answer time.
		for field, label in (("active_provider", "Provider"), ("fallback_provider", "Fallback Provider")):
			name = self.get(field)
			if name and not frappe.db.get_value("AI Provider Credential", name, "enabled"):
				frappe.throw(_("{0} {1} is disabled. Enable it, or choose another.").format(label, name))

		if self.active_provider and self.active_provider == self.fallback_provider:
			frappe.throw(_("The fallback has to be a different credential, or it is not a fallback."))


def resolved_config():
	"""What a site should answer with: provider and model, never the key."""
	s = frappe.get_cached_doc("TTAC AI Settings")
	out = {
		"allow_site_override": int(s.allow_site_override or 0),
		"log_calls": int(s.log_calls or 0),
		"log_content": int(s.log_content or 0),
		"content_retention_days": int(s.content_retention_days or 0),
	}
	for slot, prov_field, model_field in (
		("primary", "active_provider", "active_model"),
		("fallback", "fallback_provider", "fallback_model"),
	):
		cred_name = s.get(prov_field)
		if not cred_name:
			out[slot] = None
			continue
		cred = frappe.get_cached_doc("AI Provider Credential", cred_name)
		out[slot] = {
			"provider": cred.provider,
			"model": s.get(model_field) or cred.default_model or "",
			"base_url": cred.base_url or "",
		}
	return out
