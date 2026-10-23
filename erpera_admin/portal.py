# Copyright (c) 2026, The Tiny App Company and contributors
# For license information, please see license.txt

"""The customer's side of the control plane.

A key used to mean a message from us: we created a Managed Site by hand and sent
the key over. That works for three customers and falls over at thirty, so this
module lets a customer create the account, register their site and generate
their own key, without us in the loop.

Everything here runs as a Website User: someone with no Desk and no permission
on any doctype of ours. That is deliberate: the portal reaches our data only
through the methods below, and each one re-derives what the caller owns from
the session rather than trusting anything the browser sent. A site name in a
request argument is never taken as proof that the caller owns that site.

What a customer may see is also narrower than what a row holds. Which provider
answered, which model, and what it cost are ours; a customer sees their plan,
their seats, their expiry and how many questions they asked.
"""

import os
import re
import secrets

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import add_days, cint, escape_html, flt, getdate, now_datetime, today

# Trial defaults, overridable in TTAC AI Settings. The cap is the important
# one: without it a single sign-up could mint trial keys in a loop.
TRIAL_DAYS = 15
MAX_SITES = 3
DEFAULT_SEATS = 5
MAX_SEATS = 500

# A site name becomes a document name, so it has to be a plain hostname. We are
# strict here rather than clever: anything we cannot recognise is refused with
# an example, which is kinder than accepting it and failing to verify later.
HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{1,98}[a-z0-9])?$")

MIN_PASSWORD = 8

# Where someone is sent when the page cannot finish the job itself.
BILLING_EMAIL = "hello@thetinyappcompany.com"


# --------------------------------------------------------------------- helpers


def _settings():
	try:
		return frappe.get_cached_doc("TTAC AI Settings")
	except Exception:
		return frappe._dict()


def _signup_allowed():
	s = _settings()
	# Unset means on: a fresh install should not need a visit to a settings page
	# before the portal works.
	v = s.get("portal_signup_enabled")
	return True if v is None else bool(cint(v))


def _trial_days():
	return cint(_settings().get("trial_days")) or TRIAL_DAYS


def _max_sites():
	return cint(_settings().get("max_sites_per_account")) or MAX_SITES


def _default_seats():
	return cint(_settings().get("default_seats")) or DEFAULT_SEATS


def _me():
	"""The signed-in account, or refuse.

	Guest reaching a portal method is not an error worth a traceback. The page
	simply has a stale session, so this reads as a short sentence.
	"""
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.local.response["http_status_code"] = 401
		frappe.throw(_("Please sign in."), frappe.AuthenticationError)
	return user


def _owned(site):
	"""Return the caller's own Managed Site, or refuse.

	Ownership is read from the row, never from the request. A site that exists
	but belongs to someone else gives the same answer as a site that does not
	exist, so this cannot be used to find out who our customers are.
	"""
	me = _me()
	site = site if isinstance(site, str) else ""
	if not site:
		frappe.throw(_("Which site?"))

	row = frappe.db.get_value("Managed Site", site, ["name", "account"], as_dict=True)
	if not row or row.account != me:
		frappe.local.response["http_status_code"] = 404
		frappe.throw(_("No such site on this account."), frappe.DoesNotExistError)
	return row.name


def _normalise(site_name):
	"""Turn what someone pasted into a hostname.

	People paste the whole address out of the browser bar, with the scheme, a
	trailing slash, and sometimes /app on the end. Accepting all of that is a
	one-line kindness that removes most support mail.
	"""
	s = (site_name if isinstance(site_name, str) else "").strip().lower()
	s = re.sub(r"^[a-z]+://", "", s)
	s = s.split("/")[0]
	s = s.split("?")[0]
	s = s.split("@")[-1]          # someone pastes an email by mistake
	s = s.rstrip(".")
	if ":" in s:                  # a port is fine to drop; we never dial it
		s = s.split(":")[0]
	# The dot is the cheapest test that this is an address and not a stray word:
	# every site a customer can actually reach has one.
	if not s or "." not in s or not HOSTNAME.match(s):
		frappe.throw(_("That does not look like a site address. It should read like <b>acme.erpnext.com</b>."))
	return s


