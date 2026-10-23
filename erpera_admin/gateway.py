# Copyright (c) 2026, The Tiny App Company and contributors
# For license information, please see license.txt

"""Razorpay, kept behind one door.

Everything the gateway knows how to do is in here, so the rest of the control
plane never holds a key or parses a gateway response. Three rules.

The secret stays on this side. Razorpay's key_id is public and ships to the
browser with the checkout; key_secret signs and verifies and must never leave
the server, which is why both live in site_config rather than a doctype a Desk
user could read.

A browser saying a payment succeeded is a claim, not a fact. The signature it
returns is checked here with the secret, and then the payment is fetched from
Razorpay and its amount and status read from that response. Anyone can post a
success callback; nobody can forge the signature or the fetch.

Amounts cross in paise, as integers. Rupees as a float somewhere in the middle
is how you end up charging someone 1799.9999999.
"""

import hashlib
import hmac
import json

import frappe
from frappe import _
from frappe.utils import cint, flt

API = "https://api.razorpay.com/v1"
TIMEOUT = 20


class GatewayError(frappe.ValidationError):
	"""Something went wrong talking to Razorpay."""


def _stored():
	"""The active Razorpay credential.

	Preferred over site_config because a Password field is encrypted at rest
	and can be rotated from the Desk, where a config file needs a deploy and
	leaves the secret in plain text on disk. site_config stays as a fallback
	so an existing install keeps working until the row is created.
	"""
	name = frappe.db.get_value("Payment Gateway Credential",
	                           {"gateway": "Razorpay", "is_active": 1}, "name")
	if not name:
		return None
	doc = frappe.get_cached_doc("Payment Gateway Credential", name)
	secret = doc.get_password("key_secret", raise_exception=False)
	if not doc.key_id or not secret:
		return None
	return {"key_id": doc.key_id, "key_secret": secret,
	        "webhook_secret": doc.get_password("webhook_secret", raise_exception=False)}


def _creds():
	stored = _stored()
	if stored:
		return stored["key_id"], stored["key_secret"]

	key_id = frappe.conf.get("razorpay_key_id")
	key_secret = frappe.conf.get("razorpay_key_secret")
	if not key_id or not key_secret:
		# This sentence is read by a customer on their own site, so it must
		# describe their situation, not ours. Which of our keys is missing is
		# our problem, and it goes to the log where we will see it.
		frappe.log_error(
			title="Razorpay credentials missing",
			message="No active Payment Gateway Credential row and no site_config fallback.")
		frappe.throw(_("Online payment is unavailable at the moment. "
		               "Please try again shortly, or contact support."), GatewayError)
	return key_id, key_secret


def available():
	"""Can we take a payment right now? Never raises.

	Asked before a page offers a Pay button, so that a missing key on our side
	greys out one button instead of blanking the customer's whole account page
	with an error about a gateway they have never heard of.
	"""
	try:
		if _stored():
			return True
		return bool(frappe.conf.get("razorpay_key_id")
		            and frappe.conf.get("razorpay_key_secret"))
	except Exception:
		return False


def public_key():
	"""The id the checkout script needs. Safe to hand to a browser."""
	stored = _stored()
	if stored:
		return stored["key_id"]
	return frappe.conf.get("razorpay_key_id") or ""


def is_test_mode():
	return str(public_key()).startswith("rzp_test_")


def to_paise(rupees):
	"""Rupees to paise, rounded once, as an integer."""
	return int(round(flt(rupees) * 100))


def _request(method, path, payload=None):
	import requests

	key_id, key_secret = _creds()
	try:
		r = requests.request(
			method, f"{API}{path}",
			auth=(key_id, key_secret),
			headers={"Content-Type": "application/json"},
			data=json.dumps(payload) if payload is not None else None,
			timeout=TIMEOUT,
		)
	except Exception as e:
		# Never let a connection error carry the credentials into a log.
		frappe.throw(_("Could not reach the payment gateway. Please try again."),
		             GatewayError)

	try:
		body = r.json()
	except ValueError:
		body = {}

	if not r.ok:
		# Razorpay's own description is written for a person, so it is worth
		# showing. The rest of the response is not.
		desc = (body.get("error") or {}).get("description") or _("Payment gateway refused the request.")
		frappe.log_error(title="Razorpay request failed",
		                 message=f"{method} {path} -> {r.status_code}: {desc}")
		frappe.throw(desc, GatewayError)

	return body


# ------------------------------------------------------------- simulation

SIM_TTL = 3600


