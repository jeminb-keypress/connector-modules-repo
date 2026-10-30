# kits_odoo_woocommerce_connector — AI Handover Document

> **Mandatory for future AI agents:** Read this file **and** `kits_odoo_base_connector/ai.md` before modifying this module. After every code change, append a dated changelog entry below. Never erase previous history.

---

## 1. Module purpose

Odoo 20 WooCommerce connector that syncs products, customers (import-only), sales orders, coupons, and inventory for one or more WooCommerce stores. Built as a **separate** addon under `custome/20.0/kits_odoo_woocommerce_connector` (sibling of the base connector — not nested inside it).

**Current version:** `20.0.1.10.0`. Depends on base `20.0.1.8.0` (`deleted_local`).

Depends on `kits_odoo_base_connector` for instances, HTTP client, sync jobs, logs, and bindings.

Inspired by Emipro / TeqStars WooCommerce apps for feature shape only — **no proprietary code copied**.

---

## 2. Architecture

```
kits_odoo_woocommerce_connector
    │
    ├─ extends kits.connector.instance   (platform=woocommerce, store settings, actions)
    ├─ extends kits.connector.sync.job   (_run_woo_* handlers)
    ├─ services/woo_api.py               (REST helpers, pagination, 429 retry)
    ├─ product.template / product.product / sale.order field extensions
    └─ cron: schedule auto order import + inventory export jobs
                 │
                 ▼
        kits_odoo_base_connector (jobs cron, bindings, logs, HTTP)
```

### Job types

| Job type | Handler | Purpose |
|----------|---------|---------|
| `woo_import_products` | `_run_woo_import_products` | Woo → Odoo products |
| `woo_export_products` | `_run_woo_export_products` | Odoo → Woo products |
| `woo_import_customers` | `_run_woo_import_customers` | Woo → Odoo partners |
| `woo_export_customers` | _(removed)_ | Legacy job type raises; customer export not supported |
| `woo_import_orders` | `_run_woo_import_orders` | Woo → confirmed sale orders |
| `woo_export_inventory` | `_run_woo_export_inventory` | Odoo free qty → Woo stock |
| `woo_webhook_customer` | `_run_woo_webhook_customer` | Async customer.created/updated/deleted webhook apply |
| `woo_import_categories` | `_run_woo_import_categories` | Hierarchical category import |
| `woo_export_categories` | `_run_woo_export_categories` | Category export |
| `woo_import_tags` | `_run_woo_import_tags` | Tag import |
| `woo_import_attributes` | `_run_woo_import_attributes` | Attribute + term import with bindings |
| `woo_import_coupons` / `woo_export_coupons` | `_run_woo_*_coupons` | Coupon CRUD sync |
| `woo_webhook_product` / `order` / `coupon` | `_run_woo_webhook_*` | Non-customer webhooks |

---

## 3. File responsibilities

| Path | Responsibility |
|------|----------------|
| `__manifest__.py` | Depends: base connector, sale_management, sale_stock, stock, website_sale |
| `services/woo_api.py` | WooCommerce REST facade (paginated lists, CRUD, 429 backoff) |
| `models/kits_connector_instance.py` | Woo fields, auth hooks, connection test, enqueue actions, schedule cron |
| `models/kits_connector_instance_image.py` | Main/gallery/variant image sync via `website_sale` `product.image`; export uploads via WP XML-RPC |
| `models/kits_connector_instance_product.py` | Product import/export + attributes/variants/tags/barcode |
| `models/kits_connector_instance_customer.py` | Dedicated customer import (no export) + guest-safe order partner helper |
| `models/kits_connector_instance_order.py` | Order import, failure retry list, lines, shipping/discount, confirm |
| `models/kits_connector_instance_inventory.py` | Warehouse-scoped inventory export |
| `models/kits_connector_instance_webhook.py` | Customer webhook fields, HMAC verify, register/enqueue/process |
| `controllers/woo_webhook.py` | Public HTTP endpoint `/kits_woo/webhook/<id>/customers` |
| `models/kits_connector_sync_job.py` | `_run_woo_*` dispatch wrappers |
| `models/product_*.py` / `sale_order.py` | UI/tracking fields |
| `tests/` | Mocked API TransactionCase + HttpCase webhook coverage |
| `data/product_data.xml` | Shipping + discount service products |
| `data/ir_cron_data.xml` | Auto-schedule cron (15 min) |
| `views/*` | Store form/actions, menus, SO/product bits |
| `static/description/icon.png` | Module icon |
| `ai.md` | This document |

---

## 4. Configuration (per store / instance)

Required for WooCommerce platform instances:

- **Store URL** → `base_url` (e.g. `https://shop.example`)
- **Consumer Key / Secret** → `woo_consumer_key` / `woo_consumer_secret` (manager group only)
- **Company**, optional **warehouse**, **pricelist**, **sales team**
- **Order statuses** CSV (default `processing,completed`)
- **Import Product Images** (`woo_import_images`, default True)
- **Match Products by SKU** / **Allow Unmapped Order Lines**
- **Auto Import Orders** / **Auto Import Customers** / **Auto Export Inventory** flags
- **Image export media upload** (optional in `config_json`): `woo_wp_username` / `woo_wp_password` for WP XML-RPC `metaWeblog.newMediaObject`
- **Customer webhooks** (Customer Webhooks tab):
  - `woo_webhook_public_base_url` — public HTTPS origin WooCommerce can reach (no path). Local/tunnel OK for dev.
  - Delivery URL is computed as `{base}/kits_woo/webhook/{instance_id}/customers`
  - Register creates Woo `customer.created` / `updated` / `deleted` webhooks with a shared HMAC secret
  - **Production requires a public HTTPS Odoo URL** (or reverse proxy / tunnel). Multi-DB local servers may need `X-Odoo-Database` or `dbfilter` so unauthenticated POSTs hit the right DB.

API base resolved as: `{base_url}/wp-json/wc/v3` (use the same host as WP `siteurl`, e.g. `localhost` not `127.0.0.1`, or OAuth signatures fail on HTTP).

Auth: HTTPS → Basic Auth; HTTP → OAuth 1.0a one-legged (`services/woo_oauth.py`). Secrets never logged.

---

## 5. Workflows

### Products import
1. Paginate `GET /products` (draft/private → `active=False`). `trash` is a remote delete: mark this store’s bindings `deleted` and drop only this store from Connected Instances. Do not archive the Odoo product.
2. Match: binding (this instance + external id) → exactly one unbound SKU (`woo_match_by_sku`) → create. Several SKUs: log and skip. Never match by name. A `deleted_local` binding whose Odoo record is gone is not recreated unless the job payload sets `allow_recreate` (explicit selected import).
3. Categories/tags/barcode/status; variable attributes + variations
4. Images (if `woo_import_images`): main → `image_1920`; extras → `product_template_image_ids` (`website_sale` required); variant → `image_variant_1920`; hash skip; **videos out of scope**