def _plan_label(row):
	"""What the customer should read, not what the field holds.

	It has to agree with the colour the badge is about to be painted, so the
	reasons a site has stopped come first: a red badge reading "Paid" is a
	puzzle. "Internal" is a word for us, not for them, and a trial that has
	started is more usefully described by the days it has left.
	"""
	expired = row.get("valid_until") and getdate(row["valid_until"]) < getdate()
	trial = row.get("plan") == "Trial"

	if not cint(row.get("enabled")):
		return "Suspended"
	if expired:
		return "Trial ended" if trial else "Expired"
	if trial:
		if row.get("valid_until"):
			left = (getdate(row["valid_until"]) - getdate()).days
			if left == 0:
				return "Trial, last day"
			return f"Trial, {left} day{'s' if left != 1 else ''} left"
		return "Trial"
	if row.get("plan") == "Internal":
		return "Active"
	return row.get("plan") or "Active"


def _state(row):
	"""One word for how the site is doing, for the badge."""
	if not cint(row.get("enabled")):
		return "suspended"
	if row.get("valid_until") and getdate(row["valid_until"]) < getdate():
		return "expired"
	if row.get("plan") == "Trial":
		return "trial"
	return "active"


def _site_payload(row):
	"""The shape the page renders. Nothing of ours is in here.

	Note what is missing: provider, model, cost, our margins, and the key
	itself. The key is handed over once at generation and otherwise only on an
	explicit reveal, so it is not sitting in every page load.
	"""
	return {
		"name": row.get("name"),
		"site_name": row.get("site_name"),
		"company_name": row.get("company_name"),
		"plan": row.get("plan"),
		"plan_label": _plan_label(row),
		"state": _state(row),
		"seats": cint(row.get("seats")),
		"valid_until": str(row["valid_until"]) if row.get("valid_until") else None,
		"enabled": bool(cint(row.get("enabled"))),
		"calls": cint(row.get("calls_received")),
		"last_seen": str(row["last_seen"]) if row.get("last_seen") else None,
		"has_key": bool(
			frappe.utils.password.get_decrypted_password(
				"Managed Site", row.get("name"), "token", raise_exception=False)
		),
	}


# ---------------------------------------------------------------------- account


@frappe.whitelist(allow_guest=True)
def me():
	"""Who is signed in, and what they own.

	Answers for Guest too, with signed_in false, so the page can render the
	sign-up form from the same call rather than guessing from a cookie.
	"""
	user = frappe.session.user
	if not user or user == "Guest":
		return {"signed_in": False, "signup_allowed": _signup_allowed()}

	rows = frappe.get_all(
		"Managed Site",
		filters={"account": user},
		fields=["name", "site_name", "company_name", "plan", "seats", "valid_until",
		        "enabled", "calls_received", "last_seen"],
		order_by="creation asc",
		limit_page_length=0,
		ignore_permissions=True,
	)
	full_name = frappe.db.get_value("User", user, "full_name")
	return {
		"signed_in": True,
		"user": user,
		"full_name": full_name,
		"sites": [_site_payload(r) for r in rows],
		"can_add": len(rows) < _max_sites(),
		"max_sites": _max_sites(),
		"trial_days": _trial_days(),
		"default_seats": _default_seats(),
	}


