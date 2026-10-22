# Copyright (c) 2026, Reformiqo and contributors
# For license information, please see license.txt

"""The control plane's own model client.

A deliberate twin of ai_assist/ai/providers.py, not an import of it. The two
apps are separate products and the licence server has to run on benches where
ai_assist is not installed, so a cross-app import would be a dependency nobody
declared and nobody would notice breaking until a customer's question failed.

What is duplicated is narrow on purpose: speak the OpenAI dialect, adapt
Anthropic to it, retry the three status codes worth retrying. The provider
quirks (gpt-oss reasoning, cleaning foreign fields out of history) are copied
exactly, because a difference between the two is a bug that only shows up on
whichever side the customer is not using.
"""

import json
import time

import requests

OPENAI_COMPAT = {
	"openai": {"base": "https://api.openai.com/v1", "model": "gpt-4o"},
	"groq": {"base": "https://api.groq.com/openai/v1", "model": "openai/gpt-oss-120b"},
	"deepseek": {"base": "https://api.deepseek.com", "model": "deepseek-chat"},
	"mistral": {"base": "https://api.mistral.ai/v1", "model": "mistral-small-latest"},
	"qwen": {"base": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
	         "model": "qwen-plus"},
	"gemini": {"base": "https://generativelanguage.googleapis.com/v1beta/openai",
	           "model": "gemini-2.0-flash"},
}

VISION_MODELS = {
	"openai": "gpt-4o",
	"groq": "qwen/qwen3.8-27b",
	"mistral": "pixtral-12b-2409",
	"anthropic": "claude-sonnet-4-20250514",
}

TIMEOUT = 120

_session = None


class ProviderError(Exception):
	pass


class ProviderNetworkError(ProviderError):
	"""The provider never answered. Worth failing straight over to the other one."""


def _get_session():
	"""One pooled session per worker, so TLS and DNS are paid once."""
	global _session
	if _session is None:
		import requests.adapters

		_session = requests.Session()
		_session.mount("https://", requests.adapters.HTTPAdapter(
			pool_connections=4, pool_maxsize=8, max_retries=0))
	return _session


def _post(url, headers, payload, backoff_429=True):
	last = None
	net_fails = 0
	for attempt in range(3):
		try:
			r = _get_session().post(url, headers=headers, json=payload, timeout=TIMEOUT)
		except requests.RequestException as e:
			net_fails += 1
			last = str(e)
			if net_fails >= 2:
				raise ProviderNetworkError(last)
			time.sleep(1)
			continue
		if r.status_code == 429:
			last = f"HTTP 429: {r.text[:200]}"
			if not backoff_429:
				# The other provider is still untried; switching beats sleeping.
				raise ProviderError(last)
			if attempt < 2:
				time.sleep(4 * (attempt + 1))
			continue
		if r.status_code in (500, 502, 503, 529):
			last = f"HTTP {r.status_code}: {r.text[:200]}"
			if attempt < 2:
				time.sleep(2 * (attempt + 1))
			continue
		if r.status_code >= 400:
			raise ProviderError(f"HTTP {r.status_code}: {r.text[:300]}")
		return r.json()
	raise ProviderError(last or "provider unreachable")


def clean_msg(m):
	"""History can hold raw messages from another provider after a failover.

	Groq's gpt-oss adds a 'reasoning' field that Mistral rejects with a 422, so
	only the standard OpenAI-dialect fields travel.
	"""
	keep = {k: m[k] for k in ("role", "content", "tool_calls", "tool_call_id", "name")
	        if m.get(k) is not None}
	if "content" not in keep and "tool_calls" not in keep:
		keep["content"] = ""
	return keep