### Products export
1. Templates that list this store on `woo_instance_ids` (drives computed `woo_sync_to_store`), or explicit `template_ids` (no silent mass fallback)
2. Map by **binding** for this store (update Woo id) else **create** on Woo and bind — export does **not** match Woo by SKU. `deleted` / `deleted_local` bindings are skipped unless the job has `allow_recreate` (selected Export / wizard IDs). Recreate first checks the old id; a 404 may create one new product and rebind. Scheduled cron sets `automatic` and never recreates. A 404 on an automatic update marks the binding deleted remote and does not create.
3. Push name/price/desc/SKU/attrs/tags/images; export variations for current Odoo variants only (**does not** delete orphan Woo variations)
4. Image export: upload bytes via WP XML-RPC then attach by media id (modern Woo rejects `data:` URIs); needs `config_json` WP credentials
5. **Not realtime:** Odoo product edits do not push until Export Products / scheduled job runs
6. Multi-store: one Odoo template can have **one binding per store** (different Woo product ids)

### Customers (import only — no Odoo→Woo customer export)
1. Import / Fetch / webhook: match by **binding** (instance + Woo customer id) → else **create** new partner and bind
2. Never merge by email/phone across stores or onto unbound contacts (same email → separate partners). Billing company becomes a **new parent contact for that customer only** (Odoo 20 computes `is_company` from VAT, so the parent is not flagged as a company unless it has a VAT). Phone maps to `phone`. A mobile meta value is written to `mobile` when that field exists; otherwise it is stored on the binding (`external_data.mobile`). Odoo 20 base `res.partner` has no `mobile` field.
3. Woo customer id lives only on `kits.connector.binding.external_id` — **not** a field on `res.partner`
4. **Customer export is not supported** (UI/wizard/job removed in `20.0.1.9.5`); no partner `woo_sync_to_store` / `woo_instance_ids`
5. Guests (`customer_id` 0) never get customer bindings — order path may reuse/create by billing email without binding
6. Record actions: Fetch Customer, Update from WooCommerce (+ list bulk)
7. `customer.deleted` webhook → mark binding `deleted` remote; **keep** Odoo partner
8. `kits_connected_instance_ids` on partner is **computed** from bindings (readonly display), not a stored store picker

### Orders import
1. Retry `woo_order_import_failures` by ID first, then paginate with `after=woo_last_order_sync`
2. Skip if `sale.order` binding already exists (idempotent); record cancel/refund conflicts
3. Partner via `_woo_partner_from_order`; map lines (binding/SKU); fail unmapped unless allowed
4. Shipping + discount + fee lines; tax-inclusive Woo clears Odoo line taxes
5. Bind order **before** `action_confirm()`; never invent invoices/payments/stock moves

### Inventory export
1. Active `product.product` bindings for the instance whose state is not `deleted` or `deleted_local`
2. `free_qty` with `warehouse_id` context from store warehouse, read at execution time. A job carrying an older `sync_version` is skipped so a retry cannot push a stale quantity.
3. `PUT` product or variation stock fields. Never `POST` a new product. HTTP 404 marks the binding deleted remote (and disconnects that store for a simple/parent product) and stops recovery retries.
4. Stock-move completion enqueues each store independently. One store’s enqueue error does not stop the others.

### Monitoring / retries
- Use base Connectors menus: Sync Jobs + Logs
- Failed jobs: reset/enqueue/run from base job form (retry limits + backoff)

---

## 6. External ID mapping conventions

| Odoo model | External ID | Notes |
|------------|-------------|-------|
| `product.template` | Woo product id | |
| `product.product` | Woo product id (simple) or variation id | `external_data.parent_id` for variations |
| `product.category` | Woo category id | First category on product used |
| `sale.order` | Woo order id | Idempotency key |
| `res.partner` | Woo customer id | When `customer_id` present |

---

## 7. Security

- Reuses base connector User / Administrator groups
- Credentials: `groups='kits_odoo_base_connector.group_kits_connector_manager'` + `widget="password"`
- No new models → no extra `ir.access.csv` rows; company isolation via base instance/job/log/binding rules + `check_company` on warehouse/pricelist/team/order store link
- Never log consumer secrets / tokens

---

## 8. Implemented features status

| Feature | Status |
|---------|--------|
| Multi-store configuration | **Done** |
| REST API + pagination + 429 retry | **Done** |
| Product import (simple + variable, SKU match, tags, barcode) | **Done** |
| Product export (attrs/tags/images) | **Done** |
| Image sync (main + multi gallery + variant; no video) | **Done** (`website_sale` required) |
| Dedicated customer import | **Done** (binding by Woo customer id; no customer export) |
| Record-level customer Fetch / Update | **Done** (Export removed in `20.0.1.9.5`) |
| Customer Odoo→Woo export | **Removed** (`20.0.1.9.5`) |
| Order import + auto confirm + failure retry list | **Done** |
| Inventory export | **Done** (manual bulk + event-driven + cron recovery) |
| Manual + scheduled jobs | **Done** |
| Record-level Push/Pull (products) | **Done** |
| Instance Sync Operation wizard | **Done** (all / IDs / date filters) |
| Per-instance sync status | **Done** (base binding `sync_state` + product/partner summary) |
| Bindings / logs / retries via base | **Done** |
| Mocked TransactionCase tests (`kits_woo`) | **Done** (72/72 on `odoo20-connectors-woo-tests`, 2026-10-30, 0 failed) |
| Live Woo E2E (products/customers/orders/images) | **Done** (see `LIVE_TEST_REPORT.md`) |
| Realtime push on every Odoo write | **Not in scope** (marks Not Synced; export via job/action) |
| Product gallery videos | **Not in scope** |
| Customer webhooks (HMAC) | **Done** |
| Product / order / coupon webhooks | **Done** (stock fields ignored on product webhooks) |
| Categories / tags / attributes dedicated sync | **Done** |
| Coupons import/export | **Done** (`kits.woo.coupon`) |
| Payment gateway + shipping method mappings | **Done** (no invented payments) |
| Order tracking export | **Done** |
| Refund → credit note (when invoice exists) | **Done** (`kits.woo.refund`; partial/multi-invoice; never invents payments) |
| Configurable Woo → Odoo tax maps | **Done** (`kits.woo.tax.map`; fallback modes documented) |
| Coupon product/category restrictions via bindings | **Done** (unmapped IDs block unsafe export) |
| Multi-DB webhook allowlist | **Done** (`kits_woo.webhook_db_allowlist` + `get_str`) |
| Woo → Odoo stock import | **Not in scope** (Odoo is stock source of truth) |

---

## 9. Technical decisions