# Deliberately not whitelisted. Nobody signs up: the app is an open download
# and a site identifies itself with the key it collects on its own.
@rate_limit(limit=5, seconds=60 * 60)
def sign_up(email=None, full_name=None, password=None):
	"""Create an account and sign it in.

	The account is a Website User: no Desk, no permission on anything of ours.
	It exists only to own licences in this portal.

	There is no email round trip, because the account on its own is worth
	nothing: a key still has to be pasted into an ERPNext site the person
	already administers. The cap on sites per account is what keeps this from
	being a way to mint keys, not the mailbox.
	"""
	if not _signup_allowed():
		frappe.throw(_("Sign-up is closed at the moment. Write to us and we will set you up."))

	email = (email or "").strip().lower()
	full_name = (full_name or "").strip()
	password = password or ""

	if not email or not frappe.utils.validate_email_address(email):
		frappe.throw(_("Please give a working email address."))
	if not full_name:
		frappe.throw(_("Please give your name."))
	# A friendly length check first; the site's own password policy still runs
	# on insert and says more than this could about a weak one.
	if len(password) < MIN_PASSWORD:
		frappe.throw(_("Please use a password of at least {0} characters.").format(MIN_PASSWORD))

	if frappe.db.exists("User", email):
		# Says that the address is taken, which it has to for the person to know
		# what to do next, but nothing about what they own.
		frappe.throw(_("There is already an account for {0}. Sign in instead.").format(
			escape_html(email)))

	user = frappe.get_doc({
		"doctype": "User",
		"email": email,
		"first_name": escape_html(full_name),
		"enabled": 1,
		"new_password": password,
		"user_type": "Website User",
		"send_welcome_email": 0,
	})
	user.flags.ignore_permissions = True
	user.insert()
	frappe.db.commit()

	# Straight into the portal. Asking them to type the password they just chose
	# into a second form buys nothing.
	frappe.local.login_manager.login_as(user.name)
	return {"ok": True, "user": user.name}


# ------------------------------------------------------------------------ sites


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=10, seconds=60 * 60)
def register_site(site_name=None, company_name=None, seats=None):
	"""Register a site and hand back its key.

	This is the whole point of the portal: at the end of it the customer holds a
	working key that nobody typed out for them.
	"""
	me = _me()
	site_name = _normalise(site_name)

	mine = frappe.get_all("Managed Site", filters={"account": me},
	                      pluck="name", limit_page_length=0, ignore_permissions=True)
	if len(mine) >= _max_sites():
		frappe.throw(_("This account already has {0} sites. Write to us if you need more.").format(
			_max_sites()))

	if frappe.db.exists("Managed Site", site_name):
		# Says as little as it can. "You already registered that" would be the
		# friendlier sentence and would also let a stranger test for our
		# customers one hostname at a time. Nobody who belongs here needs it:
		# a site this account owns is already on the page in front of them.
		frappe.throw(_("We could not register <b>{0}</b>. If you already have a key for it, use "
		               "that one. If this looks wrong, write to {1} and we will sort it out."
		               ).format(escape_html(site_name), BILLING_EMAIL))

	seats = cint(seats) or _default_seats()
	seats = max(1, min(seats, MAX_SEATS))

	doc = frappe.get_doc({
		"doctype": "Managed Site",
		"site_name": site_name,
		"base_url": f"https://{site_name}",
		"enabled": 1,
		"plan": "Trial",
		"seats": seats,
		"valid_until": add_days(today(), _trial_days()),
		"account": me,
		"company_name": (company_name or "").strip()[:140] or None,
		"signed_up_on": now_datetime(),
		"notes": "Registered from the customer portal.",
	})
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	frappe.db.commit()

	return {
		"ok": True,
		"site": doc.name,
		"key": doc.get_password("token"),
		"valid_until": str(doc.valid_until),
		"trial_days": _trial_days(),
	}


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=20, seconds=60 * 60)
def update_site(site=None, seats=None, company_name=None):
	"""Change the seat count or the company on a site they own.

	Seats are here rather than in a support queue because they are what the
	price is made of: a customer who has hired three people should be able to
	say so and watch the number on the billing card move. The change is
	recorded, and Managed Site tracks its own history, so a later argument about
	an invoice has something to read.
	"""
	name = _owned(site)
	values = {}

	if seats is not None and str(seats).strip() != "":
		n = cint(seats)
		if n < 1:
			frappe.throw(_("A site needs at least one seat."))
		values["seats"] = min(n, MAX_SEATS)

	if company_name is not None:
		values["company_name"] = (str(company_name).strip()[:140] or None)

	if not values:
		frappe.throw(_("Nothing to change."))

	doc = frappe.get_doc("Managed Site", name)
	doc.update(values)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=5, seconds=60 * 60)
