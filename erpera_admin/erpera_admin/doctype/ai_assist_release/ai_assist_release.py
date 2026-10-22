# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

"""A version of the app, and the file a customer downloads to install it.

The size and checksum are measured here rather than typed, because a release
whose stated checksum does not match the bytes being served is worse than one
with no checksum at all.
"""

import hashlib
import os

import frappe
from frappe import _
from frappe.model.document import Document


class AIAssistRelease(Document):
	def validate(self):
		self._measure()

	def on_update(self):
		if self.is_current:
			self._become_the_only_current()

	def _path(self):
		"""Where the attached package actually sits on disk."""
		if not self.package:
			return None
		url = self.package
		if url.startswith("/private/files/"):
			return frappe.get_site_path("private", "files", os.path.basename(url))
		if url.startswith("/files/"):
			return frappe.get_site_path("public", "files", os.path.basename(url))
		return None

	def _measure(self):
		"""Read the bytes and record what they are."""
		path = self._path()
		if not path or not os.path.exists(path):
			self.size_bytes = 0
			self.checksum = None
			return

		h = hashlib.sha256()
		size = 0
		with open(path, "rb") as f:
			while True:
				chunk = f.read(1024 * 1024)
				if not chunk:
					break
				size += len(chunk)
				h.update(chunk)

		self.size_bytes = size
		self.checksum = h.hexdigest()

	def _become_the_only_current(self):
		"""Exactly one release is on offer. Two would be a coin toss."""
		others = frappe.get_all(
			"AI Assist Release",
			filters={"is_current": 1, "name": ["!=", self.name]},
			pluck="name",
		)
		for name in others:
			frappe.db.set_value("AI Assist Release", name, "is_current", 0,
			                    update_modified=False)


def current():
	"""The release customers are offered, or None if nothing is published."""
	name = frappe.db.get_value("AI Assist Release", {"is_current": 1}, "name")
	if not name:
		return None
	return frappe.get_cached_doc("AI Assist Release", name)
