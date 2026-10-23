# Setting up on Frappe Cloud

Two sites, two apps, one direction of trust.

| | app | repo | what it is |
|---|---|---|---|
| Control site | `erpera_admin` | `The Tiny App Company/erpera-admin`, branch `version-16` | the brain: prompts, tool list, query registry, model keys |
| Customer site | `ai_assist` | `The Tiny App Company/erpera-ai-assist`, branch `version-16` | plumbing only: who we are, where to ask, what it cost |

The customer site never calls a model. It asks the control site a question and
the control site reads the customer's data back over REST with a key the
customer gave it. So two links have to be made, in this order: the customer
site has to know where the control site is, and the control site has to be
allowed to read the customer site.

---

## 0. Which address the customer site calls

`ai_assist/ai/control.py` resolves it in this order, first match wins:

1. `ai_assist_control_url` in the site's own config
2. `control_url` on AI Assist Settings (a hidden field, so normally unused)
3. the shipped default, `https://control.nbg.frappe.cloud`

**The shipped default is the live control site, so a customer site needs no
configuration at all.** Install the app and it finds the brain. That is what
the module is built for: a customer installs an app, and should not also have
to know the address of the machine that licenses it.

Set the site config key only when one site has to go somewhere else, a staging
brain for instance:

```
ai_assist_control_url = https://<some-other-control-site>
```

Site config wins over everything else, so this is also how you move an already
connected customer without waiting for a release.

**Moving the brain to a custom domain later** is two steps and no migration: add
the domain on the control site in Frappe Cloud and point the DNS CNAME at it,
then change `DEFAULT_URL` in `ai_assist/ai/control.py` and release. Sites that
have not taken the release yet keep working on the `.frappe.cloud` name as long
as it still resolves, and any site that needs to move sooner takes the site
config key.

Two things not to do. Do not use `http://`: the site token travels on every
call. Do not use an internal bench address or IP for either side: the control
plane refuses a target that resolves to a private network when it reads back,
and that guard is deliberate.

---

## 1. The control site, once

Install `erpera_admin`, then:

**AI Provider Credential** (new record)
- Provider: `groq` or `anthropic`
- API Key: the real key
- Default Model, Enabled: on

**TTAC AI Settings**
- Provider, Model: the credential above. Set the Fallback pair too if you have a second key.
- **Answer On Behalf Of Sites: on.** This is the master switch. Off, and every
  customer question comes back `503 relay disabled`.
- Enrolment Mode: ships as `Key`. Set an Enrolment Code and hand that to
  customers. With mode `Key` and no code set, enrolment refuses every site.
  `Off` refuses everything, `Open` lets any site register itself, `Approval`
  registers it but holds it.
- Credits: leave **Sell Credits off** until you are actually billing. On, a
  freshly enrolled site has a zero balance and cannot ask a single question.
  The per-site `on_credits` flag is the second switch, so turning the feature
  on does not gate every existing customer at once.
- Trial Days, Trial Credits, Enrolments Per Hour: as you want them.

Transcription needs a `groq` credential specifically, even when chat runs on
Claude, because Whisper is Groq's. Add one and leave it enabled, and the relay
finds it by provider name.

---

## 2. The customer site

Install `ai_assist`. (Frappe Cloud needs the `[tool.bench.frappe-dependencies]`
block in `pyproject.toml`, which has been there since `8c93450`. Without it the
bench refuses the app with "Could not find a compatible Frappe version".)

Open `/app/ai-assist-setup` as a System Manager. The page is a four step
checklist and the last two steps are the two links.

**Connect this site.** Contact email, company name, and the enrolment code from
step 1. This posts to `erpera_admin.api.enrol`, and the site name and token come
back and are stored without anyone reading them. It runs once: a site that
already has a token is refused rather than rotated, because a second caller
claiming to be an existing site is either a mistake or an attack.

**AI can read this site.** The site address is prefilled from
`frappe.utils.get_url()`. Leave the API key and secret fields blank: a pair is
minted for whoever is doing the setup and becomes the site default, the
credential used when there is no signed in person, such as a WhatsApp message.
Every other user gets their own pair, issued the first time they ask something,
so an answer never carries more permission than the person asking.

Pasting an existing key pair instead works and is the right move if you want
the default credential to belong to a dedicated integration user rather than to
a named administrator.

Pressing the button sends the address and the pair up, and the control plane
immediately calls `frappe.auth.get_logged_user` on the customer site with it. It
reports `Connected` only when that answers. A key that silently does not work
would turn every later question into a mystery.

---

## 3. Verifying it, from both ends

**From the customer site**, the setup page's fourth row should read Connected
with a user count. On the command line:

```bash
bench --site <customer-site> execute ai_assist.ai.enrol.status
```

`configured` true means it found an address, `connected` true means it has a
token, and the `access` block is the control plane's own answer about whether it
can read the site, asked of it rather than inferred locally.

**From the control site**, open the `Managed Site` record. The ERP section
carries Status, Site Address, Last Probe, and `erp_probe_error`, which holds the
reason whenever Status is not Connected. `AI Call Record` fills up as questions
are asked, with the cost of each.

Then ask a real question in the chat. The round trip is: customer site posts
prompt plus site token to `erpera_admin.api.ask`, the brain runs the loop, calls
back into the customer site with that user's key for each read, and returns the
answer plus the usage.

---

## 4. When it does not work

| What you see | What it is |
|---|---|
| "This licence server is not accepting new sites" | Enrolment Mode is `Off` |
| "This licence server has no enrolment code set" | Mode is `Key` with an empty code |
| "That enrolment code is not right" | the code does not match |
| "... is already registered" | a `Managed Site` of that name exists; enrol never re-issues |
| "The site address must use https://" | `base_url` is `http://` |
| "points to a private network, which we cannot reach" | `base_url` resolves to a private or loopback IP from the control site |
| "Your ERPNext site rejected our key" | key or secret wrong, or that user is disabled on the customer site |
| "You don't have permission for that" | the asking user genuinely lacks that DocType permission. Correct behaviour, not a setup fault |
| `503 relay disabled` | Answer On Behalf Of Sites is off |
| "has no attribute `erp_state`" or similar | workers are serving older code. A Frappe Cloud deploy restarts them; a local bench needs `sudo supervisorctl restart <bench>-web: <bench>-workers:` |
| "AI Assist could not be reached" | the customer site cannot make outbound HTTPS, or the control address is wrong |

`erpera_allow_private_erp` in site config lifts the private network guard. It
exists so a developer can point the brain at `localhost`. **Never set it on a
production control site.**
