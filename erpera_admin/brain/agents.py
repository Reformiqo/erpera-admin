# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

"""Who the assistant can be, decided here.

These used to be rows in a doctype on the customer's own site, which meant the
instructions that shape every answer sat on a disk whose Administrator is the
customer, and a System Manager could add a seventh agent, retune the sixth, or
quietly delete the sentence that keeps an operator from submitting. All of that
was theirs to change and none of it should have been.

So there are two agents, they are defined in this file, and a site cannot add a
third. A tenant may still *narrow* what an agent does, never widen it: WhatsApp
has no Desk to navigate, and a contact that is not trusted to create documents
does not get to. Both of those take something away. Nothing a site sends can
hand an agent a permission this file did not already give it.
"""

import frappe

ANALYST = "analyst"
OPERATOR = "operator"

AGENTS = [
	{
		"agent_id": ANALYST,
		"label": "Analyst",
		"sort_order": 10,
		"description": "Answers questions about your data and opens the right lists and reports.",
		"allow_reads": 1,
		"allow_navigation": 1,
		"allow_drafts": 0,
		"write_doctypes": [],
		"instructions": """SPECIALTY: business analysis and navigation.
- Answer with real numbers from the tools; prefer opening a filtered list/report over pasting big tables.
- For money questions use run_report (Accounts Receivable, General Ledger, Profit and Loss Statement).""",
	},
	{
		"agent_id": OPERATOR,
		"label": "Operator",
		"sort_order": 20,
		"description": "Creates documents by chat, always as a draft you approve.",
		"allow_reads": 1,
		"allow_navigation": 1,
		"allow_drafts": 1,
		"write_doctypes": [],
		"instructions": """SPECIALTY: creating and managing documents (invoices, payments, orders, stock entries, masters).
- Follow the draft workflow strictly: create_record makes a DRAFT; the user submits.
- For payments, surface the party's open invoices and ask which to settle (or on account) before creating.""",
	},
]

_BY_ID = {a["agent_id"]: a for a in AGENTS}

# What a picker needs to draw itself. Deliberately not the instructions: the
# customer chooses an agent, they do not get to read how it thinks.
PUBLIC_FIELDS = ("agent_id", "label", "description", "allow_drafts", "sort_order")


def catalogue():
	"""The agents a site may choose from, in the order they should be shown."""
	return [{k: a[k] for k in PUBLIC_FIELDS}
	        for a in sorted(AGENTS, key=lambda a: a["sort_order"])]


def config(agent_id, channel=None, deny_drafts=False):
	"""The full working config for one agent, after any narrowing.

	An unknown id falls back to the Analyst rather than erroring, because an
	old tenant asking for an agent we have retired should still get an answer,
	and the Analyst is the one that can do the least.
	"""
	a = _BY_ID.get((agent_id or "").strip().lower()) or _BY_ID[ANALYST]
	out = {
		"label": a["label"],
		"instructions": a["instructions"],
		"allow_reads": a["allow_reads"],
		"allow_navigation": a["allow_navigation"],
		"allow_drafts": a["allow_drafts"],
		"write_doctypes": list(a["write_doctypes"]),
	}
	# Off-Desk channels have no page to open, so navigation is not a choice the
	# tenant makes, it is a fact about where the answer is going.
	if channel and str(channel).strip().lower() not in ("desk", "web", "app"):
		out["allow_navigation"] = 0
	if deny_drafts:
		out["allow_drafts"] = 0
	return out


def allows_drafts(agent_id):
	a = _BY_ID.get((agent_id or "").strip().lower())
	return bool(a and a["allow_drafts"])