1. Separate module folder under `custome/20.0/` — does not live inside `kits_odoo_base_connector`.
2. Reuse base HTTP client; Woo API layer only adds pagination + rate-limit sleep.
3. Job handlers are thin; business logic lives on `kits.connector.instance`.
4. Order binding written before confirm to keep retries idempotent.
5. Unmapped order lines **fail the order by default** (`woo_allow_unmapped_order_lines=False`); optional fallback to `WOO_MISC`.
6. Depends on `sale_stock` so `sale.order.warehouse_id` is available.
7. Hard-depends on `website_sale` for native multi-image gallery (`product.image`); videos not synced.
8. Failed order IDs stored in `woo_order_import_failures` and retried by ID so date checkpoints cannot skip them.
9. Product sync is job-based; mapped field edits mark bindings Not Synced (no silent auto-push). Customer sync is import/webhook/fetch only (no customer export).
10. Woo cron method is overridden on instance; base module keeps a no-op stub for multi-DB cron safety.
11. Store-level product export only includes templates with this store in `woo_instance_ids` (or explicit `template_ids`). No 200-cap fallback.
12. Odoo is the stock source of truth. Deliveries/returns/adjustments enqueue targeted inventory export when `woo_auto_export_stock` is on. Cron recovers `not_synced`/`error` product bindings only. Order import confirms SOs and does not invent stock moves.
13. Remote Woo product deletion does **not** delete or archive Odoo products. `product.deleted` and `trash` mark this store’s template and variant bindings `deleted`, remove only this store from `woo_instance_ids`, and leave other stores alone. Repeating the event is idempotent. Odoo product `unlink` marks bindings `deleted_local` and does not call the Woo delete API. Automatic import/export and stock export do not recreate either side. Selected export/import may recreate only when `allow_recreate` is set and the old remote id is confirmed missing.
14. Variant rules are our own: binding → SKU → create; attribute lines drive variable products; do not auto-remove Odoo variants when Woo omits them; export does not delete orphan Woo variations when an Odoo variant is removed.

---

## 10. Known limitations / pending

- Variant matching on import can miss exotic attribute option naming mismatches.
- Tax mapping is minimal (uses Odoo product/fiscal defaults; Woo tax lines not fully mirrored).
- Coupon/fee lines beyond `discount_total` are not fully modeled.
- Customer export intentionally unsupported; Sync wizard Export+Customers raises.
- Deleting an Odoo variant then exporting does **not** remove the Woo variation (orphan on Woo).
- Not every edge case has a dedicated test; see changelog testing notes.
- Image change detection still hash-based; partial gallery races can leave an extra `website_sale` row (pre-existing).
- Image export requires WP application/user credentials in `config_json` (XML-RPC); Woo REST alone cannot accept `data:` URIs on modern WP.
- `website_sale` Media gallery may auto-create an extra main-image gallery row on export round-trips (observed 3→4 once, then stable).
- Customer/product webhooks need public base URL + Register; without that, remote deletes do nothing in Odoo until manual sync.
- Odoo 20 base contacts have `phone` and no `mobile`. Mobile is kept on the binding unless another module adds `res.partner.mobile`.
- Billing company is a parent contact. `is_company` is computed from VAT, so the parent is not a “company” flag by itself.
- Bulk/scheduled product export does not recreate a remotely deleted product. Only an explicit selected export (`allow_recreate`) does, and only after the old id 404s.
- Stale webhook protection compares `date_modified` / `date_modified_gmt` when both sides have one. Events without timestamps are not treated as stale.
- This 2026-10-30 pass did not call a live WooCommerce store. Automated tests mock the API.

### Pending

- [x] Live smoke-test against local WooCommerce (`odoo20-connectors` + `localhost/wordpress`)
- [x] TransactionCase tests for products/customers/orders (`--test-tags kits_woo`)
- [ ] Optional: map Woo taxes more precisely
- [ ] Optional: realtime product export-on-write for flagged products
- [ ] Optional: delete orphan Woo variations when Odoo variants are removed on export

---

## 11. Validation results

| Check | Result |
|-------|--------|
| Python `py_compile` | **Passed** |
| XML well-formedness + manifest data paths | **Passed** |
| Odoo `Manifest.for_addon` discovery | **Passed** |
| Import via `odoo.addons.kits_odoo_woocommerce_connector` | **Passed** |
| Live DB install / upgrade | **Passed** on `odoo20-connectors` (module through `20.0.1.4.1`) |
| Live Woo API + browser sync | **Passed** 2026-10-29 (see changelog live browser pass); product/stock/wizard/UI |
| Automated `kits_woo` | **0 failed, 0 error(s) of 72 tests** on `odoo20-connectors-woo-tests` (2026-10-30). Live `odoo20-connectors` was not upgraded in that run. |

---

## 12. Change log

### 2026-10-27 — Initial WooCommerce connector module

**Created** (all under `custome/20.0/kits_odoo_woocommerce_connector/`)

- Module scaffold, manifest, icon
- `services/woo_api.py`
- Instance extensions: config, auth, product/order/inventory sync, cron scheduler
- Sync job handlers `_run_woo_*`
- `product.template` / `product.product` / `sale.order` fields
- Data: shipping/discount products + schedule cron
- Views/menus for WooCommerce stores and sync actions
- `ai.md`

**Modified**

- None inside `kits_odoo_base_connector` (intentionally untouched)

**Deleted**

- None

**Reason**

- Deliver MVP WooCommerce connector as an independent Odoo 20 module depending on the Kits base connector.

### 2026-10-27 — Syntax / discovery validation

**Modified**

- `ai.md` — recorded validation results
- `models/kits_connector_instance_product.py` — safer PTAV combination recordset for variant matching
- `models/kits_connector_instance_inventory.py` — safer stock qty int/float conversion

**Validation**

- `py_compile`, XML parse, manifest paths, Manifest discovery, and `odoo.addons` import all passed.
- No database install or live WooCommerce API test was run.

### 2026-10-27 — Install blocked by base selection_add (fixed in base)

**Bug**

- Installing this module failed with  
  `AssertionError: kits.connector.instance.platform_type: selection_add=[('woocommerce', 'WooCommerce')] on non-list selection '_selection_platform_type'`.

**Fix location**

- Fixed in dependency `kits_odoo_base_connector` by changing `platform_type` to a static selection list so `selection_add` is valid. This module’s `selection_add` code was already correct.

**Action for user**

- Upgrade/reinstall after updating the base module; retry WooCommerce module install.

### 2026-10-27 — Fix view xpath using @string

**Bug**

- Install failed: `View inheritance may not use attribute 'string' as a selector` on  
  `//group[@string='Credentials']`.

**Modified**

- `views/kits_connector_instance_views.xml` — xpath now targets `//group[@name='group_credentials']`; Woo groups given `name` attributes.
- Base module groups got matching `name=` anchors (see base `ai.md`).

**Action for user**

- Upgrade base connector if already installed, then retry WooCommerce install.

### 2026-10-27 — Odoo 20 standards compliance pass

**Modified**

- `__manifest__.py` — version `20.0.1.1.0`; category `Sales/Sales`; `application=False` (lives under Connectors app)
- `models/kits_connector_instance.py` — `_woo_get_warehouse()` with company fallback; order import/export inventory require resolvable warehouse; scheduled crons skip stores without warehouse
- `models/kits_connector_instance_order.py` — warehouse required before confirm (sale_stock `_check_warehouse`)
- `models/kits_connector_instance_inventory.py` — use `_woo_get_warehouse()` + `UserError`
- `models/kits_connector_instance_product.py` — Html vs Text field mapping (`description` Html, `description_sale` via `html2plaintext`)
- `models/product_product.py` — related field explicit `store=False`
- `views/kits_connector_instance_views.xml` — hide generic credentials for Woo; warehouse required when auto sync enabled; name-based xpaths only

