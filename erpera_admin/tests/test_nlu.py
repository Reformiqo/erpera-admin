"""Regression tests for the deterministic intent matcher.

The matcher is tuned by adding phrasings to nlu_samples.SAMPLES, so the property
that matters most is that teaching it a phrasing cannot make it worse. An earlier
version merged every phrasing of an intent into one BM25 document, where length
normalisation meant a new sample LOWERED its own intent's score — these tests
exist so that regression cannot come back unnoticed.
"""

import unittest

from erpera_admin.brain import nlu
from erpera_admin.brain.nlu_samples import SAMPLES

# Accuracy on the samples themselves. Not 100%: a few intents are genuine
# near-synonyms ("supplier dues" fits both outstanding_payables and
# supplier_payments_due), and forcing those apart would only overfit. This is a
# floor — it should go UP as phrasings are added, never down.
MIN_SELF_ACCURACY = 0.85


class TestIntentMatcher(unittest.TestCase):
	def test_samples_resolve_to_their_own_intent(self):
		total = correct = 0
		misses = []
		for qid, phrasings in SAMPLES.items():
			for phrase in phrasings:
				total += 1
				m = nlu.match(phrase)
				if m["id"] == qid and m["confident"]:
					correct += 1
				else:
					misses.append(f"{phrase!r} -> {m['id']} (wanted {qid})")
		self.assertGreaterEqual(
			correct / total, MIN_SELF_ACCURACY,
			f"only {correct}/{total} samples matched their own intent:\n  "
			+ "\n  ".join(misses[:15]),
		)

	def test_adding_a_phrasing_does_not_demote_its_intent(self):
		"""The core property: a sample must only ever help its own intent."""
		qid = "outstanding_receivables"
		probe = "what is our total outstanding from customers"
		before = dict(nlu.rank(probe, limit=20))

		extra = {k: list(v) for k, v in SAMPLES.items()}
		extra[qid] = extra[qid] + ["the outstanding we must still collect from buyers"]
		nlu.index(samples=extra, rebuild=True)
		try:
			after = dict(nlu.rank(probe, limit=20))
			self.assertGreaterEqual(
				after.get(qid, 0), before.get(qid, 0) - 1e-9,
				"adding a phrasing lowered its own intent's score",
			)
		finally:
			nlu.index(rebuild=True)

	def test_unseen_phrasings_route_correctly(self):
		for phrase, want in [
			("what is our total outstanding from customers", "outstanding_receivables"),
			("how much do customers owe us", "outstanding_receivables"),
			("who owes us money", "outstanding_receivables"),
			("receivable aging", "receivable_aging_report"),
			("how many customers do we have", "customer_list"),
			("profit and loss", "profit_and_loss"),
		]:
			m = nlu.match(phrase)
			self.assertTrue(m["confident"], f"{phrase!r} was not confident")
			self.assertEqual(m["id"], want, f"{phrase!r} -> {m['id']}")

	def test_gibberish_is_not_confident(self):
		for phrase in ["asdkjh qwe", "hello there", ""]:
			self.assertFalse(nlu.match(phrase)["confident"], phrase)


class TestQualifiedQuestionGuard(unittest.TestCase):
	"""A canned registry answer must not stand in for a question it cannot answer."""

	def test_qualified_question_on_a_plain_intent_goes_to_the_agent(self):
		for phrase in [
			"which 3 customers have the biggest outstanding balance",
			"outstanding broken down by customer",
			"customers who owe more than 100000",
		]:
			m = nlu.match(phrase)
			self.assertTrue(nlu.needs_agent(phrase, m["id"]), phrase)

	def test_ranking_intents_still_answer_instantly(self):
		for phrase in ["top customers", "top 5 customers by sales", "biggest customers"]:
			m = nlu.match(phrase)
			self.assertFalse(nlu.needs_agent(phrase, m["id"]), phrase)

	def test_plain_questions_are_unaffected(self):
		for phrase in ["who owes us money", "total outstanding", "how many customers do we have"]:
			self.assertFalse(nlu.needs_agent(phrase, nlu.match(phrase)["id"]), phrase)

	def test_no_canned_sample_is_diverted_from_its_own_intent(self):
		diverted = [
			(qid, p) for qid, phrasings in SAMPLES.items()
			for p in phrasings if nlu.needs_agent(p, qid)
		]
		self.assertEqual(diverted, [], f"samples diverted to the agent: {diverted[:5]}")


class TestWriteRequestGuard(unittest.TestCase):
	"""The registry only reads — a request to create must never be answered by it."""

	def test_creation_requests_are_detected(self):
		for phrase in [
			"can you create a customer name rio",
			"create a customer called Rio",
			"add a new supplier Momodou",
			"make a payment entry for 5000",
			"draft an invoice for ACME",
			"update the customer phone number",
		]:
			self.assertTrue(nlu.is_write_request(phrase), phrase)

	def test_read_questions_are_not_mistaken_for_writes(self):
		# These read like writes but are not: the verb is a noun or a modifier.
		for phrase in ["salary register", "leave applications to approve", "cost of making an item"]:
			self.assertFalse(nlu.is_write_request(phrase), phrase)

	def test_no_canned_sample_looks_like_a_write(self):
		wrong = [p for v in SAMPLES.values() for p in v if nlu.is_write_request(p)]
		self.assertEqual(wrong, [], f"read samples flagged as writes: {wrong}")


class TestQualifiedDetection(unittest.TestCase):
	def test_is_qualified(self):
		for phrase in ["which 2 customers owe us the most", "top 5 items", "sales by month"]:
			self.assertTrue(nlu.is_qualified(phrase), phrase)
		for phrase in ["who owes us money", "how many customers do we have"]:
			self.assertFalse(nlu.is_qualified(phrase), phrase)
