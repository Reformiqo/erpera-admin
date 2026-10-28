# The connector, from the inside

The customer-facing version of this is `ai_assist/docs/CONNECTOR.md`. This is
the note for us: where each piece lives, why it lives there, and what you have
to be careful of when changing it.

The product is one URL a customer pastes into Claude or ChatGPT. What arrives
at that URL is JSON-RPC from a model we do not pay for, asking to read a site we
do not host, as a user we did not authenticate. Three of the four hard parts are
therefore somebody else's, and the design is mostly about keeping it that way.

---

## Which side owns what

| | Control site (`erpera_admin`) | Customer site (`ai_assist`) |
|---|---|---|
| The tool list | **owns it.** `brain/tools.py` → `mcp_catalogue()` | caches it for 24h |
| The prompts | **owns them.** `MCP_PROMPTS` | caches them with the tools |
| The wording of every description | **owns it** | never edits it |
| Running a tool call | never sees one | `mcp/execute.py` |
| The protocol | nothing | `mcp/__init__.py` |
| Who the caller is | nothing | Frappe's own OAuth |
| The business data | **never touches it** | stays on the site |

The split is the same one the agents use and for the same reason: the customer
is Administrator on the site the connector runs on. Anything that decides what
the product may do has to be decided somewhere they are not. So the list of
tools, and the exact words each tool is described with, come down from
`erpera_admin.api.mcp_tools` over the enrolment token.

What does **not** go upstream is the call itself. A tool call is executed
locally, by `ai_assist/mcp/execute.py`, against the site's own database as the
signed-in user. The control plane never sees a customer's records, and the
connector works identically if the control plane is down, on whatever list it
last fetched. `catalogue.py` is deliberately stale-beats-empty.

Billing: a connector call is logged with `provider="mcp"` and `model=None`, so
`credits.cost_of` prices it at zero. The client's own subscription did the
thinking. This is worth not breaking by accident, because it is also the reason
the feature can be given away.

---

## How much of this is Frappe's

Most of it. v16.17 ships the entire MCP authorisation chain:

- `/.well-known/oauth-protected-resource` (RFC 9728)
- `/.well-known/oauth-authorization-server` (RFC 8414) and OpenID discovery
- PKCE
- dynamic client registration, RFC 7591, so no client id is ever handed to a customer
- `WWW-Authenticate: Bearer resource_metadata="..."` on a 401, in `frappe/app.py`

All three discovery documents are off by default, which is the only reason this
needed a button at all: `mcp.admin.set_enabled(1)` switches on
`show_protected_resource_metadata`, `show_auth_server_metadata` and
`enable_dynamic_client_registration` alongside our own flag. Turning the
connector **off** leaves them alone, since another integration may have come to
depend on them by then.

`frappe.auth.validate_oauth` sets the session user from the bearer token before
any of our code runs. We mint nothing, impersonate nobody, and read
`frappe.session.user` like any other request. An invalid token leaves Guest,
which is why `serve()` checks for Guest first and answers with the challenge
before it has looked at anything else: an unauthenticated caller should not be
able to learn whether this site even has the connector switched on.

Two Frappe facts the endpoint depends on:

- `frappe.handler.handle()` passes a returned werkzeug `Response` straight
  through (`frappe/handler.py:57`), so a whitelisted method can emit an exact
  JSON-RPC body with its own status and headers.
- the request body is parsed in `init_request`, before app code. Malformed JSON
  never reaches us (Frappe answers 417 in its own dialect) and neither does a
  bare scalar. A JSON **array** does arrive, in `form_dict["data"]`, which is
  why the `-32600` batching refusal is live code and not decoration. Comments in
  `serve()` say which branch is reachable from where; read them before deleting
  one as dead.

---

## The no-submit guarantee

Stated to the customer as a guarantee, so it has to be one. It rests on two
things, in this order:

1. **There is no submit tool in the catalogue.** `mcp_catalogue()` builds from
   `READ_TOOLS`, `WRITE_TOOLS` (`create_record`, `update_record`), `OPEN_TOOL`
   and `MCP_ONLY_TOOLS`. Nothing in any of them submits.
2. **There is no submit branch in the executor.** `execute.run()` has no
   `submit`, no `docstatus` write, no `cancel`, no `amend`, no `delete`. The
   agent's own tool loop has a hard submit gate with a confirmation step; the
   connector does not have the gate because it does not have the capability.

Point 2 is the one that matters. A list is a description of a guarantee; code
with no path to the verb is the guarantee. If anyone ever adds a submit tool
upstream, the connector will offer it and still refuse it, and that asymmetry is
intentional: the brain can describe a tool the site cannot run, never the
reverse.

Alongside it, in `execute.py`:

- `SECRET_DOCTYPES` is refused for read **and** write, whatever roles the user
  holds: AI Assist Settings, OAuth Client / OAuth Bearer Token / OAuth
  Authorization Code, Email Account, Social Login Key, User Social Login,
  Webhook, Connected App, Token Cache. A connector that can read an API key is
  no longer a connector.
- `NO_WRITE_DOCTYPES` adds User, Role, DocType, Server Script, Client Script and
  friends: readable, never writable.
- `_fit()` drops whole rows until the payload fits `MAX_RESULT_CHARS` and says
  what it left out. It replaced character truncation, which produced invalid
  JSON on a wide doctype and broke `structuredContent` with it.
- `_dedupe_key` / `_already_created` give a 60 second window in which a retried
  `create_record` returns the first document instead of making a second one.
  Clients retry; users should not find two Payment Entries because of it.
- Payment Entry enrichment (accounts, FIFO allocation against the oldest
  outstanding invoices) is shared with CoWork. A bare Frappe insert does not
  resolve those accounts.

---

## Three switches and a ceiling

`catalogue._profile()` reads them; `_gate()` and `catalogue.tools()` apply them.
They sit on top of each user's own permissions and apply to everybody,
Administrator included.

| Field | Where it bites |
|---|---|
| `mcp_enabled` | `_gate()`, before anything but `ping` |
| `mcp_read_only` | `catalogue.tools()` filters out every non-`readOnlyHint` tool, so a read-only connector is not told the write tools exist |
| `mcp_role` | `_gate()`, naming the role to ask for. The reader is an employee who needs it, not an attacker learning something |
| `mcp_doctypes` | `mcp._limited_doctype()`, on every call, before the tool runs |
| `mcp_rate_limit` | `_spent()`, an hourly Redis bucket per user, 240 by default. A broken cache opens the door rather than closing it |

`ping` deliberately answers through a closed gate. A client that cannot ping
cannot tell a switched-off connector from a broken site.

---

## Testing it

Three layers, all of them committed:

- `bench --site <site> execute ai_assist.mcp.probe.run` walks every tool against
  real data and returns what each one answered. `run(write=1)` adds the write
  pass (create → comment → update → repeat-create expecting dedupe → delete).
  `run(as_user="...")` walks it as somebody else, which needs write permission
  on the settings because it is impersonation. This is what the setup page's
  **Test It** button calls.
- `bench --site <site> run-tests --module ai_assist.tests.test_mcp` covers the
  protocol surface without HTTP: handshake, refusals, read-only, allowlist,
  prompts, resources, no-submit.
- the HTTP harness in the scratchpad, over real OAuth bearer tokens, for the
  parts only a real request exercises: the 401 challenge and its header,
  discovery, dynamic registration, malformed bodies.

What the probe finds, almost always, is the site rather than the connector: no
`Global Defaults.default_company`, a Mode of Payment with no account for this
company, a user without the roles they believed they had. That is the point of
having it on a button.

Two things that cost real time, worth remembering:

- `get_fiscal_year(date - 1 year)` can match a stray test fiscal year. `_fiscal()`
  now takes the current year and subtracts a year from its dates, then names it
  by exact start/end match. An April to March business was getting the 2025
  calendar year out of "last fiscal year".
- ToDo's controller `has_permission` strips the automatic roles, "All" among
  them, so a plain Desk user must set `allocated_to`. The probe's fixture does.

---

## Changing a tool

Upstream, in `erpera_admin/brain/tools.py`, and then nowhere else. Customer
sites pick it up within 24 hours, or immediately if the connector is toggled
(which force-refreshes). `_as_mcp()` converts our OpenAI-dialect definitions
into MCP's `inputSchema` plus annotations, so a tool added for the agent is
automatically shaped for the connector too.

Two rules:

- a new tool needs a matching branch in `execute.run()` on the site, or it will
  be offered and then refused. The brain can run ahead; the site cannot.
- `readOnlyHint` is what `mcp_read_only` filters on. Get it wrong and a
  read-only connector hands out a write tool.

Prompts are `MCP_PROMPTS`, with a `template` the site never shows a client
directly: `_prompt_summary()` strips `template` and `default` out of
`prompts/list`, and `prompts/get` renders the template with the arguments. The
templates are written to make a client call `get_context` first, because a model
in somebody else's product has none of our system prompt and does not know which
company it is looking at.

---

## Not built yet

**WhatsApp as a connector.** The gateway can already send. What it has no notion
of is an approval gate, and "an assistant somewhere else can send a message from
your business number" needs one more than it needs tools. Next slice, not this
one.

**A raw SQL tool.** Deliberately absent. Every other ERP MCP has one, and every
one of them breaks the only promise this connector makes: a connector never sees
more than its owner. A SQL string cannot be permission-checked, so `aggregate`
exists instead and covers the real question, which was always totals.