**Decisions**

- Follow sale_stock rule: confirmed orders with goods need `warehouse_id`
- Prefer consumer key/secret UI over generic `api_key`/`api_secret` for Woo stores
- Connector is not a standalone app icon; menus stay under base Connectors

**Validation**

- `py_compile`, XML parse, no `@string` xpaths, no `<tree>`, no `_sql_constraints`/`name_get`, Manifest discovery, `odoo.addons` import: **Passed**
- Live DB upgrade/install in this agent environment: **Not executed** (Postgres cluster down here; user DB is available on their Odoo process)

**User action**

- Upgrade **Kits Odoo Base Connector** then **Kits Odoo WooCommerce Connector** (or reinstall Woo).

### 2026-10-27 — Fix WooCommerce HTTP 401 on connection test (localhost HTTP)

**Bug**

- Test Connection against `http://localhost/wordpress/...` returned  
  `HTTP 401 calling .../wp-json/wc/v3/products?...`  
  Cause: WooCommerce rejects HTTP Basic Auth on non-HTTPS sites; many local
  stacks also strip the `Authorization` header.

**Modified**

- `models/kits_connector_instance.py`
  - Added `_woo_get_credentials()`
  - `_kits_get_auth_headers` now always sends `consumer_key` / `consumer_secret`
    as query parameters (WooCommerce-supported method)
  - Still adds Basic Auth on HTTPS in addition to query params
  - Never logs secret values

**Decision**

- Follow official WooCommerce REST auth behavior: query-string auth for HTTP /
  header-stripping hosts; Basic Auth preferred extra on HTTPS.

**Validation**

- Syntax/import not re-run against live Woo in this step; user should retry
  **Test Connection** after upgrading/restarting the module code.

### 2026-10-27 — Harden Woo auth for localhost + password-field wipe

**Context**

- User confirmed Base URL is only `http://localhost/wordpress` and API keys are set.
- 401 means WordPress is reachable but rejected the credentials/auth method.

**Modified**

- `models/kits_connector_instance.py`
  - `write()` ignores empty credential values so password widgets do not wipe saved keys
  - `_woo_get_credentials()` reads with `sudo()`, strips whitespace
  - `_kits_test_connection()` returns a clearer 401 troubleshooting message

**User checklist**

1. Restart Odoo so query-string auth code is loaded
2. Re-paste Consumer Key + Secret and click Save (fields look blank after reload — normal)
3. Test Connection again

### 2026-10-27 — HTTP localhost uses rest_route (permalink-safe)

**Bug**

- Still HTTP 401 with correct-looking keys on `http://localhost/wordpress`.
- Common local cause: WordPress pretty permalinks / rewrite rules break `/wp-json/`
  and auth query params.

**Modified**

- `models/kits_connector_instance.py`
  - All Woo HTTP calls go through `_woo_http_request`
  - For `http://` stores: `{store}/?rest_route=/wc/v3/...&consumer_key&consumer_secret`
    (works without pretty permalinks)
  - For `https://` stores: standard `/wp-json/wc/v3/...` + Basic Auth + query params
  - Connection errors now include WooCommerce JSON `message`/`code` (never the secret)

**Security note**

- Do **not** paste Consumer Secrets into chat. The new error text is enough to diagnose.

**User action**

1. Restart Odoo
2. Re-paste keys + Save
3. Optional: WordPress → Settings → Permalinks → Post name → Save
4. Test Connection — if it still fails, send only the new **API message** line (not keys)

### 2026-10-27 — Fix woocommerce_rest_cannot_view on HTTP (auth skipped)

**Root cause**

- API message `woocommerce_rest_cannot_view: Sorry, you cannot list resources` with
  HTTP 401 usually means the request was treated as **anonymous**.
- WooCommerce only applies consumer-key authentication when `REQUEST_URI`
  contains `wp-json/wc/`.
- Our previous `?rest_route=/wc/v3/...` URLs did **not** include that path, so WC
  skipped API-key auth → permission check failed as guest.

**Modified**

- `models/kits_connector_instance.py`
  - HTTP calls now use `{store}/index.php/wp-json/wc/v3/...` first (path contains
    `wp-json/wc`), with query-string credentials + Basic Auth fallback
  - Also tries pretty `{store}/wp-json/wc/v3/...`
  - Remembers working mode in `config_json.woo_url_mode`

**Not a wrong Base URL** — auth plumbing was the issue. Keys may still need to
belong to Admin/Shop manager with Read/Write if the new URL still returns this
message after restart.

### 2026-10-27 — Implement OAuth 1.0a for plain HTTP stores

**Root cause (confirmed by WooCommerce docs)**

- On **HTTP**, WooCommerce rejects Basic Auth / `consumer_key` query auth.
- It requires **OAuth 1.0a one-legged** signing (`oauth_consumer_key`,
  `oauth_timestamp`, `oauth_nonce`, `oauth_signature_method`, `oauth_signature`).
- That is why `woocommerce_rest_cannot_view` persisted even with valid keys on
  `http://localhost/wordpress`.

**Created**

- `services/woo_oauth.py` — HMAC-SHA1 OAuth signer (no third-party dependency)

**Modified**

- `models/kits_connector_instance.py` — HTTP requests use OAuth; HTTPS keeps
  Basic Auth + query credentials
- `services/__init__.py` — import `woo_oauth`
- Connection error text explains HTTP vs HTTPS auth rules

**Validation**

- OAuth signer unit check + py_compile: **Passed**
- Live Test Connection must be retried by user after Odoo restart

**Do not paste Consumer Secrets into chat.**

### 2026-10-28 — Enhance products, images, customers, orders

**Decision**

- Image gallery option **A**: use `product.image` when `website_sale` is installed; otherwise main + variant images only. No hard `website_sale` dependency.
- Keep all business sync logic in `kits_odoo_woocommerce_connector` (base untouched).

**Created**

- `models/kits_connector_instance_image.py` — main/gallery/variant image import/export with hash skip
- `models/kits_connector_instance_customer.py` — dedicated customer import/export + guest-safe `_woo_partner_from_order`
- `tests/` — mocked product, customer, and order TransactionCase tests

**Modified**

- `services/woo_api.py` — customer CRUD endpoints
- `models/kits_connector_instance.py` — SKU match / unmapped-line / customer auto flags; failure-id helpers; hardened image download; customer actions + cron enqueue
- `models/kits_connector_instance_product.py` — binding→SKU→create matching; tags; barcode; status; image hooks; richer export
- `models/kits_connector_instance_order.py` — failure retry, stricter product mapping, billing address, fees, tax-inclusive policy, cancel/refund conflict notes
- `models/kits_connector_sync_job.py` — customer job handlers
- `models/sale_order.py` — `woo_sync_conflict`
- Views + `__manifest__.py` (20.0.1.2.0) + `models/__init__.py` + `ai.md`

