# Copyright (c) 2026, The Tiny App Company and contributors
# For license information, please see license.txt

"""Carry the vendor-side names over to TTAC.

The product is still ERPERA AI and still answers on erpera.io. What moves here
is only what the vendor sees: the module these doctypes live in, the settings
single, and the per-user credential child table.

This runs **before** the model sync on purpose. Renamed after it, migrate would
first create an empty TTAC AI Settings from the new JSON and leave every
setting stranded in the old one, which on a live control plane means the
provider, the model and the enrolment code all quietly vanish.
"""

import frappe

MODULE_OLD = "ERPERA Admin"
MODULE_NEW = "TTAC Admin"

DOCTYPES = (
	("ERPERA AI Settings", "TTAC AI Settings"),
	("ERPERA Site Credential", "TTAC Site Credential"),
)


def execute():
	_rename_module()
	for old, new in DOCTYPES:
		_rename_doctype(old, new)
	# A Single keeps its values in tabSingles keyed by doctype name, and
	# rename_doc does not move them: it rewrites values that *point at* a
	# renamed doc, not the column naming the single itself.
	frappe.db.sql(
		"update `tabSingles` set doctype = %s where doctype = %s",
		("TTAC AI Settings", "ERPERA AI Settings"))
	# Password fields live in __Auth, keyed by doctype name, and rename_doc
	# leaves them where they are. Left behind they are not corrupt, they are
	# simply unreachable, which is worse: the enrolment code reads back empty
	# and every site's stored API secret goes with it, so the control plane
	# stops being able to read any customer's data and says nothing about why.
	# Swept unconditionally so a site renamed by an earlier run is repaired.
	for old, new in DOCTYPES:
		frappe.db.sql("update `__Auth` set doctype = %s where doctype = %s", (new, old))
		# A Single's docname *is* its doctype name, so its __Auth row carries
		# the old name in both columns. A child row's name is a hash, so the
		# equality below only ever matches the single.
		frappe.db.sql("update `__Auth` set name = %s where name = %s", (new, old))
	frappe.db.commit()
	frappe.clear_cache()


def _rename_module():
	"""Move the Module Def row itself.

	Frappe refuses this through the ORM ("Only Custom Modules can be renamed"),
	and rightly: a module that belongs to an app is not a user's to rename. It
	is still ours. The row is a primary key with a small, knowable set of
	referrers, so it moves by hand and every referrer is swept after it.
	"""
	if not frappe.db.exists("Module Def", MODULE_OLD):
		return
	if not frappe.db.exists("Module Def", MODULE_NEW):
		frappe.db.sql(
			"update `tabModule Def` set name = %s, module_name = %s where name = %s",
			(MODULE_NEW, MODULE_NEW, MODULE_OLD))
	_repoint_module()
	# Only reachable if both existed, from a half-finished earlier run.
	frappe.delete_doc("Module Def", MODULE_OLD, force=True, ignore_missing=True)


def _repoint_module():
	"""Every Link that pointed at the old module now points at the new one."""
	seen = set()
	rows = frappe.db.sql("""
		select parent, fieldname from `tabDocField`
		where fieldtype = 'Link' and options = 'Module Def'
	""", as_dict=True)
	# The obvious ones are listed too, because a DocField row can be missing
	# for a column that is nonetheless there.
	for dt in ("DocType", "Page", "Report", "Workspace", "Dashboard",
	           "Dashboard Chart", "Number Card", "Print Format", "Web Form",
	           "Server Script", "Client Script", "Notification"):
		rows.append({"parent": dt, "fieldname": "module"})
	for r in rows:
		dt, fn = r["parent"], r["fieldname"]
		if (dt, fn) in seen:
			continue
		seen.add((dt, fn))
		if not frappe.db.table_exists(f"tab{dt}"):
			continue
		try:
			if not frappe.db.has_column(dt, fn):
				continue
			frappe.db.sql(f"update `tab{dt}` set `{fn}` = %s where `{fn}` = %s",
			              (MODULE_NEW, MODULE_OLD))
		except Exception:
			# A table that will not take the update is not a reason to abandon
			# the rename; the ones that matter are named above.
			continue


def _rename_doctype(old, new):
	if not frappe.db.exists("DocType", old):
		return
	if frappe.db.exists("DocType", new):
		# Already there. Nothing safe left to do automatically, and silently
		# dropping either side would be the wrong guess, so say so and stop.
		frappe.log_error(
			title="TTAC rename skipped",
			message=f"Both {old!r} and {new!r} exist; renamed by hand?")
		return
	frappe.rename_doc("DocType", old, new, force=True)
