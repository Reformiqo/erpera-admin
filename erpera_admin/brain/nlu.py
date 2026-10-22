# Deterministic intent matching, on the control plane.
#
# Deterministic intent matching — no model, no API call, no tokens.
#
# The job: map "how much does Kushal owe us", "outstanding from customers",
# "who hasn't paid" and "receivables please" onto the same registry entry. The
# previous matcher counted how many words appeared in a query's label, which
# needed two hits to fire — so "how many customers do we have?" scored 1 and
# matched nothing at all.
#
# The approach is ordinary information retrieval: normalise and stem the words,
# expand them through a domain lexicon so "owe" reaches "receivable", then score
# each query by IDF-weighted overlap against its label plus a set of real sample
# phrasings. Rare words ("birthday") count for far more than common ones
# ("report"), which is what stops everything collapsing onto one popular entry.

import math
import re

# ── normalisation ────────────────────────────────────────────────────────────

STOPWORDS = {
	"a", "about", "all", "am", "an", "and", "any", "are", "as", "at", "be", "been",
	"by", "can", "could", "did", "do", "does", "for", "from", "get", "give", "got",
	"has", "have", "how", "i", "if", "in", "is", "it", "its", "know", "let", "like", "me", "much", "my", "of", "on", "or", "our", "out", "please", "pls",
	"see", "should", "show", "so", "some", "tell", "that", "the", "their", "them",
	"there", "these", "they", "this", "to", "us", "want", "was", "we", "were",
	"what", "when", "where", "which", "who", "will", "with", "would", "you", "your",
}

# Deliberately NOT stopwords, because they carry intent here: many, total, top,
# open, due, late, today, month, pending, now.


def _stem(w):
	"""Crude, predictable suffix stripping.

	Consistency matters more than linguistic correctness: the same rule runs
	over both the query documents and the user's message, so "boxe" is a fine
	stem as long as "boxes" always reaches it. An earlier version stripped
	"es" wholesale, which took "quotes" to "quot" while "quotation" stayed put
	— the synonym that was supposed to join them never fired.
	"""
	if w.endswith("ies") and len(w) > 4:
		return w[:-3] + "y"
	if w.endswith("ing") and len(w) > 5:
		return w[:-3]
	if w.endswith("ed") and len(w) > 4:
		return w[:-2]
	if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
		return w[:-1]
	return w


# ── domain lexicon ───────────────────────────────────────────────────────────
# Each group collapses to its first member. This is where "grammar" actually
# gets handled: people say owe / owing / unpaid / pending, and mean receivable.

SYNONYM_GROUPS = [
	("receivable", "owe", "owing", "owed", "unpaid", "outstanding", "due", "debtor", "collect", "collection"),
	("payable", "creditor", "payout"),
	("customer", "client", "buyer", "account", "custom"),
	("supplier", "vendor", "seller", "purchaser"),
	("sale", "sell", "sold", "revenue", "turnover", "income", "billing"),
	("purchase", "buy", "bought", "buying", "procurement", "spend", "spending"),
	("stock", "inventory", "quantity", "qty", "onhand", "warehouse", "availability", "available"),
	("invoice", "bill", "billed"),
	("payment", "paid", "pay", "remittance", "settle"),
	("employee", "staff", "worker", "people", "headcount", "personnel", "team"),
	("overdue", "late", "delayed", "past", "expired", "arrear"),
	("profit", "loss", "pnl", "margin", "earning", "bottomline"),
	("bank", "cash", "fund", "liquidity", "treasury"),
	("order", "po", "so"),
	("quotation", "quote", "estimate", "proposal"),
	("delivery", "deliver", "dispatch", "shipment", "shipped"),
	("count", "many", "number", "total", "how"),
	("top", "best", "highest", "biggest", "largest", "most"),
	("low", "least", "lowest", "minimum", "short", "shortage", "reorder",
	 "finish", "finishing", "exhaust", "depleted", "empty", "runout", "over"),
	("pending", "open", "awaiting", "unprocessed", "incomplete"),
	("today", "todays", "daily"),
	("month", "monthly", "mtd"),
	("item", "product", "sku", "good", "article"),
	("leave", "holiday", "absence", "timeoff", "vacation"),
	("attendance", "present", "checkin", "punch"),
	("salary", "payroll", "wage", "compensation"),
	("tax", "gst", "vat", "duty"),
	("ledger", "gl", "entry", "transaction", "posting"),
	("project", "job", "assignment"),
	("task", "todo", "activity"),
	("production", "manufacturing", "workorder", "wo"),
	("bom", "recipe", "formula"),
	("quality", "inspection", "qc"),
	("aging", "ageing", "bucket"),
	("territory", "region", "area", "zone"),
	("birthday", "bday", "born", "anniversary"),
	("department", "dept", "division"),
	("expense", "claim", "reimbursement"),
	("creditnote", "return", "refund"),
	("trial", "trialbalance"),
	("summary", "overview", "dashboard", "snapshot", "report"),
]