**Validation**

- `python3 -m compileall` on the module: **Passed**
- XML well-formedness: **Passed**
- Odoo TransactionCase / module upgrade: **Not executed** (Postgres not running)
- Live WooCommerce API testing: **Not performed**

**Remaining limitations**

- Full Woo tax-rate → Odoo fiscal mapping still simplified
- Run `-u kits_odoo_woocommerce_connector` and `--test-tags kits_woo` when DB is available

### 2026-10-28 — Live E2E on `odoo20-connectors` + WordPress

**Results:** See `LIVE_TEST_REPORT.md`. Live Woo **YES**. Unit tests **11/11** on clean DB.

**Fixes during live run**

- `tax_id` → `tax_ids` on sale order lines (Odoo 20 rename) — order import blocker
- Product unit test mock iterators made re-usable for idempotent re-import

**Live evidence**

- Instances 3 & 4 connected to `http://localhost/wordpress` (not `127.0.0.1` — OAuth host must match WP siteurl)
- Seeded Woo products 71–76 (simple + Size S/M/L variable), customers 2–3, order 77
- Import/export products & customers OK; order 77 → confirmed `S00027`; re-import no duplicate
- Second physical WP store not created (no root/multisite); dual REST keys used as Store A/B

### 2026-10-28 — Hard-depend on `website_sale` for multi-image gallery

**Decision**

- Videos remain out of scope.
- Multi-image gallery requires native `product.image`; add `website_sale` to module `depends`.

**Modified**

- `__manifest__.py` — version `20.0.1.3.0`, depend on `website_sale`
- `models/kits_connector_instance_image.py` — docstring updated
- `ai.md` — dependency note

### 2026-10-28 — Image export via WP XML-RPC + BinaryValue handling

**Bugs found in live export of “Large Meeting Table”**

1. Modern WooCommerce rejects `data:` image URIs (`No URL Provided` / `esc_url_raw`).
2. Odoo 20 `image_1920` returns `BinaryValue` / `BinaryValueAttachment`, not a plain base64 string.

**Modified**

- `models/kits_connector_instance_image.py` — upload via `xmlrpc.php` `metaWeblog.newMediaObject`, attach by media id; BinaryValue-safe helpers
- `models/kits_connector_instance.py` — `_woo_download_image` returns base64 **str** for Image fields
- Instance `config_json` keys: `woo_wp_username` / `woo_wp_password`

**Live evidence**

- Odoo template **26** → Woo product **48** with image `.../uploads/2026/10/product-26-1.jpg`

### 2026-10-28 — Live variant + multi-image demos

**Tests**

- Variable product `cup123` (Woo **80**) → Odoo template **81**, variants `CUP123-S/M/L` (108–110)
- Multi-image demo Woo **92** ↔ Odoo template **86**: import gallery + export round-trip; reimport gallery stable
- Report artifacts: `/tmp/kits_cup123_import_result.json`, `/tmp/kits_demo_multi_image_test.json`, `LIVE_TEST_REPORT.md`

### 2026-10-29 — Cron AttributeError on DBs without Woo loaded

**Related base fix** (documented in `kits_odoo_base_connector/ai.md`)

- Cron `WooCommerce: Schedule Auto Sync Jobs` failed on `odoo20-connectors-woo-tests` with missing `_cron_woo_schedule_jobs`.
- Base module now provides a no-op stub; Woo module overrides with the real scheduler.

**Modified (woo)**

- `data/ir_cron_data.xml` — comment clarifying override/stub relationship
- `ai.md` — body sections refreshed (gallery hard-dep, job-based sync, live test status) + this changelog

### 2026-10-29 — ai.md completeness pass

**Modified**

- `ai.md` only — aligned workflows/status/decisions/limitations with current code; filled missing image-export and live-test changelog entries above.

### 2026-10-29 — Record-level sync, wizard, status, event-driven stock

**What changed**

- Depends on base `20.0.1.2.0` binding sync status.
- `models/product_template.py` / `product_product.py` — Export/Update actions, multi-store picker, mapped-field dirty marking, `woo_sync_state` summary.
- `wizard/kits_woo_sync_wizard.py` + `kits_woo_instance_picker.py` + views/security — Specific Operation (direction, entity, all/IDs/date) and store picker.
- `models/kits_connector_instance_product.py` — payload `woo_ids` / `modified_after` / `template_ids` / `sync_versions`; removed 200-product export fallback.
- `models/kits_connector_instance_order.py` / `customer.py` — ID and date filters via job payload.
- `models/kits_connector_instance_inventory.py` — targeted export, stale-version skip, 404 → binding error, pending-job merge.
- `models/stock_move.py` — `_action_done` enqueues targeted inventory when auto stock export is enabled.
- `models/kits_connector_instance.py` — Sync Operation action; cron inventory recovery (`recovery_only`).
- Views/menus for product actions and wizard; `__manifest__.py` `20.0.1.4.0`.
- `tests/test_woo_record_sync.py` — mapping, status, wizard, stock enqueue, stale jobs, order idempotency, access.

**Why**

- Users needed selected-record Push/Pull and filtered store operations, visible sync status, and stock updates driven by Odoo movements without full-store spam or double deductions.

**How it works**

- Record Push queues `woo_export_products` with `template_ids`. Pull requires a binding and queues `woo_import_products` with `woo_ids`.
- Wizard validates unsupported ops (order export, stock import) and queues the matching job type.
- Stock moves that finish call `_woo_enqueue_inventory_export`; versions prevent older jobs from overwriting newer edits. Cron only requeues dirty bindings when no inventory job is already pending.
- Order import still binds before confirm; retries do not create a second SO or invent stock moves.

**Impact**

- Compatible with existing bulk buttons. Export candidates without `woo_sync_to_store` are no longer silently included. Shopify unaffected. Base remains Woo-free aside from the pre-existing cron stub.

**Testing**

- `compileall` + XML parse: **Passed**.
- `-u kits_odoo_base_connector,kits_odoo_woocommerce_connector --test-tags kits_woo` on `odoo20-connectors-woo-tests`: **0 failed, 0 error(s) of 22 tests** (pre-live); later **23/23** after export sync-state regression test.
- Live Woo regression of the new UI paths: **See next changelog (2026-10-29 live browser pass)**.
- Untested vs full Section 8.1 list: concurrent job races under load, multi-company ACL matrix, full attribute-combination rewrite matrix, image-only hash edge cases beyond existing suite.

### 2026-10-29 — Live browser verification + export sync_state fix

**Bug found in live push**

- Product export job completed (`updated=1`) but template binding stayed `not_synced` because `_woo_export_one_template` re-bound only on **create**, not on **update**.

**Code fix**

- `models/kits_connector_instance_product.py` — after successful create/update, always `_woo_bind` the template (and remember remote images when present).
- `tests/test_woo_record_sync.py` — `test_export_update_marks_template_synced` (+ assert on selected export update path).
- `__manifest__.py` — version `20.0.1.4.1`.

**Live environment**