def _chat_openai(messages, system, tools, api_key, model, base, max_tokens, backoff_429=True):
	msgs = ([{"role": "system", "content": system}] if system else []) + \
	       [clean_msg(m) for m in messages]
	payload = {"model": model, "messages": msgs, "max_tokens": max_tokens}
	if "gpt-oss" in (model or ""):
		# These models burn hidden reasoning tokens by default, and the agent
		# loop is tool-driven, so the deliberation is latency nobody asked for.
		payload["reasoning_effort"] = "low"
	if tools:
		payload["tools"] = tools

	data = _post(f"{base}/chat/completions",
	             {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
	             payload, backoff_429=backoff_429)

	msg = ((data.get("choices") or [{}])[0].get("message")) or {}
	calls = []
	for tc in msg.get("tool_calls") or []:
		fn = tc.get("function") or {}
		try:
			args = json.loads(fn.get("arguments") or "{}")
		except Exception:
			args = {}
		calls.append({"id": tc.get("id"), "name": fn.get("name"), "arguments": args})

	return {"text": msg.get("content") or "", "tool_calls": calls, "raw_message": msg,
	        "usage": data.get("usage") or {}, "model": data.get("model") or model}


def _chat_anthropic(messages, system, tools, api_key, model, max_tokens, backoff_429=True):
	"""Adapt the OpenAI dialect to Anthropic content blocks and back again."""
	amsgs = []
	for m in messages:
		role = m.get("role")
		if role == "tool":
			amsgs.append({"role": "user", "content": [{
				"type": "tool_result",
				"tool_use_id": m.get("tool_call_id"),
				"content": str(m.get("content") or ""),
			}]})
		elif role == "assistant" and m.get("tool_calls"):
			blocks = []
			if m.get("content"):
				blocks.append({"type": "text", "text": m["content"]})
			for tc in m["tool_calls"]:
				fn = tc.get("function") or {}
				try:
					args = json.loads(fn.get("arguments") or "{}")
				except Exception:
					args = {}
				blocks.append({"type": "tool_use", "id": tc.get("id"),
				               "name": fn.get("name"), "input": args})
			amsgs.append({"role": "assistant", "content": blocks})
		else:
			amsgs.append({"role": role, "content": m.get("content") or ""})

	payload = {"model": model or "claude-sonnet-4-20250514",
	           "max_tokens": max_tokens, "messages": amsgs}
	if system:
		payload["system"] = system
	if tools:
		payload["tools"] = [{
			"name": t["function"]["name"],
			"description": t["function"].get("description") or "",
			"input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}},
		} for t in tools]

	data = _post("https://api.anthropic.com/v1/messages",
	             {"x-api-key": api_key, "anthropic-version": "2023-06-01",
	              "Content-Type": "application/json"},
	             payload, backoff_429=backoff_429)

	text, calls = "", []
	for block in data.get("content") or []:
		if block.get("type") == "text":
			text += block.get("text") or ""
		elif block.get("type") == "tool_use":
			calls.append({"id": block.get("id"), "name": block.get("name"),
			              "arguments": block.get("input") or {}})

	# raw_message in the OpenAI dialect, so the agent loop stays provider-blind.
	raw = {"role": "assistant", "content": text or None}
	if calls:
		raw["tool_calls"] = [{"id": c["id"], "type": "function",
		                      "function": {"name": c["name"],
		                                   "arguments": json.dumps(c["arguments"])}}
		                     for c in calls]

	u = data.get("usage") or {}
	return {"text": text, "tool_calls": calls, "raw_message": raw,
	        "usage": {"prompt_tokens": u.get("input_tokens") or 0,
	                  "completion_tokens": u.get("output_tokens") or 0},
	        "model": data.get("model") or model}


def dispatch(provider, messages, system, tools, api_key, model, max_tokens,
             base_url=None, backoff_429=True):
	provider = (provider or "").lower()
	if provider == "anthropic":
		return _chat_anthropic(messages, system, tools, api_key, model, max_tokens, backoff_429)
	conf = OPENAI_COMPAT.get(provider)
	if not conf:
		raise ProviderError(f"Unknown provider: {provider}")
	base = (base_url or conf["base"]).rstrip("/")
	return _chat_openai(messages, system, tools, api_key, model or conf["model"],
	                    base, max_tokens, backoff_429)


def vision(provider, api_key, model, image_data_url, prompt, max_tokens=2048,
           base_url=None):
	"""One image, one question, text back."""
	provider = (provider or "").lower()
	if provider == "anthropic":
		import re

		mt = re.match(r"^data:(image/[a-zA-Z+]+);base64,(.+)$", image_data_url, re.S)
		if not mt:
			raise ProviderError("Unsupported image format")
		data = _post("https://api.anthropic.com/v1/messages",
		             {"x-api-key": api_key, "anthropic-version": "2023-06-01",
		              "Content-Type": "application/json"},
		             {"model": model, "max_tokens": max_tokens, "messages": [{
			             "role": "user", "content": [
				             {"type": "image", "source": {"type": "base64",
				                                          "media_type": mt.group(1),
				                                          "data": mt.group(2)}},
				             {"type": "text", "text": prompt},
			             ]}]})
		text = "".join(b.get("text") or "" for b in data.get("content") or []
		               if b.get("type") == "text")
		u = data.get("usage") or {}
		return text, {"prompt_tokens": u.get("input_tokens") or 0,
		              "completion_tokens": u.get("output_tokens") or 0}

	conf = OPENAI_COMPAT.get(provider) or {}
	base = (base_url or conf.get("base") or "https://api.openai.com/v1").rstrip("/")
	data = _post(f"{base}/chat/completions",
	             {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
	             {"model": model, "max_tokens": max_tokens, "messages": [{
		             "role": "user", "content": [
			             {"type": "text", "text": prompt},
			             {"type": "image_url", "image_url": {"url": image_data_url}},
		             ]}]})
	text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
	return text, (data.get("usage") or {})
