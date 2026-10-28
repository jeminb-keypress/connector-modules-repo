# kits_odoo_woocommerce_connector — AI Handover Document

> **Mandatory for future AI agents:** Read this file **and** `kits_odoo_base_connector/ai.md` before modifying this module. After every code change, append a dated changelog entry below. Never erase previous history.

---

## 1. Module purpose

Odoo 20 WooCommerce connector that syncs products, sales orders, and inventory for one or more WooCommerce stores. Built as a **separate** addon under `custome/20.0/kits_odoo_woocommerce_connector` (sibling of the base connector — not nested inside it).

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
| `woo_export_customers` | `_run_woo_export_customers` | Odoo → Woo customers |
| `woo_import_orders` | `_run_woo_import_orders` | Woo → confirmed sale orders |
| `woo_export_inventory` | `_run_woo_export_inventory` | Odoo free qty → Woo stock |

---

## 3. File responsibilities

| Path | Responsibility |
|------|----------------|
| `__manifest__.py` | Depends: base connector, sale_management, sale_stock, stock, website_sale |
| `services/woo_api.py` | WooCommerce REST facade (paginated lists, CRUD, 429 backoff) |
| `models/kits_connector_instance.py` | Woo fields, auth hooks, connection test, enqueue actions, schedule cron |
| `models/kits_connector_instance_image.py` | Main/gallery/variant image sync (gallery via `product.image` if `website_sale` present) |
| `models/kits_connector_instance_product.py` | Product import/export + attributes/variants/tags/barcode |
| `models/kits_connector_instance_customer.py` | Dedicated customer import/export + guest-safe order partner helper |
| `models/kits_connector_instance_order.py` | Order import, failure retry list, lines, shipping/discount, confirm |
| `models/kits_connector_instance_inventory.py` | Warehouse-scoped inventory export |
| `models/kits_connector_sync_job.py` | `_run_woo_*` dispatch wrappers |
| `models/product_*.py` / `sale_order.py` | UI/tracking fields |
| `tests/` | Mocked API TransactionCase coverage |
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
- **Auto Import Orders** / **Auto Export Inventory** flags

API base resolved as: `{base_url}/wp-json/wc/v3`.

Auth: HTTP Basic Auth via `requests` `auth=(key, secret)` injected in `_kits_get_auth_headers` (secrets never logged).

---

## 5. Workflows

### Products import
1. Paginate `GET /products` (all statuses; draft/private → `active=False`)
2. Match: binding → optional unbound SKU (`woo_match_by_sku`) → create
3. Categories/tags/barcode/status; variable attributes + variations
4. Images: main + optional gallery (`product.image` when `website_sale` installed); hash skip

### Products export
1. Prefer templates with `woo_sync_to_store`; else up to 200 saleable templates
2. Create/update Woo products with attributes/tags/images; bind IDs; export variations

### Customers import/export
1. Import `GET /customers` with binding → validated email → create
2. Export bound partners (+ capped unbound with email); email required by Woo
3. Guests (`customer_id` 0) never get customer bindings — order path only

### Orders import
1. Retry `woo_order_import_failures` by ID first, then paginate with `after=woo_last_order_sync`
2. Skip if `sale.order` binding already exists (idempotent); record cancel/refund conflicts
3. Partner via `_woo_partner_from_order`; map lines (binding/SKU); fail unmapped unless allowed
4. Shipping + discount + fee lines; tax-inclusive Woo clears Odoo line taxes
5. Bind order **before** `action_confirm()`; never invent invoices/payments/stock moves

### Inventory export
1. All `product.product` bindings for the instance
2. `free_qty` with `warehouse_id` context from store warehouse
3. `PUT` product or variation stock fields

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
| Image sync (main/variant; gallery if `website_sale`) | **Done** |
| Dedicated customer import/export | **Done** |
| Order import + auto confirm + failure retry list | **Done** |
| Inventory export | **Done** |
| Manual + scheduled jobs | **Done** |
| Bindings / logs / retries via base | **Done** |
| Mocked TransactionCase tests | **Added** (DB run pending) |
| Webhooks | **Not in scope** |
| Invoices / payments / refunds | **Not in scope** |

---

## 9. Technical decisions

1. Separate module folder under `custome/20.0/` — does not live inside `kits_odoo_base_connector`.
2. Reuse base HTTP client; Woo API layer only adds pagination + rate-limit sleep.
3. Job handlers are thin; business logic lives on `kits.connector.instance`.
4. Order binding written before confirm to keep retries idempotent.
5. Unmapped order lines **fail the order by default** (`woo_allow_unmapped_order_lines=False`); optional fallback to `WOO_MISC`.
6. Depends on `sale_stock` so `sale.order.warehouse_id` is available.
7. Gallery images use `product.image` only when `website_sale` is installed (no hard dependency).
8. Failed order IDs stored in `woo_order_import_failures` and retried by ID so date checkpoints cannot skip them.

---

## 10. Known limitations / pending

- Variant matching on import can miss exotic attribute option naming mismatches.
- Tax mapping is minimal (uses Odoo product/fiscal defaults; Woo tax lines not fully mirrored).
- Product export without `woo_sync_to_store` falls back to a 200-product cap.
- Coupon/fee lines beyond `discount_total` are not fully modeled.
- Webhooks not implemented.
- DB install / live Woo API end-to-end tests **not executed** in this environment (per task instruction / Postgres historically unavailable).

### Pending

- [ ] Install on a live DB and smoke-test against a WooCommerce sandbox
- [ ] Add TransactionCase tests for idempotent order import and binding conflicts
- [ ] Optional: map Woo taxes more precisely

---

## 11. Validation results

| Check | Result |
|-------|--------|
| Python `py_compile` | **Passed** |
| XML well-formedness + manifest data paths | **Passed** |
| Odoo `Manifest.for_addon` discovery | **Passed** |
| Import via `odoo.addons.kits_odoo_woocommerce_connector` | **Passed** |
| Live DB install | **Not executed** (not claimed) |
| Live Woo API sync | **Not executed** |

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