- Odoo `http://127.0.0.1:2099` DB `odoo20-connectors`; Woo `http://localhost/wordpress`.
- Test records: `product.template` **88** ↔ Woo **94** (instance **9**); variable Woo **95** ↔ template **90**; Store B instance **10** (no tmpl 88 binding).

**Observed results (browser + shell/API; not claimed without observation)**

| Scenario | Result |
|----------|--------|
| Dirty → Not Synced → Export job → Woo name | **Pass** after fix: binding/`woo_sync_state` **Synced**; Woo title matched Odoo |
| Woo edit → Pull into Odoo | **Pass**: name `Kits Live Push Test FROM WOO PULL` on both; UI status **Synced**, bindings **1** |
| Inventory adjust 21→25 + event job | **Pass**: job `reason=stock_move_done`; Woo qty **25** |
| Re-export same stock | **Pass**: Woo stayed **25** (absolute qty, no double deduct) |
| Delivery (−2) + return (+2) with `skip_sms` | **Pass**: Odoo/Woo **23** then **25**; jobs `stock_move_done` |
| Manual bulk inventory export | **Pass** (re-export / instance action path) |
| 15-min cron `_cron_woo_schedule_jobs` | **Pass** (callable; recovery enqueue path exercised) |
| Multi-store isolation | **Pass** for bindings: tmpl 88 only instance **9**. Store B same-WP create earlier hit SKU/400 — binding isolation still holds |
| Sync Operation wizard UI | **Pass**: Store/Direction/Operation/Filter + Queue Sync visible |
| Wizard validation (inventory import) | **Pass** (ValidationError) |
| Variant Woo 95 re-import ×2 | **Pass**: still **2** variants, no duplicates |
| UI: menus, Sync to Woo, Woo tab, Export/Update smart buttons (under More) | **Pass** in browser |
| WP admin product 94 | **Pass**: title + stock **25** after login |

**Automated**

- `-u kits_odoo_woocommerce_connector --test-tags=/kits_odoo_woocommerce_connector` on `odoo20-connectors-woo-tests`: **0 failed, 0 error(s) of 23 tests**.

**Blockers / notes**

- Completing deliveries via `button_validate` may open `confirm.stock.sms`; use context `skip_sms=True` or wizard `dont_send_sms` with `button_validate_picking_ids` context.
- Store B dual-create against the same WordPress catalog is limited by unique SKU; instance-specific bindings remain the isolation guarantee.
- Temporary WP admin password was set locally for UI login during this pass (not stored in this doc).

### 2026-10-29 — Customer sync: Fetch, controlled export, no cross-instance merge

**What changed**

- Depends on base `20.0.1.3.0` partner connected-instance fields.
- `models/res_partner.py` — `woo_sync_to_store` (default False), `woo_sync_state`, Export / Update /
  Fetch actions, dirty marking, selected-record enqueue with instance picker.
- `models/kits_connector_instance_customer.py` — import match is **binding-only** (never email/phone
  merge across or within stores for registered customers); export only `woo_sync_to_store` or
  explicit `partner_ids`; email-conflict create binds existing remote; `_woo_fetch_customer_by_id`.
- `wizard/kits_woo_customer_fetch.py` + picker `partner_ids` + partner views/security.
- `__manifest__.py` — `20.0.1.5.0`.
- `tests/test_woo_customer_sync.py` — multi-instance email isolation, re-fetch, export flag,
  selected IDs, retry/no-duplicate, email conflict bind, connected instances.

**Why**

- Align customer UX with product-style Push/Pull + Ksolves-style Fetch/Export workflow without
  copying Ksolves code; prevent accidental mass export and cross-store partner merges.

**How it works**

- Fetch wizard: store + Woo customer ID → update mapped partner or create a new one, then open it.
- Export: requires explicit selection / `woo_sync_to_store`; instance picker when multiple stores.
- Bindings remain the only map; retries update the same external ID.

**Testing**

- Automated: `-u kits_odoo_base_connector,kits_odoo_woocommerce_connector
  --test-tags=/kits_odoo_woocommerce_connector` on `odoo20-connectors-woo-tests` →
  **0 failed, 0 error(s) of 30 tests**.
- Live (`odoo20-connectors` + Woo): fetch Woo customer **4** → partner **77**, re-fetch same ID;
  export partner with flag → Woo **19**, retry `updated=1` / one binding; Connectors tab +
  Export/Update/Fetch buttons observed in browser on partner 77.
- **Not browser-clicked through** every wizard field combination in this pass (Fetch wizard opened
  via shell/`_woo_fetch_customer_by_id`; UI buttons verified present under More).

### 2026-10-29 — Customer webhooks (HMAC) + icon audit

**What changed**

- Depends on base `20.0.1.5.0` (`sync_state=deleted` / `_kits_mark_deleted_remote`).
- `models/kits_connector_instance_webhook.py` — per-instance webhook fields; register/unregister;
  HMAC-SHA256 verify (`X-WC-Webhook-Signature` = base64(HMAC-SHA256(raw_body, secret)));
  enqueue with delivery_id / body_hash idempotency + stale pending supersede; process
  created/updated via `_woo_import_one_customer` (binding-only, no email merge); deleted →
  mark binding Deleted Remote **without** deleting the Odoo partner.
- `controllers/woo_webhook.py` — public `POST|GET` `/kits_woo/webhook/<instance_id>/customers`
  (`csrf=False`, `auth=public`); GET probe for Woo delivery URL checks.
- `services/woo_api.py` — webhook CRUD; `models/kits_connector_sync_job.py` —
  `_run_woo_webhook_customer`; views: Customer Webhooks page + Register/Unregister header
  buttons; `__manifest__.py` `20.0.1.6.0`; controllers wired in `__init__.py`.
- Icons (Odoo 20 Material / `oi` data-icon, not Font Awesome): instance smart buttons already
  `checklist` / `article` / `link`; partner Fetch `manage_search`; Export `cloud_upload`;
  Update `sync_alt`; product Export/Update same. No `fa-*` left in either module’s views.

**Why**

- Match App Store connector UX (Ksolves/Webkul/Emipro shape only — no proprietary copy): near-real-time
  customer create/update/delete with secure verification, instance isolation, and soft-delete mappings.

**How it works**

1. Set Public Odoo Base URL → Register Customer Webhooks (writes Woo webhook IDs into `woo_webhook_ids`).
2. Woo POSTs signed JSON to the instance URL; Odoo verifies HMAC, enqueues `woo_webhook_customer`.
3. Job cron / Run Now applies import or soft-deletes the binding.
4. Existing Fetch/Export/`woo_sync_to_store`/cron/product/order paths unchanged.

**Setup (public HTTPS)**

- Production: expose Odoo over HTTPS; put that origin in `woo_webhook_public_base_url`.
- Dev: local WordPress may use `http://127.0.0.1:2099` if it can reach Odoo.
- Multi-database Odoo: configure `dbfilter` **or** ensure delivery includes `X-Odoo-Database`
  (Woo cannot set custom headers easily — prefer single DB / dbfilter in production).

**Testing**