CANON = {}
for group in SYNONYM_GROUPS:
	head = _stem(group[0])
	for word in group:
		CANON[_stem(word)] = head


def tokens(text):
	"""Text -> the canonical, stemmed, stopword-free token set we score on."""
	raw = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()
	out = set()
	for w in raw:
		if w in STOPWORDS or len(w) < 2:
			continue
		s = _stem(w)
		out.add(CANON.get(s, s))
	return out


# ── the index ────────────────────────────────────────────────────────────────

_INDEX = None


def _build_index(queries, samples):
	"""Index each intent as a SET of short documents — one per sample phrasing.

	Not one document per intent. These samples are paraphrases of each other, not
	prose about a topic: a question matches if it looks like ANY ONE of them. With
	a single merged document BM25's length normalisation punished exactly the
	intents we had taught the most, so adding a phrasing could push its own intent
	below a rival's — the opposite of what a training set should do. Scoring each
	phrasing separately and keeping the best makes a new sample monotonically
	helpful, which is the property that lets us tune this by adding examples.
	"""
	docs, labels, variants, qualified = {}, {}, {}, {}
	for qid, q in queries.items():
		lab = tokens(" ".join([q.get("label", ""), qid.replace("_", " ")]))
		phrasings = samples.get(qid, [])
		rest = tokens(" ".join([q.get("department", ""), " ".join(phrasings)]))
		labels[qid] = lab
		docs[qid] = lab | rest          # kept for IDF and for callers that inspect it
		# The label rides along with every phrasing, so naming the intent is
		# still strong evidence however the rest of the sentence is worded.
		vs = [lab | tokens(p) for p in phrasings]
		dept = tokens(q.get("department", ""))
		if dept:
			vs.append(lab | dept)
		variants[qid] = vs or [lab]
		qualified[qid] = bool(
			_QUALIFIED.search(q.get("label", "") or "")
			or any(_QUALIFIED.search(p) for p in phrasings)
		)

	# IDF stays intent-level: a token in every intent tells us nothing, and that
	# is a property of the vocabulary, not of how many ways we spelled one intent.
	n = max(len(docs), 1)
	df = {}
	for toks in docs.values():
		for t in toks:
			df[t] = df.get(t, 0) + 1
	idf = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}
	all_vs = [v for vs in variants.values() for v in vs]
	avgdl = (sum(len(v) for v in all_vs) / len(all_vs)) if all_vs else 1.0
	return {"docs": docs, "labels": labels, "variants": variants,
		"qualified": qualified, "idf": idf, "avgdl": avgdl or 1.0}


def index(queries=None, samples=None, rebuild=False):
	global _INDEX
	if _INDEX is None or rebuild:
		from erpera_admin.brain import queries as qq
		from erpera_admin.brain.nlu_samples import SAMPLES
		_INDEX = _build_index(queries or qq.QUERIES, samples or SAMPLES)
	return _INDEX


# ── matching ─────────────────────────────────────────────────────────────────

LABEL_BOOST = 1.9

MIN_SCORE = 0.42      # below this we genuinely do not know
MIN_MARGIN = 0.08     # top must beat runner-up by this, else it is ambiguous


