# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class AICreditLedger(Document):
	"""One movement of credits.

	Written only through erpera_admin.credits, which holds the row lock that
	keeps balance_after honest. Nothing here edits a balance on its own.
	"""

	pass