- Automated on `odoo20-connectors-woo-tests`:
  `-u kits_odoo_base_connector,kits_odoo_woocommerce_connector --test-tags=/kits_odoo_woocommerce_connector`
  → **0 failed, 0 error(s) of 42 tests** (includes HttpCase signature reject/accept + GET probe,
  create/update/delete soft-delete, idempotency, supersede, register mock).
- Live `odoo20-connectors` + Woo `localhost/wordpress` (instance **9**):
  | Check | Result |
  |-------|--------|
  | Bad HMAC → 401 | **Pass** (with `X-Odoo-Database`) |
  | create/update → partner **81**, name Webhook Updated, binding synced | **Pass** |
  | Same `delivery_id` → one job | **Pass** |
  | delete → binding `deleted`, partner **81** still active | **Pass** |
  | Store B no binding for external 99001 | **Pass** |
  | Stale pending superseded | **Pass** |
  | Register Customer Webhooks on Woo (topics → webhook ids 1/2/3) | **Pass** |
  | Existing partner **77** / product tmpl **88** bindings still synced | **Pass** |
  | Browser: Customer Webhooks tab + Register button; smart icons `checklist`/`article`/`link`; partner More → `link`/`cloud_upload`/`sync_alt`/`manage_search`; product Export/Update icons | **Pass** (rendered `::before` / `data-icon` inspected) |

**Limitations**

- Without `dbfilter`/single DB, unauthenticated webhook POSTs to a multi-DB host return “No database is selected” unless the caller sends `X-Odoo-Database`.
- Live Woo→Odoo push of a real WP admin customer edit was not waited on beyond Register + simulated signed POSTs (HMAC path fully exercised).

### 2026-10-29 — Emipro-aligned expansion (catalog, coupons, maps, orders, webhooks)

**What changed (Woo `20.0.1.7.0`, base `20.0.1.6.0`)**

- **Catalog:** `kits_connector_instance_catalog.py` — hierarchical category import/export;
  tag import; attribute + term import with bindings (`product.attribute` /
  `product.attribute.value` external ids `attrId:termId`). Product
  `woo_catalog_visibility` / `woo_featured` import+export.
- **Coupons:** model `kits.woo.coupon` + import/export jobs; required-field validation;
  per-instance uniqueness on code; soft-delete via webhook.
- **Payment/shipping maps:** `kits.woo.payment.gateway.map`,
  `kits.woo.shipping.method.map`; fetch from Woo; applied on order import for
  `carrier_id` / shipping product override / payment method display. **Never** invents
  account payments or unpaid invoices.
- **Orders:** stores payment method fields; safer cancel for `failed`; unsupported status
  logging; refund sync creates credit notes **only** when a posted invoice exists
  (idempotent binding `account.move` / `refund:<id>`); tracking export to Woo meta + note.
- **Webhooks:** generalized `/kits_woo/webhook/<instance_id>/<resource>` for
  `products|orders|coupons|customers`; resource register/unregister; product webhooks
  **strip stock fields** (Odoo inventory SoT). Customer webhooks unchanged and non-conflicting.
- Wizard operations extended: categories/tags/attributes/coupons.
- Icons: tracking `local_shipping`; gateway/shipping fetch `payments` /
  `local_shipping`.
- Tests: `tests/test_woo_catalog_coupon_webhook.py` (+ HttpCase product webhook).

**Public HTTPS**

- Same requirement as customer webhooks: set `woo_webhook_public_base_url` to a URL
  WooCommerce can reach; prefer `dbfilter`/single DB in production.

**Testing**

- Automated on `odoo20-connectors-woo-tests`:
  `--test-tags=/kits_odoo_woocommerce_connector` → **0 failed, 0 error(s) of 53 tests**.
- Live `odoo20-connectors` + Woo (instance **9**): fetch payment gateways (bacs/cheque/cod),
  shipping methods (flat_rate/free_shipping/local_pickup/…), category import job
  `updated=1` **Pass**. Full live refund/credit-note and browser icon re-pass for every
  new button **not** re-executed in this segment (refund path covered by unit logic;
  prior icon pass still valid for shared Material icons).

**Known limitations** (superseded by 20.0.1.8.0 entry below where addressed)

- Attribute export wizard path not provided (import + product export cover variants).
- Live end-to-end Woo admin edit → webhook delivery for products/orders/coupons was not
  waited on in this pass (registration/HMAC/job paths covered by tests + prior customer live).

### 2026-10-30 — ai.md current-state reconciliation

**Version:** Woo `20.0.1.9.5` (docs only).

- Refreshed live sections (purpose, workflows, status, decisions, limitations) so they
  match code: customer import-only + bindings; product multi-store M2M + per-store
  bindings; no partner store M2M; webhook/variant delete behavior; removed stale
  “webhooks not implemented” / “partner export” notes. Historical changelog kept.

### 2026-10-30 — Remove customer export flow

**Version:** Woo `20.0.1.9.5`.

**What changed**

- Removed Odoo → Woo customer export (store button, partner Export action,
  `woo_sync_to_store` on partners, export helpers).
- Import / Fetch / Update-from-Woo / webhooks / order partner resolution unchanged.
- Sync wizard + legacy `woo_export_customers` jobs raise a clear error if used.

### 2026-10-29 — Revert customer match to Woo customer_id bindings

**Version:** Woo `20.0.1.9.4`.

**What changed**

- Reverted email-only matching (`20.0.1.9.2` / `20.0.1.9.3`).
- Customers map again via `kits.connector.binding` on Woo **customer id** per store.
- Same email on different stores (or unbound local contact) creates **separate** partners.
- Delete webhook marks binding `deleted` remote; partner kept.
- Removed partner `woo_instance_ids` M2M (products keep their own store M2M).

### 2026-10-29 — Customer match by email; no Woo customer id stored (reverted)

**Version:** Woo `20.0.1.9.3` — **reverted** in `20.0.1.9.4`.

### 2026-10-29 — Customer match by email (no duplicate partners) (reverted)

**Version:** Woo `20.0.1.9.2` — **reverted** in `20.0.1.9.4`.

### 2026-10-29 — WooCommerce submenu under Connectors

**Version:** Woo `20.0.1.9.1`.

- Added parent menu **Connectors → WooCommerce** (only when this module is installed).
- Nested under it: Stores, Sync Operation, Coupons, Refunds.
- Configuration → WooCommerce Stores shortcut kept for managers.

### 2026-10-29 — Product store selection (M2M) + auto product export cron

**Version:** Woo `20.0.1.9.0`.

**What changed**

- `product.template.woo_instance_ids` — Many2many of WooCommerce stores on the
  product **WooCommerce** tab. Export (manual, store Export Products, cron) only
  targets selected stores.
- `woo_sync_to_store` is now computed from whether any store is selected (checkbox
  OFF clears stores; ON with a single company store auto-selects it).
- Instance flag `woo_auto_export_products` — scheduled cron enqueues export for
  products that list that store and are new / not synced / error.
- Import from a store auto-adds that store to `woo_instance_ids`.
- Upgrade migration links existing template bindings into the M2M.