def change_password(current_password=None, new_password=None):
	"""Change the signed-in account's own password.

	The current one is required, so someone who walks up to an unlocked browser
	cannot lock the owner out of their own licences.
	"""
	me = _me()
	current_password = current_password or ""
	new_password = new_password or ""

	from frappe.utils.password import check_password

	try:
		check_password(me, current_password)
	except frappe.AuthenticationError:
		# Deliberately not re-raised as AuthenticationError: Frappe answers that
		# with a 401 and clears the session cookie, so a typo in this one field
		# would sign the person out of the page they are standing on.
		frappe.throw(_("That is not your current password."))

	if len(new_password) < MIN_PASSWORD:
		frappe.throw(_("Please use a password of at least {0} characters.").format(MIN_PASSWORD))
	if new_password == current_password:
		frappe.throw(_("That is the password you already have."))

	# Through the document rather than update_password(), so the site's own
	# password policy gets a say in the new one just as it did at sign-up.
	user = frappe.get_doc("User", me)
	user.new_password = new_password
	user.flags.ignore_permissions = True
	user.save(ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist()
@rate_limit(limit=30, seconds=60 * 60)
def reveal_key(site=None):
	"""Show the key again.

	Keys are usually write-once for good reasons, but the alternative here is a
	customer who has lost theirs regenerating and taking their own site down
	for the minute it takes to paste the new one in. They own it; they may read
	it.
	"""
	name = _owned(site)
	key = frappe.utils.password.get_decrypted_password(
		"Managed Site", name, "token", raise_exception=False)
	if not key:
		frappe.throw(_("This site has no key yet. Generate one."))
	return {"ok": True, "key": key}


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=10, seconds=60 * 60)
def rotate_key(site=None):
	"""Issue a new key, which stops the old one immediately."""
	name = _owned(site)
	doc = frappe.get_doc("Managed Site", name)
	doc.token = secrets.token_urlsafe(32)
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	# The sites cache a yes for an hour, so the old key keeps working until that
	# lapses. Saying so is better than letting them think the rotation failed.
	return {"ok": True, "key": doc.get_password("token")}

# --------------------------------------------------------------------- download

@frappe.whitelist(allow_guest=True)
@rate_limit(limit=120, seconds=60 * 60)
def release():
	"""What to download and what to do with it.

	Open. There is no account to make and nothing to sign in to: a sign-up
	form in front of a download is a toll gate, not a security control, and
	the thing that actually decides whether a site may ask a question is the
	licence check it makes after it is installed.
	"""
	from erpera_admin.ttac_admin.doctype.ai_assist_release.ai_assist_release import current

	rel = current()
	if not rel:
		return {"ok": False, "reason": "no_release",
		        "message": _("No release is published yet. Please check back shortly.")}

	return {
		"ok": True,
		"version": rel.version,
		"released_on": rel.released_on,
		"size_bytes": rel.size_bytes,
		"checksum": rel.checksum,
		"notes": rel.notes,
		"url": "/api/method/erpera_admin.portal.download",
		"filename": f"ai_assist-{rel.version}.tar.gz",
		"trial_days": _trial_days(),
		# Where this installation lives, taken from the address they are
		# reading this on. The package ships with a default that suits our own
		# bench and nobody else's, so the install pins the real one.
		"control_url": (frappe.utils.get_url() or "").rstrip("/"),
	}


