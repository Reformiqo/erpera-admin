# Copyright (c) 2026, The Tiny App Company and contributors
# For license information, please see license.txt

"""The answer that costs nothing, decided before any model is asked.

A large share of what people type has a known answer: outstanding receivables,
stock balance, today's sales. Matching those deterministically and running the
registry query is free, sub-second, and cannot invent a figure. It also keeps
working when the provider is rate-limited or down, which is exactly when a
person most wants a straight number.

So this runs first, on every fresh question, and only hands over to the model
when it is not sure. Three things make it hand over: the match was not
confident, the phrasing needs reasoning the registry cannot do, or the sentence
asks to create something rather than to look.
"""

import frappe

from erpera_admin.brain import nlu
from erpera_admin.brain import queries as qq


def answer(site, message, company=None, number_format=None):
	"""A finished reply, or None to let the agent run."""
	msg = (message or "").strip()
	if not msg:
		return None
	m = nlu.match(msg)
	if not m["confident"] or nlu.needs_agent(msg, m["id"]) or nlu.is_write_request(msg):
		return None
	q = qq.QUERIES.get(m["id"]) or {}
	try:
		res = qq.execute(site, m["id"], company, number_format=number_format)
	except Exception:
		return None  # never let the fast path break a question the agent could answer

	# A qualified question that produced nothing almost always means the match
	# was topically wrong — "which 2 customers owe us the most" landing on Top
	# Customers (by sales) returns "0 groups, ₹0.00", which reads as an answer
	# and is not one. Plain questions keep their legitimate empty results.
	if not res.get("rows") and not res.get("total_count") and nlu.is_qualified(msg):
		return None

	reply = f"**{res.get('title') or q.get('label') or ''}**: {res.get('summary') or ''}".strip(" :")
	actions = []
	if q.get("doctype"):
		filters = {}
		for f in q.get("filters") or []:
			if isinstance(f, (list, tuple)) and len(f) == 3:
				filters[f[0]] = [f[1], f[2]] if f[1] != "=" else f[2]
		actions.append({"type": "list", "doctype": q["doctype"], "filters": filters})
	return {
		"reply": reply or "Here you go.",
		"state": {"phase": "idle"},
		"history": [],
		"actions": actions,
		"instant": True,
		"result": res,
	}


def basic(site, message, company=None, number_format=None):
	"""The whole of Basic mode: run it, offer a choice, or admit the miss.

	Three outcomes, and the middle one matters: a confident match runs the
	query, a near-miss offers the two or three intents it was choosing between
	rather than guessing, and a genuine miss says so and suggests.

	The wording lives here rather than on the customer's site because it is the
	visible half of the matcher: which near misses are worth offering, and in
	what order, is the product.
	"""
	m = nlu.match(message or "")
	if m["confident"]:
		res = qq.execute(site, m["id"], company, number_format=number_format)
		return {
			"mode": "basic",
			"result": res,
			"query_id": m["id"],
			"suggestions": qq.suggestion_meta(res.get("followUps") or []),
			"matched": {"id": m["id"], "score": m["score"]},
		}

	def _label(qid):
		q = qq.QUERIES.get(qid)
		return q["label"] if q else qid

	near = [q for q in ([m["id"]] + m["alternatives"]) if q][:3]
	if near and m["score"] >= nlu.MIN_SCORE * 0.6:
		return {
			"mode": "basic",
			"answer": "Did you mean " + " or ".join(_label(q) for q in near) + "?",
			"suggestions": qq.suggestion_meta(near),
		}

	options = ", ".join(sorted({q["label"] for q in list(qq.QUERIES.values())[:12]}))
	return {
		"mode": "basic",
		"answer": "I couldn't match that to a quick query. Try one from the left panel, for example: "
		          + options + ".",
	}