**How to use**

1. On the product → WooCommerce tab → select Store A and/or Store B.
2. Enable **Auto Export Products** on each store that should cron-export.
3. Manual Export / store Export Products only pushes to stores selected on the product.

### 2026-10-29 — Production fixes: tax maps, coupon bindings, refunds, DB allowlist

**Versions:** Woo `20.0.1.8.0`, base `20.0.1.7.0`.

**What changed**

- **Tax mapping** (`kits.woo.tax.map` + `kits_connector_instance_tax.py`):
  - Per-instance Woo rate id / class → existing `account.tax` (or Zero/Exempt).
  - Modes: `mapped` | `fallback_clear` (default, legacy-safe) | `fallback_product`.
  - Fetch rates from Woo (`GET /taxes`); never invents Odoo taxes.
  - Exact rate-id match only (no silent class fallback when Woo sends rate ids).
  - Tax total discrepancy logged on SO; totals never silently rewritten.
  - UI: instance **Tax Maps** tab + Fetch Tax Rates.
- **Coupon restrictions** (`kits.woo.coupon`):
  - M2M included/excluded products and categories via **this store’s** bindings.
  - Raw Woo ID text retained when unmapped/deleted so eligibility is never silently broadened.
  - Export blocked while `restriction_error` / unresolved IDs remain.
- **Refunds** (`kits.woo.refund`):
  - Stable per-instance `woo_refund_id`; states pending/review/done/skipped/error.
  - Partial + multi-invoice allocation; draft credit notes by default
    (`woo_auto_post_credit_notes` opt-in). No invented invoices/payments/stock returns.
  - Idempotent retries; menu **WooCommerce Refunds** + Retry Sync.
- **Webhook DB routing** (`controllers/woo_webhook.py`):
  - System parameter `kits_woo.webhook_db_allowlist` (comma-separated).
  - Uses Odoo 20 `ir.config_parameter.get_str` / `set_str`.
  - When set, active DB (and any `X-Odoo-Database`) must be allowlisted; mismatch → 403.
  - Prefer `dbfilter`/single DB; never trust arbitrary DB names; no secrets in logs.
- **Tests:** `tests/test_woo_tax_coupon_refund_webhook.py` (+ updates to coupon export test).

**Public HTTPS / multi-DB**

1. Set `woo_webhook_public_base_url` to a URL Woo can reach (tunnel for local Odoo).
2. Prefer `dbfilter` or single database in production.
3. If multi-DB + `X-Odoo-Database` is required, set
   `kits_woo.webhook_db_allowlist=<dbname1>,<dbname2>`.
4. Register resource webhooks (products/orders/coupons) separately from customers.
5. Point any HTTPS tunnel at the Odoo HTTP port (e.g. `:2099`), not an unrelated port.

**Live webhook verification procedure**

1. Expose Odoo: `ngrok http 2099` (or equivalent) → set Public Odoo Base URL.
2. Set allowlist if multi-DB: Settings → Technical → System Parameters.
3. On the store: Register Customer Webhooks + Register Catalog/Order/Coupon Webhooks.
4. Edit a product / order / coupon in WP admin; confirm Woo delivery + Odoo job done.
5. Confirm product webhook does **not** overwrite Odoo stock.
6. Delete remote coupon/product: binding → Deleted Remote; Odoo record remains.
7. Replay same Delivery-ID → one job (idempotent). Bad signature → 401.

**Testing**

- Automated `odoo20-connectors-woo-tests`:
  `--test-tags=/kits_odoo_woocommerce_connector` → **0 failed, 0 error(s) of 64 tests**.
- Live `odoo20-connectors`: module upgrade OK; tax fetch OK (0 Woo rates on store);
  HMAC sim `POST …/products` → 200 + job enqueued (with `X-Odoo-Database`).
- **Live Woo→Odoo product/order/coupon delivery: NOT verified** — blockers:
  - Public URL is loopback `http://127.0.0.1:2099`
  - Existing ngrok pointed at `:3586`, not Odoo `:2099`
  - Only customer webhooks registered on Woo for instance 9

**Remaining limitations**

- Attribute export wizard still absent.
- Compound/group Odoo taxes and complex fiscal-position rewrites are not auto-derived
  from Woo; administrators map concrete sales taxes.
- Partial refund line scaling uses quantity/price adjustment on draft credit notes;
  exotic multi-tax/multi-invoice edge cases may stay in **Needs Review**.
- Live product/order/coupon webhook E2E still needs a correct HTTPS tunnel + register.

### 2026-10-30 — Deletion, SKU ambiguity, stock isolation, import-only customer details

**Version:** Woo `20.0.1.10.0`, base `20.0.1.8.0`.

**What changed**

- Ambiguous SKU import logs and skips. Exactly one unbound SKU still links. No name matching.
- `trash` and `product.deleted` mark only that store deleted remote, including variant bindings, and remove that store from `woo_instance_ids`. The Odoo product stays active. Repeat events are idempotent.
- Odoo product/variant `unlink` sets `deleted_local` and does not call Woo delete. Automatic import does not recreate the product. Selected import/export may pass `allow_recreate`.
- Scheduled product export sends `automatic: true` and skips deleted bindings. A 404 during export marks deleted remote instead of creating a product.
- Stock export skips deleted bindings, reads the current free quantity, skips stale versions, and on 404 does not create a product. One store’s enqueue failure does not block the others. Field edits no longer flip deleted bindings back to not synced.
- Product webhooks ignore stock fields (unchanged) and skip a payload whose `date_modified` is older than the binding’s stored remote timestamp.
- Customer import stores phone, mobile meta (on the binding when `res.partner` has no `mobile` field), and billing company as a new parent contact per customer. Customer export remains unavailable.
- Refund success log no longer throws when a draft credit note has no name, so a completed refund is not stamped with a false order conflict.
- Cost, when Woo sends `cost` or cost meta (`_wc_cog_cost`, `_alg_wc_cog_cost`), is written to `standard_price`. Missing cost does not overwrite Odoo. Variation id and a short meta snapshot are stored on the binding.

**Tests (executed)**

- Database `odoo20-connectors-woo-tests`, `--test-tags=/kits_odoo_woocommerce_connector`:
  **0 failed, 0 error(s) of 72 tests** (2026-10-30).
- Not executed: live WooCommerce API, WP admin, or the browser on `odoo20-connectors`.

**Modified files**

- Base: `models/kits_connector_binding.py`, `views/kits_connector_binding_views.xml`, `__manifest__.py`, `ai.md`
- Woo: `models/kits_connector_instance.py`, `kits_connector_instance_product.py`, `kits_connector_instance_inventory.py`, `kits_connector_instance_customer.py`, `kits_connector_instance_webhook.py`, `kits_connector_instance_refund.py`, `stock_move.py`, `product_template.py`, `product_product.py`, `res_partner.py`, `wizard/kits_woo_sync_wizard.py`, `tests/test_woo_deletion_rules.py`, `tests/test_woo_record_sync.py`, `tests/__init__.py`, `__manifest__.py`, `ai.md`
