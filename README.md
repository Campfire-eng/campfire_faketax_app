# FakeTax

A demo Campfire OAuth app that puts made-up sales tax on invoices. It exists to exercise every
app-extension capability: link, webhook and iframe buttons, embedded panels, event subscriptions
and install config. The tax is fake; the plumbing is real.

Tax rule: the invoice's shipping state (or, for a credit memo, the client's address) picks a
jurisdiction from `jurisdictions.py`, seventeen US states with a state rate, an average local
add-on and whether the state taxes services and SaaS. A workspace only collects where it has
**nexus**, chosen during onboarding and editable on the manage screen, which can also override
any state's combined rate. Each line carries a **tax code** in a Campfire custom field
(`TAXABLE`, `SAAS`, `SERVICE`, `EXEMPT`) that decides whether that line is taxed in that state.
Unknown states fall back to the workspace's `default_rate` install config. Per line,
`tax = (amount - discount) * rate / 100`, rounded half-up to cents, with
`tax_description = "FakeTax CA 7.25%"` (or `FakeTax NY no nexus`, `FakeTax OR no sales tax`).
Until a workspace finishes onboarding FakeTax taxes everywhere, so the demo works out of the box.

## Setup

1. **Create the app** in Campfire (http://localhost:3000) under Developer → Apps. Only the name
   matters; `manifest.json` sets everything else in step 4. Copy the client id and secret.
2. **Signing secret**: on the app, rotate the signing secret and copy the `whsec_…` value (it is
   shown once).
3. `cp .env.example .env` and paste `CLIENT_ID`, `CLIENT_SECRET` and `SIGNING_SECRET`.
4. **Manifest**: paste `manifest.json` into the app's Manifest tab and save. It sets the
   app's details (redirect URL `http://localhost:3999/callback`, connect URL, the
   `invoices:read` and `invoices:write` scopes), the allowed domain, five buttons, two panels and
   the invoice event subscription. The app section is versioned: applying it opens a development version,
   so click **Promote** on the app page to make it live. For a private app that is immediate,
   with no review. The allowed domain, signing secret, slots and events are not versioned and
   take effect as soon as the manifest is saved.