def simulation_enabled():
	"""Is this server allowed to fake a payment?

	Three guards, because a server that can mint paid orders for free is
	exactly the server an attacker wants. It has to be switched on
	deliberately, the site has to be in developer mode, and a live key
	disables it outright no matter what the config says.
	"""
	if not cint(frappe.conf.get("razorpay_simulate")):
		return False
	if str(public_key()).startswith("rzp_live_"):
		return False
	return bool(cint(frappe.conf.get("developer_mode")))


def _sim_key(ref):
	return f"razorpay:sim:{ref}"


def simulate_checkout(order_id, fail=False):
	"""Return what the browser would hand back after paying.

	The signature is a real HMAC over the real secret, so the verification
	path downstream is the live one. Only the two network calls are faked.
	"""
	if not simulation_enabled():
		frappe.throw(_("Test payments are not enabled."), GatewayError)

	cached = frappe.cache().get_value(_sim_key(order_id))
	if not cached:
		frappe.throw(_("No simulated order {0} is open.").format(order_id), GatewayError)

	import secrets as _secrets
	payment_id = "pay_SIM" + _secrets.token_hex(7)
	frappe.cache().set_value(_sim_key(payment_id), {
		"order_id": order_id,
		"amount": cint(cached.get("amount")),
		"status": "failed" if fail else "captured",
		"method": "card",
	}, expires_in_sec=SIM_TTL)

	_key_id, key_secret = _creds()
	signature = hmac.new(
		key_secret.encode(),
		f"{order_id}|{payment_id}".encode(),
		hashlib.sha256,
	).hexdigest()
	return {
		"razorpay_order_id": order_id,
		"razorpay_payment_id": payment_id,
		"razorpay_signature": signature,
	}


def create_order(amount_rupees, receipt, notes=None):
	"""Open an order at Razorpay and return what the browser needs."""
	paise = to_paise(amount_rupees)
	if paise < 100:
		frappe.throw(_("The smallest payment this gateway takes is one rupee."))

	if simulation_enabled():
		import secrets as _secrets

		order = {"id": "order_SIM" + _secrets.token_hex(7)}
		frappe.cache().set_value(_sim_key(order["id"]),
		                         {"amount": paise, "receipt": str(receipt)},
		                         expires_in_sec=SIM_TTL)
	else:
		order = _request("POST", "/orders", {
			"amount": paise,
			"currency": "INR",
			"receipt": str(receipt)[:40],
			"notes": notes or {},
			# Razorpay captures on its own, so a paid order cannot sit uncaptured
			# and silently expire.
			"payment_capture": 1,
		})
	return {
		"order_id": order.get("id"),
		"amount": paise,
		"currency": "INR",
		"key_id": public_key(),
		"test_mode": is_test_mode(),
		"simulated": simulation_enabled(),
	}


def verify_signature(order_id, payment_id, signature):
	"""Is this callback really from Razorpay?

	Compared with compare_digest so a wrong signature takes the same time as a
	right one.
	"""
	_key_id, key_secret = _creds()
	expected = hmac.new(
		key_secret.encode(),
		f"{order_id}|{payment_id}".encode(),
		hashlib.sha256,
	).hexdigest()
	return hmac.compare_digest(expected, str(signature or ""))


def fetch_payment(payment_id):
	"""Ask Razorpay what actually happened, rather than believing the browser."""
	if simulation_enabled() and str(payment_id).startswith("pay_SIM"):
		sim = frappe.cache().get_value(_sim_key(payment_id))
		if not sim:
			frappe.throw(_("No simulated payment {0}.").format(payment_id), GatewayError)
		p = {"id": payment_id, "order_id": sim["order_id"], "status": sim["status"],
		     "amount": cint(sim["amount"]), "currency": "INR",
		     "method": sim.get("method"), "captured": sim["status"] == "captured"}
	else:
		p = _request("GET", f"/payments/{payment_id}")
	return {
		"id": p.get("id"),
		"order_id": p.get("order_id"),
		"status": p.get("status"),
		"amount": cint(p.get("amount")),
		"currency": p.get("currency"),
		"method": p.get("method"),
		"captured": bool(p.get("captured")),
		"email": p.get("email"),
		"contact": p.get("contact"),
	}


def verify_webhook(body, signature):
	"""Check a webhook body against the configured webhook secret."""
	stored = _stored()
	secret = (stored or {}).get("webhook_secret") or frappe.conf.get("razorpay_webhook_secret")
	if not secret:
		return False
	expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
	return hmac.compare_digest(expected, str(signature or ""))