@frappe.whitelist(allow_guest=True)
@rate_limit(limit=30, seconds=60 * 60)
def download():
	"""Hand over the package itself.

	Served from here rather than as a plain file URL so the release stays one
	addressable thing: publish a new one and this hands over the new one, with
	no stale link anywhere to go wrong.
	"""
	from erpera_admin.ttac_admin.doctype.ai_assist_release.ai_assist_release import current

	rel = current()
	if not rel:
		frappe.throw(_("No release is published yet."))

	path = rel._path()
	if not path or not os.path.exists(path):
		frappe.log_error(title="AI Assist release file missing",
		                 message=f"{rel.name} points at {rel.package}")
		frappe.throw(_("The download is temporarily unavailable. Please contact support."))

	with open(path, "rb") as f:
		content = f.read()

	frappe.local.response.filename = f"ai_assist-{rel.version}.tar.gz"
	frappe.local.response.filecontent = content
	frappe.local.response.type = "download"
	frappe.local.response.display_content_as = "attachment"


# ---------------------------------------------------------------------- billing


def _money(value, currency="INR", trim=False):
	"""A number the customer reads, grouped the way they count.

	Indian grouping is not a formatting preference here. ₹14,82,650 and
	₹1,482,650 are read at different speeds by the people buying this.

	`trim` drops the paise from a round number. A headline price reads as ₹720;
	an amount someone actually paid keeps its decimals, because that is what the
	invoice says.
	"""
	v = flt(value)
	precision = 0 if (trim and v == int(v)) else None
	try:
		if precision is None:
			return frappe.utils.fmt_money(v, currency=currency)
		return frappe.utils.fmt_money(v, precision=precision, currency=currency)
	except Exception:
		return f"{v:,.2f}"


@frappe.whitelist()
def billing():
	"""What this costs, and what has been paid.

	Prices are read from AI Assist Plan rather than written into this file, so
	changing one is an edit in the admin panel and not a deployment.
	"""
	me = _me()

	seats = 0
	for row in frappe.get_all("Managed Site", filters={"account": me},
	                          fields=["seats"], limit_page_length=0, ignore_permissions=True):
		seats += cint(row.get("seats"))

	plans = []
	for p in frappe.get_all(
		"AI Assist Plan",
		filters={"is_active": 1},
		fields=["name", "plan_name", "tagline", "price_per_seat", "currency", "period",
		        "gst_percent", "payment_url", "includes", "highlight", "sort_order"],
		order_by="sort_order asc, plan_name asc",
		limit_page_length=0,
		ignore_permissions=True,
	):
		rate = flt(p.price_per_seat)
		cur = p.currency or "INR"
		net = rate * seats
		tax = net * flt(p.gst_percent) / 100.0
		plans.append({
			"name": p.name,
			"plan_name": p.plan_name,
			"tagline": p.tagline,
			"period": p.period or "Year",
			"currency": cur,
			"gst_percent": flt(p.gst_percent),
			"rate": rate,
			"rate_text": _money(rate, cur, trim=True),
			"highlight": bool(cint(p.highlight)),
			"includes": [ln.strip() for ln in (p.includes or "").splitlines() if ln.strip()],
			"payment_url": p.payment_url or "",
			"your_net": _money(net, cur, trim=True) if seats else None,
			"your_total": _money(net + tax, cur, trim=True) if seats else None,
		})

	payments = []
	for r in frappe.get_all(
		"AI Assist Payment",
		filters={"account": me},
		# Deliberately not `notes`: that field is where we write to each other.
		fields=["name", "paid_on", "period_start", "period_end", "site", "plan", "seats",
		        "amount", "tax", "total", "currency", "status", "invoice_no",
		        "invoice_url", "method"],
		order_by="paid_on desc, creation desc",
		limit_page_length=50,
		ignore_permissions=True,
	):
		cur = r.currency or "INR"
		payments.append({
			"name": r.name,
			"paid_on": str(r.paid_on) if r.paid_on else None,
			"period_start": str(r.period_start) if r.period_start else None,
			"period_end": str(r.period_end) if r.period_end else None,
			"site": r.site,
			"plan": r.plan,
			"seats": cint(r.seats),
			"total_text": _money(r.total, cur),
			"status": r.status or "Paid",
			"invoice_no": r.invoice_no,
			"invoice_url": r.invoice_url or "",
			"method": r.method,
		})

	return {"seats": seats, "plans": plans, "payments": payments,
	        "billing_email": BILLING_EMAIL}