5. **Feature flag**: in Django admin (http://localhost:8001/admin), give the workspace the
   `oauth_app_extensions` customer feature flag. Without it no slot shows and no event is sent.
6. Run it:

   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   .venv/bin/python app.py          # binds 0.0.0.0:3999
   .venv/bin/python -m pytest -q    # unit tests
   ```

   Flask listens on 0.0.0.0 because Campfire calls the webhooks from inside Docker, where it
   rewrites `localhost` to `host.docker.internal`.
7. **Connect**: click Connect on the app's connection page in Campfire (or at
   http://localhost:3999). After the callback FakeTax asks Campfire which workspace the token
   belongs to (`GET /ca/api/v1/oauth-app-install/config` returns `customer_id`), writes its
   install config, and sends a new workspace through **onboarding**: pick the states with nexus
   and a default tax code, then let FakeTax create the `Tax code` custom field on invoice lines
   and credit memo lines (`POST /ca/api/v1/custom-fields`, scope `custom_fields:write`). The last
   step returns you to Campfire: the Connect button carries `return_to`, which FakeTax only honours
   for Campfire's own origin.
8. **Manage**: the connection page in Campfire shows **Manage in FakeTax**. It asks Campfire for
   a signed link (`POST /ca/api/v1/oauth-apps/connections/<id>/manage-link`), whose `cf_ctx`
   token has `surface: "manage"` and no record. FakeTax verifies it once and opens the workspace's
   jurisdiction table: nexus, state and local rates, your overrides, and the tax codes.

If you rotate the signing secret again, Campfire signs with both secrets for 24 hours. Put the new
one in `SIGNING_SECRET` (and optionally the old one in `SIGNING_SECRET_PREVIOUS`) and restart.

## Five-minute demo

1. **Connect** FakeTax at http://localhost:3999. The workspace card shows the install config
   Campfire now holds.
2. **Open a draft invoice** in Campfire whose shipping address is in California, e.g.
   `1 Main St\nOakland, CA 94607`. The detail header shows **Calculate tax** (a secondary button;
   it only appears for draft and open invoices because of `visible_when`), plus **Tax breakdown**
   and **View in FakeTax** in the app menu.
3. **Calculate tax**: confirm "Overwrite tax on every line?". Campfire posts a signed webhook to
   `/slots/calculate`; FakeTax reads the invoice, writes 7.25% on every line and answers with a
   success toast and a refresh, so the new tax shows straight away.
4. **Tax breakdown** opens FakeTax in a drawer. The frame asks Campfire for its context over the
   bridge, shows the lines, lets you override the rate and **Apply**; it then toasts, refreshes the
   invoice and closes itself.
5. **View in FakeTax** opens `/report/<id>?cf_ctx=…` in a new tab: a per-line report, shown only
   after the context token checks out. Reload the tab and it refuses: the token is single use.
6. **List bulk**: on the invoice list, select several invoices and choose **Calculate FakeTax**
   from the header menu. One toast summarises how many were written, sent for approval or refused
   (a closed period, for instance), and the list refreshes.
7. **Auto-calculate**: tick it on FakeTax's home page and Save (this PUTs install config to
   Campfire). Create a new invoice in Campfire: the `Invoice.created` event arrives at `/events`
   and FakeTax writes tax a moment later. Reload the invoice to see it.
8. **No rate**: clear the default rate, then Calculate tax on an invoice shipping to a state FakeTax
    has no table for, say Idaho.
   The webhook replies with an `open_iframe` effect and the breakdown drawer opens to pick a rate.
9. **Simulate failure**: set it to `500` and click Calculate tax: Campfire shows "FakeTax could not
   complete this action". Set `slow` (12s sleep, past the 8s timeout): "did not respond in time".
10. **Activity**: the app's Activity tab in Developer → Apps lists every click (status, timing,
    effects) and every event delivery, including the failures you just caused.
11. **Panels**: the invoice detail page and the edit modal gain a **FakeTax** tab showing the same
    breakdown frame embedded in the page; the invoice list gains a **FakeTax** tab with the
    workspace's settings (default rate, auto-calculate), saved straight into install config.
12. **Revoke**: disconnect FakeTax from the workspace in Campfire. Reload the invoice: the FakeTax
    buttons and tabs are gone, because they only show while a grant covers their scopes.

## How it maps to Campfire capabilities

| Capability | Manifest entry | FakeTax endpoint | What it shows |
|---|---|---|---|
| Link slot, detail header | `view` | `GET /report/<id>?cf_ctx=` | Context token verified server-side (signature, `aud`, `iss`, expiry, single-use `jti`, `object_ids` contains the id) before any data is shown |
| Link slot, row menu | `open-row` | same | The same link from a list row |
| Webhook slot, detail header | `calculate` | `POST /slots/calculate` | Signed request, `visible_when`, `confirm_text`, `toast` + `refresh` effects, `open_iframe` when it needs input |
| Webhook slot, list header | `calculate-bulk` | same | Up to 50 selected ids in one call; partial success summarised in one toast |
| Iframe slot, drawer | `breakdown` | `GET /frame/breakdown/<id>`, `POST /frame/api/*` | `static/app-bridge.js`: `ready` → context, `toast`, `refresh`, `resize`, `close` |
| Panel, detail page and edit modal | `breakdown-tab` | `GET /frame/breakdown/<id>` | The breakdown frame embedded as a tab; same bridge, no drawer |
| Panel, list page | `settings` | `GET /frame/settings`, `POST /frame/api/settings[/save]` | A token with no record; settings written to install config from inside Campfire |
| Event subscription | `event_subscriptions` | `POST /events` | Signed thin events; FakeTax re-reads the invoice with its own token |
| Install config | (none) | `GET/PUT /ca/api/v1/oauth-app-install/config` | Per-workspace `default_rate`, `auto_calculate`, `base_url` set by the app's own token |
| Onboarding | `connect_url` + `return_to` | `/setup/<workspace>` (3 steps) | Nexus, default tax code, custom field creation with the app token, return to Campfire |
| Manage link | `manage_url` | `GET /manage?cf_ctx=`, `/manage/<workspace>` | Signed entry from the connection page; single-use token, then a browser session |
| Credit memos | `event_subscriptions` | `POST /events` → `GET/PATCH /coa/api/v1/credit-memo/<id>` | Client address (`GET /coa/api/vendor/<id>`, scope `vendors:read`) picks the jurisdiction |
| OAuth connect + refresh | app settings | `/connect`, `/callback`, `/refresh/<id>` | PKCE, refresh on expiry or 401, grant dropped when refresh is refused |

## Invoice API used (and why it works with an app token)

- **Read**: `GET /coa/api/v1/invoice/<id>/` (`AccountingInvoiceUpdateDestroy`,
  `chart_of_accounts/api/invoices.py`). Returns the invoice with `lines` (each with `id`,
  `product`, `amount`, `discount`, `tax`, `tax_description`, `tags`, …), `shipping_address`,
  `location_of_sale` and `payment_status`.
- **Write**: `PATCH /coa/api/v1/invoice/<id>/` with `{"lines": [...]}`. The view's permission class
  is `ModelPermissions`, which is scope-aware (`oauth_scope_aware = True`), so
  `view_enforces_scopes` lets an app token through: GET needs
  `chart_of_accounts.view_accountinginvoice` (`invoices:read`), PATCH needs
  `change_accountinginvoice` (`invoices:write`).
- **Shipping state**: there is no structured shipping state on an invoice. `shipping_address` is
  free text (Campfire composes it as `Street\nCity, ST 12345\nCountry`), so FakeTax parses the
  state from it (two-letter code before a ZIP, `, ST`, or a full state name), falling back to
  `location_of_sale`.
- **Lines are replaced, not merged**: a PATCH with `lines` deletes and recreates every line and
  rebuilds the journal entry when GL-affecting values change (tax does). FakeTax therefore sends
  every line back with its writable fields (`product`, `product_bundle`, `quantity`, `rate`,
  `amount`, `discount`, `discount_percentage`, `department`, `tags`, `withholding_tax_rate`,
  `service_date`, `description`, `charge_type`, `entity_use_code`, `custom_field_*`) plus `tax` and
  `tax_description`. Line ids change after a write. Fields outside that list would be lost.
- **Approval**: when the workspace requires approval, `audit_log` files the change as a
  `DraftQueue` row and answers `202` with the draft (`status: "pending"`, `new_payload`). FakeTax
  reports that as "Submitted for approval".
- **Refusals**: a closed period or other validation error comes back as `400`; FakeTax counts the
  invoice as refused, shows the first reason in the toast and never retries.
- Campfire rejects nonzero tax on a zero-amount line; FakeTax's tax is zero there anyway.
- On an invoice that has a `tax_rate` with component lines, Campfire splits the line tax FakeTax
  writes across those components' tax accounts.

## Security notes

- Slot webhooks and events: HMAC-SHA256 over `"{timestamp}:{raw body}"`, checked against
  `Campfire-Webhook-Signature-v1` and `-Previous`, refused when the timestamp is more than 300s off.
- Context tokens are single use (`seen_jti` table). The iframe needs several calls with the one
  token Campfire gives it, so its first call claims the `jti` and opens a server-side frame session
  bound to it and the workspace; later calls are accepted only while that session and the token
  are live. When the calculate webhook answers with `open_iframe`, Campfire hands the frame the
  webhook's own token, so FakeTax opens the frame session for that `jti` before replying.
- The bridge only accepts messages from `CAMPFIRE_FRONTEND_ORIGIN` sent by the parent window, and
  posts only to that origin.

## Files

| File | What |
|---|---|
| `app.py` | Flask routes |
| `campfire.py` | Campfire API client: token exchange, refresh, invoice read/write, install config |
| `jurisdictions.py` | Seeded states, rates, taxability rules and tax codes |
| `tax.py` | Pure tax rules: nexus, overrides, tax codes, state parsing, PATCH payloads, event loop guard |
| `security.py` | Signature and context-token verification |
| `store.py` | SQLite: grants, per-workspace tax setup, seen jti, frame sessions, settings, cached invoices |
| `static/app-bridge.js` | The iframe side of the Campfire bridge |
| `templates/` | Home, report, breakdown, settings, onboarding and manage pages |
| `manifest.json` | The whole app: details, scopes, allowed domain, buttons, panels, events. Paste into the Manifest tab |
| `tests/` | pytest |
