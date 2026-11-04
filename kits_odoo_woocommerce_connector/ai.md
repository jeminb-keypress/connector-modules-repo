# kits_odoo_woocommerce_connector — AI Handover Document

> **Mandatory for future AI agents:** Read this file **and** `kits_odoo_base_connector/ai.md` before modifying this module. After every code change, append a dated changelog entry below. Never erase previous history.

---

## 1. Module purpose

Odoo 20 WooCommerce connector that syncs products, customers (import-only), sales orders, coupons, and inventory for one or more WooCommerce stores. Built as a **separate** addon under `custome/20.0/kits_odoo_woocommerce_connector` (sibling of the base connector — not nested inside it).

**Current version:** `20.0.1.14.2`. Depends on base `20.0.1.9.1`.

The WooCommerce app menu is owned by this module. The base module has no menu.

Depends on `kits_odoo_base_connector` for instances, HTTP client, sync jobs, logs, and bindings.

Inspired by Emipro / TeqStars WooCommerce apps for feature shape only — **no proprietary code copied**.

---

## 2. Architecture

```
kits_odoo_woocommerce_connector
    │
    ├─ extends kits.connector.instance   (platform=woocommerce, store settings, actions)
    ├─ extends kits.connector.sync.job   (_run_woo_* handlers)
    ├─ kits.woo.customer.binding         (store + Woo customer id → res.partner)
    ├─ kits.woo.product.binding          (store + Woo product id → product.template)
    ├─ kits.woo.variant.binding          (store + Woo variation id → product.product)
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
| `woo_import_coupons` / `woo_export_coupons` | `_run_woo_*_coupons` | Coupon import, export, and update |
| `woo_export_tags` | `_run_woo_export_tags` | Tag export and update |
| `woo_export_shipments` | `_run_woo_export_shipments` | Set Woo orders to completed after Odoo delivery |
| `woo_webhook_product` / `order` / `coupon` | `_run_woo_webhook_*` | Non-customer webhooks |

### Menus

The base module has no app menu. This module owns WooCommerce.

1. Stores
2. Sale: Orders, Customers, Customer Bindings, Refunds
3. Product: Products, Product Bindings, Variant Bindings
4. Sync Operations (connector administrator)
5. Sync Jobs
6. Logs
7. Coupons
8. Configuration: WooCommerce Settings (connector administrator)

There is no generic Bindings menu and no delivery-order menu. Orders are `sale.order` rows with `woo_order_id`. Customers are top-level contacts with `woo_is_store_customer`. Products are templates with `woo_instance_ids`. Sync Jobs and Logs open the base models filtered to `platform_type = woocommerce`.

---

## 3. File responsibilities

| Path | Responsibility |
|------|----------------|
| `__manifest__.py` | Version `20.0.1.14.2`. Depends: base connector, sale_management, sale_stock, stock, website_sale, delivery, payment, account |
| `services/woo_api.py` | WooCommerce REST facade (paginated lists, CRUD, 429 backoff) |
| `models/kits_connector_instance.py` | Woo fields, auth hooks, connection test, enqueue actions, schedule cron |
| `models/kits_connector_instance_image.py` | Main/gallery/variant image sync via `website_sale` `product.image`; export uploads via WP XML-RPC |
| `models/kits_woo_customer_binding.py` | Customer binding: partner, store, external customer id |
| `models/kits_woo_product_binding.py` | Product binding and variant binding |
| `models/kits_woo_binding_sync.py` | Shared sync state for product and variant bindings |
| `models/kits_connector_instance_product.py` | Product import/export + attributes/variants/tags/barcode |
| `models/kits_connector_instance_customer.py` | Dedicated customer import (no export) + guest-safe order partner helper |
| `models/kits_connector_instance_order.py` | Order import, failure retry list, lines, shipping/discount, confirm |
| `models/kits_connector_instance_inventory.py` | Warehouse-scoped inventory export, including Export Stock From |
| `models/kits_connector_instance_mapping.py` | CSV/Excel product mapping. Links existing products only. |
| `models/account_move.py` | Refund in WooCommerce from a customer credit note |
| `models/kits_connector_instance_webhook.py` | Customer webhook fields, HMAC verify, register/enqueue/process |
| `controllers/woo_webhook.py` | Public HTTP endpoint `/kits_woo/webhook/<id>/customers` |
| `models/kits_connector_sync_job.py` | `_run_woo_*` dispatch wrappers |
| `models/product_*.py` / `sale_order.py` | UI/tracking fields |
| `tests/` | Mocked API TransactionCase + HttpCase webhook coverage |
| `data/product_data.xml` | Shipping + discount service products |
| `data/ir_cron_data.xml` | Auto-schedule cron (15 min) |
| `views/kits_woocommerce_menus.xml` | WooCommerce app menu. See Menus above. |
| `views/*` | Store form, bindings, coupons, refunds, orders, products, customers, sync wizard |
| `wizard/kits_woo_sync_wizard.py` | One Sync Operation wizard with 20 operations (`20.0.1.14.1`). Company defaults to the current company. Mapping runs immediately. Every other operation queues a job and opens Sync Jobs. |
| `wizard/kits_woo_customer_fetch.py` | Fetch one customer. Same company default as Sync Operation. |
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
- **Synchronize Price** (`woo_sync_price`, default True)
- **Synchronize Weight** (`woo_sync_weight`, default False)
- **Create Products Automatically** (`woo_create_products`, default True)
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

### Operations wizard

WooCommerce → Sync Operations opens `kits.woo.sync.wizard`. The store is required. The form shows only the fields for the selected operation.

| Operation | Fields shown | How it runs |
|-----------|--------------|-------------|
| Import Customers | none | Job `woo_import_customers` |
| Import Products | Import All, or Import New and Updated Only; Do Not Update Existing Products | Job `woo_import_products` |
| Import Specific Products | WooCommerce product ids | Job `woo_import_products` |
| Import Unshipped Orders | Date From, Date To | Job `woo_import_orders`. Statuses `pending`, `processing`, `on-hold` |
| Import Shipped Orders | Date From, Date To | Job `woo_import_orders`. Status `completed` |
| Import Cancelled Orders | none | Job `woo_import_orders`. Status `cancelled` |
| Import/Update Specific Orders | WooCommerce order ids | Job `woo_import_orders` |
| Import Categories | none | Job `woo_import_categories` |
| Import Tags | none | Job `woo_import_tags` |
| Import Attributes | none | Job `woo_import_attributes` |
| Import Coupons | none | Job `woo_import_coupons` |
| Map Products using CSV/Excel | mapping file | Immediate. No job |
| Export Shipment Information / Update Order Status | none | Job `woo_export_shipments` |
| Export Stock | Export Stock From | Job `woo_export_inventory` |
| Export Categories | none | Job `woo_export_categories`, `category_mode=export` |
| Update Categories | none | Job `woo_export_categories`, `category_mode=update` |
| Export Tags | none | Job `woo_export_tags`, `tag_mode=export` |
| Update Tags | none | Job `woo_export_tags`, `tag_mode=update` |
| Export Coupons | none | Job `woo_export_coupons`, `coupon_mode=export` |
| Update Coupons | none | Job `woo_export_coupons`, `coupon_mode=update` |

Import stock is not in the list. Product export stays on the product form and the product Action menu.

A queued operation opens WooCommerce → Sync Jobs for that job. If the same store, job type, and payload are already pending or running, that job is opened and a second job is not created. Failed jobs use the base retry limit and backoff. Mapping, publish, unpublish, remote cancel, tracking export, the single-order status button, and the credit-note refund run immediately and show a notification.

### Record buttons

- Product form and product list: Export to WooCommerce, Update from WooCommerce, Publish on WooCommerce, Unpublish on WooCommerce. Publish and unpublish change the WooCommerce status on each selected store. They do not archive the Odoo product.
- Sales order: Update Woo Status, Cancel in WooCommerce, Export Tracking. Cancel in WooCommerce does not cancel the Odoo order. Export Tracking writes the tracking meta and a customer note. It does not change the WooCommerce status.
- Customer credit note: Refund in WooCommerce. One remote refund per credit note. The amount cannot exceed the order total minus refunds already marked done. No second credit note is created. The WooCommerce call uses `api_refund` false.

### Products import
1. Paginate `GET /products`. `trash` and a remote delete archive this store’s product binding and its variant bindings (`active=False`, `sync_state=deleted`, `exists_in_store=False`) and drop only this store from Connected Instances. The Odoo template and variants stay.
2. Validate every variant before writing. Each variant needs its own SKU. A repeated SKU inside the same Woo product fails the product. A variable product with no option values fails. The product import uses a savepoint, so a failed product leaves no partial binding.
3. Match, in order: variant binding for this store + external variation id → product binding for this store + the same external id (the template’s variant) → variant binding for this store + SKU (when `woo_match_by_sku`) → exactly one `product.product` with that SKU in this company or with no company. Several Odoo variants with that SKU fail the product. A simple product uses the Woo product id as the external variant id.
4. When an Odoo product matches, reuse it. Create this store’s product binding if it is missing, then the variant binding. Do not rewrite the Odoo name, description, tags, SKU, barcode, or cost. Price, images, and weight are written only when `woo_sync_price`, `woo_import_images`, and `woo_sync_weight` are on. Cost is written only when the Odoo variant is created.
5. When nothing matches, create the Odoo product only if `woo_create_products` is on. Otherwise fail that product and log the reason. Deleting the Odoo product removes its bindings and does not leave a tombstone, so a later import can create again when automatic creation is on.
6. Store title, short and long descriptions, type, published/draft, taxable, virtual, categories, tags, image URLs, and store dates stay on `kits.woo.product.binding`. Variant title, SKU, URL, manage-stock, quantity rule, and variant images stay on `kits.woo.variant.binding`.
7. Images (if `woo_import_images`): main → `image_1920`; extras → `product_template_image_ids` (`website_sale` required); variant → `image_variant_1920`; hash skip; **videos out of scope**
8. Import All updates matched products. Import New and Updated Only skips a linked product whose WooCommerce modification date is not newer than `store_write_date`. Do Not Update Existing Products skips every product that already has a binding for this store. Specific ids import only those WooCommerce products. Automatic creation and the match order above still apply.

### Product mapping

CSV and xlsx. The header needs a WooCommerce product id (`woo_id` or `woo_product_id`) and either `odoo_id` or `sku`. Every row is validated before any binding is written. Missing products, invalid ids, duplicate rows, ambiguous SKUs, and a Woo id or Odoo product already linked to something else are reported and not applied. Mapping does not create or delete products. A single-variant product also gets a variant binding with the same Woo id. A product with several variants is linked on the template only. The binding belongs to the store selected on the wizard.

### Products export
1. Templates that list this store on `woo_instance_ids` (drives computed `woo_sync_to_store`), or explicit `template_ids` (no silent mass fallback)
2. Map by **binding** for this store (update Woo id) else **create** on Woo and bind — export does **not** match Woo by SKU. `deleted` / `deleted_local` bindings are skipped unless the job has `allow_recreate` (selected Export / wizard IDs). Recreate first checks the old id; a 404 may create one new product and rebind. Scheduled cron sets `automatic` and never recreates. A 404 on an automatic update marks the binding deleted remote and does not create.
3. Push name/price/desc/SKU/attrs/tags/images; export variations for current Odoo variants only (**does not** delete orphan Woo variations)
4. Image export: upload bytes via WP XML-RPC then attach by media id (modern Woo rejects `data:` URIs); needs `config_json` WP credentials
5. **Not realtime:** Odoo product edits do not push until Export Products / scheduled job runs
6. Multi-store: one Odoo template can have **one binding per store** (different Woo product ids)

### Customers (import only — no Odoo→Woo customer export)
1. Search `kits.woo.customer.binding` by the current store and the Woo customer id. When found, use that contact and do not change its name, email, phone, or existing addresses. Missing child addresses may still be added.
2. Otherwise search top-level contacts in this company by case-insensitive email, across stores. Reuse the contact, create a binding for this store, and do not change its existing details. If that contact is already bound to a different Woo customer on this store, skip it.
3. Otherwise create the contact from the Woo customer and create the binding. A customer with no first or last name is skipped and logged. A company name alone is not a person name. `woo_company_name` is set only when the contact is created.
4. Billing and shipping are child contacts (`invoice` and `delivery`). Before creating one, exact-match an existing child on name, street, city, zip, state, and country. Phone is part of the billing match only. Reuse an exact match. Never write an existing address. An empty location is skipped.
5. The Woo customer id lives only on `kits.woo.customer.binding.external_id`. The binding has no sync payload and no `external_data`.
6. **Customer export is not supported** (UI/wizard/job removed in `20.0.1.9.5`).
7. Guests (`customer_id` 0) never get customer bindings. The order path may reuse or create a contact by billing email and keeps the address on that contact.
8. `customer.deleted` deletes this store’s customer binding, clears the store-customer indicator, and keeps the Odoo contact. A repeated event is safe.
9. Deleting the Odoo contact deletes its customer bindings and does not call WooCommerce.
10. `kits_connected_instance_ids` and `woo_is_store_customer` are computed from customer bindings. The Customers menu searches `woo_is_store_customer` with Odoo 20’s operator `in` and value `[True]`. `_search_woo_is_store_customer` treats that as true. Treating an unknown operator as “not a customer” inverts the list.

### Orders import
1. Retry `woo_order_import_failures` by ID first, then paginate with `after=woo_last_order_sync`. A scoped wizard job does not retry the failure list and does not move the watermark.
2. Unshipped uses Woo statuses `pending`, `processing`, and `on-hold`. Shipped uses `completed`. Cancelled uses `cancelled`. Specific orders use the given Woo ids. Date From and Date To apply to shipped and unshipped imports.
3. An order that is already linked is updated in place: status, payment metadata, safe cancellation, and refunds. Confirmed lines are not rewritten.
4. A cancelled Woo order does not create an Odoo order. If one is already linked, it is cancelled only when it has no done delivery and no invoice. Otherwise the skip reason is logged on the order and in Logs.
5. Partner via `_woo_partner_from_order`; map lines (binding/SKU); fail unmapped unless allowed
6. Shipping + discount + fee lines; tax-inclusive Woo clears Odoo line taxes
7. Bind order **before** `action_confirm()`; never invent invoices/payments/stock moves
8. A confirmed order’s delivery is a standard stock picking. Open it from the order’s Delivery button, or from Inventory → Operations → Deliveries. WooCommerce has no delivery menu.

### Shipment status
Export Shipment Information sets the WooCommerce order status to `completed` when the Odoo order is confirmed, every outgoing delivery is done, and the Woo status is not already completed, cancelled, refunded, failed, or trash. It does not write a tracking number. Export Tracking remains a separate button and only writes meta plus a customer note.

### Inventory export
1. Active `kits.woo.variant.binding` rows for this store whose state is not `deleted`. Woo stock is never imported.
2. Quantity comes from the variant binding rule: `odoo` uses `free_qty`, `fixed` uses `qty_sync_value`, `percentage` uses `free_qty * qty_sync_value / 100`. The warehouse context is the store warehouse. A job carrying an older `sync_version` is skipped.
3. Export Stock From limits the job to variants that have a done stock move on or after that date.
4. `PUT` uses the variant external id. A variation is updated when its external id differs from the parent product id. Never `POST` a new product. HTTP 404 archives the binding and stops recovery retries.
5. Stock-move completion enqueues each store independently. One store’s enqueue error does not stop the others.

### Categories, tags, and coupons

Import reuses the store binding for the same external id. Category export creates categories that are not yet linked and keeps the parent before the child. Category update only writes categories that already have a binding. Tag export creates a missing WooCommerce tag, or reuses one remote tag with the same name when exactly one exists. Tag update only writes linked tags. Coupon export creates coupons that are not yet linked. Coupon update only writes linked coupons. Unsupported coupon restrictions still block the export.

### Monitoring / retries
- WooCommerce → Sync Jobs and WooCommerce → Logs. The base module has no menu.
- After Queue/Run on a background operation, Sync Jobs opens on that job.
- Failed jobs: reset/enqueue/run from the job form (retry limits + backoff)

---

## 6. External ID mapping conventions

| Odoo model | Binding model | External ID | Notes |
|------------|---------------|-------------|-------|
| `product.template` | `kits.woo.product.binding` | Woo product id | Store title and descriptions stay here. One binding per store. |
| `product.product` | `kits.woo.variant.binding` | Woo product id (simple) or variation id | Belongs to its product binding. Unique per store and external id, and per store and variant. |
| `res.partner` | `kits.woo.customer.binding` | Woo customer id | Partner, store, and external id only. One binding per store and partner. |
| `product.category` | `kits.connector.binding` | Woo category id | Generic binding. Also linked on the product binding. |
| `product.tag` / attributes | `kits.connector.binding` | Woo ids | Generic binding. |
| `sale.order` | `kits.connector.binding` | Woo order id | Idempotency key. `sale.order.woo_order_id` is a stored copy. |
| `kits.woo.coupon` / refunds | `kits.connector.binding` | Woo ids | Generic binding. |

---

## 7. Security

- Reuses base connector User / Administrator groups
- Credentials: `groups='kits_odoo_base_connector.group_kits_connector_manager'` + `widget="password"`
- `kits.woo.customer.binding`, `kits.woo.product.binding`, and `kits.woo.variant.binding` have user read and manager create/write/unlink, with a company domain. Their menus are Customer Bindings under Sale, and Product Bindings and Variant Bindings under Product. `kits.connector.binding` has no WooCommerce menu.
- Company isolation also uses the base instance/job/log/binding rules and `check_company` on warehouse, pricelist, team, and the order store link.
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
| Dedicated customer import | **Done** (store + Woo id, then case-insensitive email reuse; no customer export) |
| Separate customer, product, and variant bindings | **Done** (`20.0.1.13.0`) |
| Record-level customer Fetch / Update | **Done** (Export removed in `20.0.1.9.5`) |
| Customer Odoo→Woo export | **Removed** (`20.0.1.9.5`) |
| Order import + auto confirm + failure retry list | **Done** |
| Inventory export | **Done** (manual bulk + event-driven + cron recovery) |
| Manual + scheduled jobs | **Done** |
| Record-level Push/Pull (products) | **Done** |
| Operations wizard, 20 operations | **Done** (`20.0.1.14.1`). Import stock is not an operation. |
| Product mapping CSV/Excel | **Done**. Links existing products for the selected store. Accepts Odoo 20 `BinaryValue` uploads. |
| Publish / unpublish | **Done**. WooCommerce status only. Odoo `active` is unchanged. |
| Cancel order in WooCommerce | **Done**. Does not cancel the Odoo sales order. |
| Refund from Odoo credit note | **Done**. One WooCommerce refund per credit note. |
| Shipment status export | **Done**. Sets WooCommerce to completed. Separate from tracking export. |
| Tag export / update | **Done** |
| Category and coupon export / update | **Done**. Separate create and update modes. |
| Per-instance sync status | **Done** on product and variant bindings (`sync_state`). Customer bindings have no sync state. Orders, coupons, categories, tags, attributes, and refunds still use the generic binding. |
| Bindings / logs / retries | **Done**. Logs and jobs use the base models through WooCommerce menus. Customer, product, and variant bindings have their own menus. |
| Mocked TransactionCase tests (`kits_woo`) | **Done** (0 failed, 0 errors of 101 tests on `odoo20-connectors-woo-tests`, 2026-11-03; includes wizard matrix for all 20 operations) |
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
13. Remote Woo product deletion archives this store’s product and variant bindings and does not delete or archive Odoo products. A deleted remote variant archives only that variant binding and is not recreated automatically. Odoo `unlink` of a contact, template, or variant deletes the related dedicated bindings and does not call WooCommerce. There is no `deleted_local` tombstone on these three models, so a later import follows the normal match and `woo_create_products` rules.
14. Customers are Woo → Odoo only. Inventory is Odoo → Woo only. Product catalog sync stays bidirectional. One Odoo contact or product may have a separate binding per store. A store writes only its own binding.
15. Generic `kits.connector.binding` remains for orders, coupons, categories, tags, attributes, and refunds. `_woo_find_odoo` and `_woo_bind` route partners, templates, and variants to the dedicated models.

---

## 10. Known limitations / pending

- Variant matching on import can miss exotic attribute option naming mismatches.
- Tax mapping is minimal (uses Odoo product/fiscal defaults; Woo tax lines not fully mirrored).
- Coupon/fee lines beyond `discount_total` are not fully modeled.
- Customer export is not an operation. Stock import is not an operation.
- A product with several variants can be mapped to a WooCommerce product id. The file does not assign variation ids.
- A WooCommerce refund from a credit note is recorded with `api_refund` false, so the payment gateway is not charged again.
- Deleting an Odoo variant then exporting does **not** remove the Woo variation (orphan on Woo).
- Not every edge case has a dedicated test; see changelog testing notes.
- Image change detection still hash-based; partial gallery races can leave an extra `website_sale` row (pre-existing).
- Image export requires WP application/user credentials in `config_json` (XML-RPC); Woo REST alone cannot accept `data:` URIs on modern WP.
- `website_sale` Media gallery may auto-create an extra main-image gallery row on export round-trips (observed 3→4 once, then stable).
- Customer/product webhooks need public base URL + Register; without that, remote deletes do nothing in Odoo until manual sync.
- Odoo 20 base contacts have `phone` and no `mobile`. The customer binding does not store a mobile fallback.
- Billing company text is `res.partner.woo_company_name` on the main contact. Billing and shipping locations are child contacts. Guest order addresses still sit on the guest contact.
- Deleting an Odoo product removes its bindings. The next import can create a new product when Create Products Automatically is on and the SKU no longer matches.
- An archived remote product is skipped by export and stock jobs. A later import can find the inactive binding and reactivate it for the same external id.
- Bulk/scheduled product export does not recreate a remotely deleted product. Only an explicit selected export (`allow_recreate`) does, and only after the old id 404s.
- Stale webhook protection compares `date_modified` / `date_modified_gmt` when both sides have one. Events without timestamps are not treated as stale.
- The automated suite for `20.0.1.14.0` ran on `odoo20-connectors-woo-tests` (0 failed, 0 errors of 90 tests). The browser database `odoo20-connectors` was last upgraded through `20.0.1.13.4` and still needs this upgrade before the new wizard appears there.
- Sync Operation and Fetch Customer set `company_id` to the current company in `default_get`. An empty company hides every store that belongs to a company, because `check_company` then keeps only records with no company.

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
| Live DB install / upgrade | **Passed** on `odoo20-connectors` through `20.0.1.13.4` |
| Live Woo API + browser sync | **Passed** 2026-10-29 for product/stock/wizard/UI. Menus, the customer list, the store dropdown, and Refunds under Sale were checked in the browser on 2026-11-02. |
| Automated `kits_woo` | **0 failed, 0 error(s) of 101 tests** on `odoo20-connectors-woo-tests` (2026-11-03, module `20.0.1.14.1`). Stats line counted 133 tests. |

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

### 2026-11-02 — Independent WooCommerce menu

**Version:** `20.0.1.12.0`.

**Hierarchy** (`views/kits_woocommerce_menus.xml`)

- WooCommerce
  - Stores
  - Sync Operations
  - Coupons
  - Refunds
  - Sync Jobs
  - Logs
  - Bindings
  - Configuration
    - WooCommerce Settings

**Shared versus platform UI**

- Jobs, logs, and bindings remain base models and base list/form/search views.
- This module adds `kits_woo_sync_job_action`, `kits_woo_log_action`, and `kits_woo_binding_action` with domain `platform_type = woocommerce`.
- Stores and WooCommerce Settings reuse `kits_woo_instance_action`.
- Groups are unchanged: connector user for the app and record menus, connector administrator for Sync Operations and Configuration.
- Installing this module does not restore the generic Connectors menu. Its upgrade calls `kits_remove_connector_app_menus` after the WooCommerce items have left that parent.

**Files**

- `__manifest__.py`
- `views/kits_woocommerce_menus.xml`
- `migrations/20.0.1.12.0/end-migrate.py`
- `ai.md`

### 2026-11-02 — Separate customer, product, and variant bindings

**Version:** `20.0.1.13.0`. Base module code was not changed.

**Models**

- `kits.woo.customer.binding`: `partner_id`, `instance_id`, `external_id`, `company_id`, `active`. Unique per store and external id, and per store and partner.
- `kits.woo.product.binding`: template, store, external product id, store title, short and long descriptions, product type, published, taxable, virtual, categories, tags, image URLs, variant counts, store dates, plus the shared sync fields.
- `kits.woo.variant.binding`: variant, parent product binding, store, external variant id, SKU, store title, URL, exists in store, manage stock, quantity rule (`odoo`, `fixed`, `percentage`), variant images, store dates, and the shared sync fields.
- `kits.woo.binding.sync` holds `sync_state`, `sync_error`, `sync_version`, `sync_date`, `active`, `exists_in_store`, and `external_data` for product and variant bindings only.

**Matching**

- Customers: store + external id, then case-insensitive email on top-level contacts, then create. No name skips the customer. Existing contact details and addresses are not overwritten. Billing and shipping are exact-matched child contacts.
- Products: store + external variation id, then store + SKU binding, then one Odoo variant by SKU. Create only when `woo_create_products` is on. Missing, duplicate, and option-less variable products fail before writes.

**Deletion**

- Woo customer deleted: unlink that store’s customer binding and keep the contact.
- Woo product deleted: archive the product binding and its variant bindings.
- Woo variant deleted: archive that variant binding and do not recreate it.
- Odoo contact, template, or variant deleted: delete the related dedicated bindings and do not call WooCommerce.

**Migration**

- `migrations/20.0.1.13.0/post-migrate.py` copies legacy generic bindings for `res.partner`, `product.template`, and `product.product` into the new models, then removes those generic rows. Orders, coupons, categories, tags, attributes, and refunds stay on `kits.connector.binding`.

**Tests (executed)**

- Database `odoo20-connectors-woo-tests`, `--test-tags=/kits_odoo_woocommerce_connector`, no live WooCommerce calls:
  **0 failed, 0 error(s) of 83 tests** (2026-11-02).
- The module stats line in the same run counted 109 tests. The result line is 83 tests, 0 failed, 0 errors.
- Live database `odoo20-connectors` was not upgraded.

**Files**

- New: `models/kits_woo_binding_sync.py`, `models/kits_woo_customer_binding.py`, `models/kits_woo_product_binding.py`, `views/kits_woo_binding_views.xml`, `migrations/20.0.1.13.0/post-migrate.py`, `tests/test_woo_separate_bindings.py`
- Modified: `__manifest__.py`, `models/__init__.py`, `security/ir.access.csv`, `views/kits_woocommerce_menus.xml`, `views/kits_connector_instance_views.xml`, `views/res_partner_views.xml`, instance customer/product/inventory/webhook/image/order/coupon models, `res_partner.py`, `product_template.py`, `product_product.py`, and the customer, product, deletion, record-sync, and catalog webhook tests.

### 2026-11-02 — Remove the generic Bindings menu

**Version:** `20.0.1.13.1`.

The WooCommerce **Bindings** menu and `kits_woo_binding_action` are removed. That screen listed `kits.connector.binding`, which no longer holds customers, products, or variants. Customer Bindings, Product Bindings, and Variant Bindings stay. Orders, categories, tags, attributes, coupons, and refunds still use the shared binding model; they are reached from their own records and menus.

### 2026-11-02 — Group Sale and Product menus

**Version:** `20.0.1.13.2`.

WooCommerce top menu order: Stores, Sale, Product, Sync Operations, Sync Jobs, Logs, then Coupons, Refunds, and Configuration.

Sale contains Orders, Customers, and Customer Bindings. Product contains Products, Product Bindings, and Variant Bindings. Orders are sale orders with a WooCommerce order id. Customers are top-level contacts that have a customer binding. Products are templates linked to a WooCommerce store.

### 2026-11-02 — Sync Operation store dropdown

**Version:** `20.0.1.13.3`.

The Sync Operation and Fetch Customer forms set their company to the current company before the store dropdown is opened. A store that belongs to that company is listed. The company is no longer derived only from a store that has not been selected yet.

### 2026-11-02 — Refunds under Sale

**Version:** `20.0.1.13.4`.

The Refunds menu is a child of Sale, after Customer Bindings. It is no longer a top-level WooCommerce item.

### 2026-11-02 — Handover document reconciled with 20.0.1.13.4

Docs only. No code change and no version bump.

The living sections now match the code: current menu tree, no generic Bindings menu, customer search operator `in` / `[True]`, product match through an existing product binding, Sync Operation and Fetch Customer company default, and deliveries as stock pickings with no WooCommerce menu. The validation table records the live upgrade through `20.0.1.13.4` and keeps the automated result at `20.0.1.13.0`.

### 2026-11-03 — Remaining sync operations

**Version:** `20.0.1.14.0`. Base module code was not changed.

**Wizard** (`kits.woo.sync.wizard`)

The store is always required. Only the fields for the selected operation are shown.

| Operation | Extra fields | Execution |
|-----------|--------------|-----------|
| Import Customers | none | queued `woo_import_customers` |
| Import Products | Import All or New and Updated Only, Do Not Update Existing | queued `woo_import_products` |
| Import Specific Products | WooCommerce product ids | queued `woo_import_products` |
| Import Unshipped Orders | Date From, Date To | queued `woo_import_orders`, statuses pending/processing/on-hold |
| Import Shipped Orders | Date From, Date To | queued `woo_import_orders`, status completed |
| Import Cancelled Orders | none | queued `woo_import_orders`, status cancelled |
| Import/Update Specific Orders | WooCommerce order ids | queued `woo_import_orders` |
| Import Categories / Tags / Attributes / Coupons | none | existing import jobs |
| Map Products using CSV/Excel | file | immediate, no job |
| Export Shipment Information / Update Order Status | none | queued `woo_export_shipments` |
| Export Stock | Export Stock From | queued `woo_export_inventory` |
| Export Categories / Update Categories | none | `woo_export_categories` with `category_mode` |
| Export Tags / Update Tags | none | `woo_export_tags` with `tag_mode` |
| Export Coupons / Update Coupons | none | `woo_export_coupons` with `coupon_mode` |

Import stock is not offered. Product export stays on the product form and the product list.

A queued run opens WooCommerce → Sync Jobs for that job. The same store, job type, and payload already pending or running opens that job and does not create another one. Failed jobs still use the base retry and backoff.

**Direct actions**

- Product form and list: Publish on WooCommerce, Unpublish on WooCommerce. The Odoo product stays active. Each selected store is logged.
- Sales order: Update Woo Status, and Cancel in WooCommerce. Cancellation does not cancel the Odoo order. Tracking export is unchanged and does not change the Woo status.
- Customer credit note: Refund in WooCommerce. One Woo refund per credit note. The amount cannot exceed the order total minus refunds already marked done. No second credit note is created.

**Mapping file**

Header needs `woo_id` or `woo_product_id`, and `odoo_id` or `sku`. CSV and xlsx are accepted. Rows are validated before any binding is written. Missing products, bad ids, duplicate rows, ambiguous SKUs, and conflicting bindings are reported. Products are not created or deleted. A single-variant product also gets a variant binding with the same Woo id. A multi-variant product is linked at the template only.

**Tests (executed)**

- Database `odoo20-connectors-woo-tests`, `--test-tags=/kits_odoo_woocommerce_connector`, no live WooCommerce calls:
  **0 failed, 0 error(s) of 90 tests** (2026-11-03).
- The module stats line in the same run counted 120 tests. The result line is 90 tests, 0 failed, 0 errors.
- Living sections 3, 5, 8, 10, and 11 describe this release. The historical changelog above is unchanged.

### 2026-11-03 — Wizard operations matrix testing (`20.0.1.14.1`)

**Version:** Woo `20.0.1.14.1`, base `20.0.1.9.1`.

**Bugs fixed during testing**

1. Product mapping through the Sync Operation wizard failed on Odoo 20 because Binary fields expose `BinaryValue` (raw bytes) while `_woo_parse_product_map` always base64-decoded. Fixed in `models/kits_connector_instance_mapping.py` (`_woo_mapping_file_bytes`).
2. HTTP error messages from `ConnectorHttpClient` included OAuth query strings (`oauth_consumer_key`, signature). Fixed in base `services/http_client.py` (`_safe_url_for_log`). Live DB still needs a restart/upgrade to pick up base `20.0.1.9.1`.

**Automated**

- Added `tests/test_woo_wizard_all_operations.py` covering all 20 wizard operations (payloads, validation, mocked import/export edge cases, mapping Binary upload, OAuth URL redaction).
- Suite on `odoo20-connectors-woo-tests`: **0 failed, 0 error(s) of 101 tests**.

**Live API (`odoo20-connectors` + `http://localhost/wordpress`, store id 1)**

Connection test passed. Wizard exposed 20 operations. Queued and ran jobs:

| Op | Job result |
|----|------------|
| Import Customers | done — reused=22 |
| Import Specific Products (111) | done — updated=1 |
| Import Products (new/updated + skip existing) | done — skipped=29, failed=19 (existing SKU/data quality issues on store catalog) |
| Import Categories / Tags / Attributes / Coupons | done |
| Import Unshipped Orders | done — created=2, failed=1 (unmapped SKU) |
| Import Shipped / Cancelled | done |
| Import Specific Orders (999999) | done — failed=1 HTTP 404 (expected) |
| Export Stock | done — updated=31 |
| Export/Update Categories, Tags, Coupons | done |
| Export Shipment | done — skipped=2 |
| Map Products (wizard Binary path) | **Blocked** on live until server reloads `20.0.1.14.1` |

**Files**

- `models/kits_connector_instance_mapping.py`
- `tests/test_woo_wizard_all_operations.py`
- `tests/__init__.py`
- `__manifest__.py`
- `ai.md`
- base: `services/http_client.py`, `__manifest__.py`, `ai.md`

### 2026-11-04 — Slim store form header buttons (`20.0.1.14.2`)

**Version:** `20.0.1.14.2`.

Removed store-form header buttons that duplicate Sync Operation: Import Products, Import Orders, Import Customers, Export Inventory, Import Categories, Import Attributes, Import Coupons, Export Coupons.

Kept: Sync Operation, Export Products (not in the wizard), and the four webhook register/unregister buttons. Payment/shipping/tax fetch buttons on the map tabs are unchanged.

**Files:** `views/kits_connector_instance_views.xml`, `__manifest__.py`, `ai.md`.