# ---------------------------------------------------------------- credits

@frappe.whitelist()
def credit_calculator(site=None):
	"""What the purchase page needs to draw itself."""
	from erpera_admin import credits as credits_mod

	name = _owned(site)
	p = credits_mod.pricing()
	bal = credits_mod.balance(name)
	return {
		"pricing": p,
		"balance": bal["credits"],
		"valid_upto": bal["valid_upto"],
		"expired": bal["expired"],
	}


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=20, seconds=60 * 60)
def create_credit_order(site=None, credits=None):
	"""Open a Razorpay order for a credit purchase.

	The amount is quoted here and nowhere else. The browser says how many
	credits it wants; what they cost is not its business to decide.
	"""
	from erpera_admin import credits as credits_mod, gateway

	name = _owned(site)
	q = credits_mod.quote(credits)

	payment = frappe.get_doc({
		"doctype": "AI Assist Payment",
		"account": _me(),
		"site": name,
		"credits": q["credits"],
		"status": "Pending",
		"amount": q["net_total"],
		"tax": q["tax"],
		"total": q["grand_total"],
		"currency": "INR",
		"method": "Razorpay",
	}).insert(ignore_permissions=True)

	order = gateway.create_order(
		q["grand_total"], receipt=payment.name,
		notes={"site": name, "credits": str(q["credits"]), "payment": payment.name})

	payment.db_set("reference", order["order_id"], update_modified=False)
	frappe.db.commit()

	return {"order": order, "quote": q, "payment": payment.name}


@frappe.whitelist(methods=["POST"])
def confirm_credit_payment(razorpay_order_id=None, razorpay_payment_id=None,
                           razorpay_signature=None):
	"""Turn a finished Razorpay payment into credits.

	Three things are checked before a single credit is granted: the signature,
	which only Razorpay could have produced; the payment's own status fetched
	from Razorpay rather than reported by the browser; and the amount, against
	what we asked for. Then the record is flipped from Pending to Paid under a
	row lock, so a double callback cannot grant twice.
	"""
	from erpera_admin import credits as credits_mod, gateway

	if not gateway.verify_signature(razorpay_order_id, razorpay_payment_id,
	                                razorpay_signature):
		frappe.throw(_("This payment could not be verified."))

	row = frappe.db.get_value("AI Assist Payment", {"reference": razorpay_order_id},
	                          ["name", "site", "account"], as_dict=True)
	if not row:
		frappe.throw(_("Unknown order."))
	_owned(row.site)

	paid = gateway.fetch_payment(razorpay_payment_id)
	if paid["status"] not in ("captured", "authorized"):
		frappe.throw(_("This payment has not gone through."))
	if paid["order_id"] != razorpay_order_id:
		frappe.throw(_("This payment belongs to a different order."))

	# Lock the record so two callbacks for one order cannot both grant.
	locked = frappe.db.sql(
		"""SELECT name, status, credits, total FROM `tabAI Assist Payment`
		   WHERE name = %s FOR UPDATE""", row.name, as_dict=True)[0]
	if locked.status == "Paid":
		# Already handled. Saying so is friendlier than an error.
		return {"ok": True, "already": True,
		        "credits": credits_mod.balance(row.site)["credits"]}

	if paid["amount"] != gateway.to_paise(locked.total):
		frappe.throw(_("The amount paid does not match the order."))

	credits_mod.grant(row.site, locked.credits, payment=row.name,
	                  remarks=_("Razorpay {0}").format(razorpay_payment_id))

	frappe.db.set_value("AI Assist Payment", row.name, {
		"status": "Paid",
		"paid_on": frappe.utils.today(),
		"method": paid.get("method") or "Razorpay",
	}, update_modified=False)
	frappe.db.commit()

	return {"ok": True, "already": False, "credits_added": locked.credits,
	        "credits": credits_mod.balance(row.site)["credits"]}