def rank(message, limit=3):
    """Score every query against the message with BM25.

    BM25 rather than plain overlap because document length matters here: an
    intent with three sample phrasings should not beat a well-described one
    just because a single hit covers more of its tiny vocabulary. `b` controls
    how hard length is normalised; k1 is fixed since our term frequencies are
    all 1 (documents are token sets).
    """
    idx = index()
    want = tokens(message)
    if not want:
        return []
    idf, labels, variants = idx["idf"], idx["labels"], idx["variants"]
    avgdl = idx["avgdl"]
    k1, b = 1.2, 0.5

    want_weight = sum(idf.get(t, 1.0) for t in want) or 1.0
    scored = []
    for qid, vs in variants.items():
        lab = labels.get(qid, set())
        best = 0.0
        for doc in vs:
            hit = want & doc
            if not hit:
                continue
            dl = len(doc) or 1
            norm = k1 * (1 - b + b * (dl / avgdl))
            # LABEL_BOOST: "Profit & Loss" naming the intent is far stronger
            # evidence than the same word buried in one of its sample phrasings.
            got = sum(
                idf.get(t, 1.0) * (LABEL_BOOST if t in lab else 1.0) * (k1 + 1) / (1 + norm)
                for t in hit
            )
            # Breadth bonus: explaining three words of the question is stronger
            # evidence than one heavily-weighted hit on the intent's name.
            got *= 1.0 + 0.18 * (len(hit) - 1)
            if got > best:
                best = got
        if best:
            scored.append((qid, best / want_weight))
    scored.sort(key=lambda x: -x[1])
    return scored[:limit]


# A registry intent answers exactly ONE canned question ("what is outstanding?").
# If the user qualified it — a ranking, a breakdown, a named entity, a custom
# period — the canned answer is not merely incomplete, it is wrong: asking "which
# 3 customers owe the most" and being told the company-wide total looks like an
# answer and is not one. These questions must go to the agent, which can group
# and filter. Erring toward the agent costs seconds; erring toward the registry
# costs trust.
_QUALIFIED = re.compile(
	r"\b(top|biggest|largest|highest|lowest|smallest|worst|best|most|least"
	r"|rank|ranked|ranking|breakdown|break down|split|group(ed)? by"
	r"|per (customer|supplier|item|month|week|day|branch|warehouse|company)"
	r"|by (customer|supplier|item|month|week|day|branch|warehouse|company)"
	r"|each|every|compare|comparison|versus|vs"
	r"|more than|less than|greater than|above|below|over|under"
	r"|which \d+|first \d+|last \d+|between)\b",
	re.I,
)


# Asking for something to be CREATED or CHANGED is never a registry question.
# The registry only reads, so matching "make a payment entry for 5000" to the
# "recent payments" query answers a different question entirely and looks like
# the work was done. Deliberately verb-led: "approve" and a bare "register" are
# excluded because they appear as nouns in read questions ("leave applications to
# approve", "salary register").
_WRITE_INTENT = re.compile(
	r"\b(create|creating|add|adding|make|draft|drafting|raise|issue"
	r"|enter|log a|record a|book a|post a|register (a|an|new)"
	r"|update|updating|change|changing|edit|rename|delete|remove|cancel|submit)\b",
	re.I,
)


def is_write_request(message):
	"""True when the user is asking for something to be created or changed."""
	return bool(_WRITE_INTENT.search(message or ""))


def is_qualified(message):
	"""True when the message narrows, ranks or slices — regardless of intent."""
	return bool(_QUALIFIED.search(message or ""))


def needs_agent(message, intent_id=None):
	"""True when a message asks for more than the matched registry query can give.

	Intent-aware on purpose. Some intents ARE rankings ("Top Customers",
	"Revenue by Month") and a qualified question is exactly what they answer; the
	flag is derived from each intent's own phrasings rather than hand-listed, so
	a new ranking intent classifies itself. Only a qualified question that landed
	on a PLAIN intent is a mismatch worth paying the agent for.
	"""
	if not _QUALIFIED.search(message or ""):
		return False
	if intent_id and index()["qualified"].get(intent_id):
		return False
	return True


def match(message):
	"""Resolve a message to an intent.

	Returns {"id", "score", "confident", "alternatives"}. `confident` is only
	true when the best match clears MIN_SCORE *and* beats the runner-up by
	MIN_MARGIN — an ambiguous question should be asked about, not guessed at.
	"""
	scored = rank(message, limit=3)
	if not scored:
		return {"id": None, "score": 0.0, "confident": False, "alternatives": []}
	top_id, top_score = scored[0]
	runner = scored[1][1] if len(scored) > 1 else 0.0
	confident = top_score >= MIN_SCORE and (top_score - runner) >= MIN_MARGIN
	return {
		"id": top_id,
		"score": round(top_score, 3),
		"confident": confident,
		"alternatives": [q for q, _ in scored[1:]],
	}


def match_free_text(message):
	"""Back-compatible entry point: a query id, or None."""
	m = match(message)
	return m["id"] if m["confident"] else None
