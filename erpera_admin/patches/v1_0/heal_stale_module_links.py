# Copyright (c) 2026, The Tiny App Company and contributors
# For license information, please see license.txt

"""Repoint anything left behind by the TTAC module rename.

A row that names a Module Def which no longer exists is not a cosmetic
leftover. `get_workspace_sidebar_items` throws `DoesNotExistError` on it while
building the desk sidebar, and that happens inside the session boot, so the
whole of /app answers 500 for every user on the site and the traceback names
frappe rather than us.

The rename patch swept these, but only on the branch where the old Module Def
was still there to be renamed, so a site that arrived at the new name by any
other route kept its dangling rows and re-running the patch could never fix
them. That branch is fixed; this exists because the patch has already run on
the sites that need it, and a logged patch does not run again.

Safe to run repeatedly, and finds nothing on a healthy site.
"""

import frappe

from erpera_admin.patches.v1_0.rename_erpera_to_ttac import (
	MODULE_NEW,
	MODULE_OLD,
	_repoint_module,
)


def execute():
	if not frappe.db.exists("Module Def", MODULE_NEW):
		# The rename never happened here, so there is nothing stale to repoint
		# and the old name is still the right answer.
		return

	_repoint_module()
	frappe.db.delete("Module Def", {"name": MODULE_OLD})
	frappe.db.commit()
	frappe.clear_cache()
