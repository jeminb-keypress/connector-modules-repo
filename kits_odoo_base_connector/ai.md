# kits_odoo_base_connector — AI Handover Document

> **Mandatory for future AI agents:** Read this file before modifying the module. After every code change, append a dated changelog entry below. Never erase previous history.

---

## 1. Module purpose

Reusable Odoo 20 integration foundation for marketplace/ERP connectors. Inspired by Emipro’s Common Connector Library architecture (connection instances, sync jobs, logging, bindings) but implemented independently — no proprietary code was copied.

This module provides **shared infrastructure only**. It does **not** implement WooCommerce/Shopify/Magento/Amazon/QuickBooks business sync or platform APIs.

**Current version:** `20.0.1.9.1`. No app menu. Platform modules own their menus and their operation wizards. WooCommerce `20.0.1.14.1` keeps its 20-operation wizard, shipment export, tag export, product mapping, publish/unpublish, remote cancel, and credit-note refund in that module.

### Planned dependent modules

| Module | Role |
|--------|------|
| `kits_odoo_woocommerce_connector` | WooCommerce sync |
| `kits_odoo_shopify_connector` | Shopify sync |
| `kits_odoo_magento_connector` | Magento sync |
| `kits_odoo_amazon_connector` | Amazon sync |
| `kits_odoo_quickbooks_connector` | QuickBooks sync |

---

## 2. Architecture overview

```
Platform module
    │ inherits / extends
    ▼
kits.connector.instance  ──uses──►  kits.connector.api.mixin  ──uses──►  ConnectorHttpClient
    │                                      (auth / validate hooks)
    ├── kits.connector.sync.job   (queue + cron + retries)
    ├── kits.connector.log        (operation audit trail)
    └── kits.connector.binding    (external ↔ Odoo ID map for records that have no platform binding)
```

### Design principles

1. **Platform-agnostic base** — only a `generic` platform type ships here.
2. **Extension by inheritance** — platforms use `selection_add`, override hooks, and add `_run_<job_type>` handlers.
3. **No fake features** — generic connection test only validates configuration; it does not call remote APIs.
4. **Company isolation** — all main models have `company_id` + multi-company access rules.
5. **Secure credentials** — `api_key` / `api_secret` restricted to connector managers; views use `widget="password"`.

---

## 3. File responsibilities

| Path | Responsibility |
|------|----------------|
| `__manifest__.py` | Module metadata, depends (`base`, `mail`), data load order |
| `__init__.py` | Loads `models` and `services` |
| `models/kits_connector_api_mixin.py` | Abstract HTTP helpers + auth/validation hooks |
| `models/kits_connector_instance.py` | Connection instance, test connection, log/job helpers |
| `models/kits_connector_sync_job.py` | Sync job workflow, retries, cron processor |
| `models/kits_connector_log.py` | Operation logs |
| `models/kits_connector_binding.py` | External ID mappings + bind helpers |
| `services/http_client.py` | Reusable `requests`-based HTTP client |
| `services/exceptions.py` | `ConnectorError` hierarchy |
| `security/kits_connector_security.xml` | Privilege + User/Administrator groups (Odoo 20 style) |
| `security/ir.access.csv` | ACLs + multi-company domains (Odoo 20 unified access) |
| `data/ir_cron_data.xml` | Cron: process pending jobs every 5 minutes |
| `views/*` | Shared list/form/search views and window actions. No app menu. |
| `static/description/icon.png` | App icon |
| `ai.md` | This handover document |

---

## 4. Implemented features and status

| Feature | Status | Notes |
|---------|--------|-------|
| Connection management | **Done** | Instance model: platform, company, URL, timeout, state, active, credentials, config JSON |
| API framework | **Done** | `ConnectorHttpClient` + mixin hooks (`_kits_get_auth_headers`, `_kits_validate_response`, …) |
| Sync engine | **Done** | Jobs with draft/pending/running/done/failed/cancelled; enqueue; run now; retries; cron |
| Logging | **Done** | Levels, status, message, details, optional request/response snippets |
| External ID mapping | **Done** | Unique constraints + `_kits_bind_record` / `_kits_find_binding` |
| Sync status on bindings | **Done** | `sync_state` (`synced` / `not_synced` / `error` / `deleted` / `deleted_local`), `sync_error`, `sync_version` + mark helpers `_kits_mark_deleted_remote`, `_kits_mark_deleted_local`, `_kits_mark_odoo_records_deleted_local`. Dirty marking skips both deletion states so a later edit cannot queue automatic recreation. |
| Partner connected instances | **Done** | Computed `kits_connected_instance_ids` / `kits_binding_ids` / count on `res.partner` from `kits.connector.binding`. WooCommerce customers use `kits.woo.customer.binding` and extend this compute. |
| Security | **Done** | Groups, ACL CRUD split user/manager, company rules, credential groups |
| Shared views | **Done** | List, form, and search views, plus window actions. No app menu since `20.0.1.9.0`. |
| Platform business sync | **Out of scope** | Belong in dependent modules |
| Webhooks | **Not implemented** | Intentionally deferred to platform modules |
| OAuth flows | **Not implemented** | Platforms override auth hooks as needed |

---

## 5. Extension guidance (for platform modules)

### Add a platform type

```python
class KitsConnectorInstance(models.Model):
    _inherit = 'kits.connector.instance'

    # Base field must use a static list (not a selection method) for selection_add.
    platform_type = fields.Selection(
        selection_add=[('woocommerce', 'WooCommerce')],
        ondelete={'woocommerce': 'cascade'},
    )
```

### Override authentication / connection test

```python
def _kits_get_auth_headers(self, method, url, headers, kwargs):
    headers = dict(headers or {})
    # inject platform auth — never log secrets
    return headers

def _kits_test_connection(self):
    self.ensure_one()
    self._kits_http_request_json('GET', '/some/health-endpoint')
    return True
```

### Implement a sync handler

On `kits.connector.sync.job` (or inherit):

```python
def _run_import_orders(self):
    self.ensure_one()
    # platform import logic…
    return 'Imported N orders'
```

Create jobs via:

```python
instance.action_create_sync_job('import_orders', payload={...})
```

### Bind external records

Use `kits.connector.binding` when the platform has no model of its own:

```python
env['kits.connector.binding']._kits_bind_record(
    instance, 'sale.order', order.id, external_id='123'
)
```

WooCommerce does not use this model for customers, product templates, or variants. Those use `kits.woo.customer.binding`, `kits.woo.product.binding`, and `kits.woo.variant.binding`.

---

## 6. Important technical decisions

1. **Odoo 20 access model** — use `security/ir.access.csv` (`id,name,model_id,group_id/id,operation,domain`) instead of legacy `ir.model.access.csv` + separate `ir.rule` XML. Multi-company rules are CSV rows with empty `group_id` and a domain.
2. **Odoo 20 groups** — use `res.groups.privilege` with `category_id` (see stock/survey).
3. **Constraints** — use `models.Constraint(...)` (not `_sql_constraints`).
4. **Display names** — use `_compute_display_name` (not deprecated `name_get`).
5. **Views** — use `<list>` (not `<tree>`); credential fields use `widget="password"`.
6. **Job dispatch** — convention `_run_<job_type>`; no central registry dict (keeps overrides simple).
7. **Background processing** — native `ir.cron` + job table (no `queue_job` dependency).
8. **HTTP library** — `requests` (already used by Odoo); wrapped, not called ad hoc from platforms ideally.
9. **Transaction commits** — `_process_job` commits after marking `running` unless `tools.config['test_enable']`, so attempt counts survive hard crashes.
10. **addons_path** — `conf/odoo20.conf` updated to include `/home/keypress/workspace1/custome/20.0` before core addons.

### Dependencies

- Odoo modules: `base`, `mail`
- Python: `requests` (Odoo dependency)

---

## 7. Odoo 20 compatibility notes

- Current version is `20.0.1.9.0`. The first release was `20.0.1.0.0`.
- Validated against local Odoo `20.0` tree under `odoo/odoo20`
- Config warnings observed (environment pre-existing, not introduced by this module): unknown options `longpolling_port`, `xmlrpc_interface`, `netrpc_interface` in `odoo20.conf`
- Privilege UX lives under **Settings → Users → Productivity → Kits Connectors**

---

## 8. Bugs fixed / known limitations / pending tasks

### Known limitations

- The first install attempt on 2026-10-27 did not run because PostgreSQL was down. Later upgrades of this module have run on `odoo20-connectors`.
- Generic `_kits_test_connection` does not perform a network call (by design).
- No webhook receiver / controller in base.
- No automated Odoo `TransactionCase` tests yet.
- Generic credential fields are convenience slots; platforms should prefer their own named fields with proper `groups=`.

### Pending tasks (suggested)

- [x] Install/upgrade on a live PostgreSQL-backed DB (done on `odoo20-connectors`; this module no longer has menus to smoke-test).
- [ ] Add `tests/` for binding uniqueness, job retry transitions, HTTP error mapping.
- [ ] Optionally add log GC cron for old logs.
- [x] Build first platform module (`kits_odoo_woocommerce_connector`) on top of this base. (done 2026-10-27 as sibling under `custome/20.0/`)

---

## 9. Validation results (actual)

| Check | Result |
|-------|--------|
| Python `py_compile` all module `.py` files | **Passed** |
| XML well-formedness (`ElementTree`) | **Passed** |
| Manifest parse + data file existence | **Passed** |
| Odoo module discovery (`get_module_path` / addons path) | **Passed** — path resolved under `custome/20.0` |
| HTTP client unit checks (URL build, truncate, status mapping) | **Passed** |
| `odoo-bin -i kits_odoo_base_connector --stop-after-init` on 2026-10-27 | **Not completed** — PostgreSQL was down |
| Later upgrades | **Passed** on `odoo20-connectors` through `20.0.1.9.0` |

The 2026-10-27 install did not run. Later upgrades of this module did.

---

## 10. Change log

### 2026-10-27 — Initial implementation

**Created**

- Full module scaffold under `custome/20.0/kits_odoo_base_connector/`
- Models: instance, API mixin, sync job, log, binding
- Services: `http_client.py`, `exceptions.py`
- Security: privilege/groups + `ir.access.csv`
- Views/menus/cron/icon
- `ai.md` (this file)

**Modified outside module**

- `conf/odoo20.conf` — `addons_path` set to  
  `/home/keypress/workspace1/custome/20.0,/home/keypress/workspace1/odoo/odoo20/addons`  
  so the custom module is loadable.

**Deleted**

- None

**Reason**

- Deliver reusable Odoo 20 base connector framework for future platform modules, with documentation suitable for AI handovers.

### 2026-10-27 — Binding display name depends

**Modified**

- `models/kits_connector_binding.py` — added `@api.depends('odoo_model', 'odoo_record_id', 'external_id')` on `_compute_display_name`.

**Reason**

- Ensure display names recompute when mapping fields change (Odoo 20 compute pattern).

### 2026-10-27 — WooCommerce platform module created (external)

**Created (outside this module)**

- Sibling addon `custome/20.0/kits_odoo_woocommerce_connector` depending on this base.

**Modified in this module**

- `ai.md` only — marked WooCommerce pending task complete; no code changes.

**Reason**

- Track that the first platform connector now exists and reuses documented extension points.

### 2026-10-27 — Fix platform_type for selection_add

**Modified**

- `models/kits_connector_instance.py` — `platform_type` now uses a static selection list `[('generic', 'Generic')]` instead of `selection='_selection_platform_type'`. Removed unused `_selection_platform_type`.
- `ai.md` — documented that `selection_add` requires a list-based Selection on the base field.

**Reason / bug fixed**

- Installing `kits_odoo_woocommerce_connector` raised  
  `AssertionError: ... selection_add=... on non-list selection '_selection_platform_type'`.  
  Odoo only allows `selection_add` when the parent Selection is a list, not a method.

### 2026-10-27 — Named groups for view inheritance

**Modified**

- `views/kits_connector_instance_views.xml` — added `name="group_connection"` and `name="group_credentials"` so platform modules can xpath without using `@string` (forbidden in Odoo view inheritance).

**Reason**

- WooCommerce install failed with: `View inheritance may not use attribute 'string' as a selector.`

### 2026-10-27 — Version bump for compliance upgrades

**Modified**

- `__manifest__.py` — version `20.0.1.1.0` (signals upgrade after view name anchors + selection list fix)
- `ai.md` — changelog only

**Reason**

- Users who already installed `20.0.1.0.0` need a clear upgrade path before reinstalling the WooCommerce connector.

### 2026-10-29 — Woo cron stub for multi-DB safety

**Bug**

- Cron `WooCommerce: Schedule Auto Sync Jobs` failed on DBs without the Woo module loaded
  (`AttributeError: ... no attribute '_cron_woo_schedule_jobs'`), e.g. `odoo20-connectors-woo-tests`
  when the worker ran cron across all databases.

**Modified**

- `models/kits_connector_instance.py` — no-op `@api.model def _cron_woo_schedule_jobs`
  (overridden by `kits_odoo_woocommerce_connector` when installed)
- `__manifest__.py` — version `20.0.1.1.1`

**Fix**

- Upgrade base module, then restart Odoo:
  `-u kits_odoo_base_connector`

### 2026-10-29 — Binding sync status + stale-job version

**What changed**

- `models/kits_connector_binding.py` — fields `sync_state`, `sync_error`, `sync_version`; helpers
  `_kits_mark_not_synced`, `_kits_mark_synced`, `_kits_mark_error`,
  `_kits_mark_records_not_synced`, `_kits_is_stale_job`. Successful `_kits_bind_record` sets
  `sync_state=synced` and bumps `sync_version`.
- `views/kits_connector_binding_views.xml` — status badge, filters, form fields.
- `__manifest__.py` — version `20.0.1.2.0`.

**Why**

- Platform modules need per-instance Synced / Not Synced / Error without Woo-specific columns on
  products. Stale job detection needs a monotonic version when a newer edit happens while an older
  job is still queued.

**How it works**

- Bindings remain the only Odoo ↔ external map. Platform modules mark Not Synced on mapped-field
  writes and Synced after a successful push/pull. Jobs may pass the version captured at enqueue
  time and skip apply when `_kits_is_stale_job` is true.

**Impact**

- Compatible with WooCommerce and Shopify. No Woo business logic added to base.
- Existing bindings default to `not_synced` until the next successful bind/sync.

**Testing**

- Exercised indirectly by Woo `kits_woo` suite on `odoo20-connectors-woo-tests`
  (`-u kits_odoo_base_connector,kits_odoo_woocommerce_connector --test-tags kits_woo`):
  **0 failed, 0 error(s) of 22 tests** (2026-10-29 morning), then **23/23** after Woo export
  sync-state regression (still uses base `_kits_bind_record` / `sync_state`).

### 2026-10-29 — Live verification note (no base code change)

**Observed with Woo live pass on `odoo20-connectors`**

- Binding `sync_state` / `sync_version` correctly reflected Not Synced after mapped writes and
  Synced after successful `_kits_bind_record` (Woo export update path fixed to call bind).
- Stale-job skip via `sync_versions` exercised on inventory and product export jobs.
- No base module code changes in this live-test pass; Woo `20.0.1.4.1` consumes existing helpers.

### 2026-10-29 — Partner connected-instances fields

**What changed**

- `models/res_partner.py` — computed `kits_connected_instance_ids`, `kits_binding_ids`,
  `kits_binding_count`, plus `action_kits_open_bindings`.
- `__manifest__.py` — version `20.0.1.3.0`.
- Partner form UI for these fields lives in platform modules (WooCommerce) so `website`'s
  required `ir.ui.view.visibility` does not break base installs without website.

**Why**

- Customers need a cross-platform view of which connector instances they are linked to, with
  per-instance external IDs and sync status stored on bindings (not duplicated on the partner).

**How it works**

- Bindings with `odoo_model=res.partner` drive the computed M2M/instance list. Platforms never
  merge contacts across instances by email in base; matching rules stay in platform modules.

**Testing**

- Covered by Woo customer suite on `odoo20-connectors-woo-tests` (**30/30** kits_woo tests,
  including `test_partner_connected_instances_compute`).
- Live: partner 77 after fetch showed connected instance **9** and Synced status.

### 2026-10-29 — Deleted-remote binding state + Material smart-button icons

**What changed**

- `models/kits_connector_binding.py` — `sync_state` value `deleted` (“Deleted Remote”);
  `_kits_mark_deleted_remote()` soft-marks mappings when platforms report remote delete
  (does **not** archive/delete the Odoo record). Successful `_kits_bind_record` still sets
  `synced` so a recreated remote ID can revive the mapping.
- `views/kits_connector_binding_views.xml` — muted badge + “Deleted Remote” search filter
  (already present with the selection).
- `views/kits_connector_instance_views.xml` — smart-button icons use Odoo 20 Material names
  `checklist` / `article` / `link` (not Font Awesome `fa-*`).
- `__manifest__.py` — version `20.0.1.5.0`.

**Why**

- Platform webhooks (WooCommerce customer.deleted, etc.) need a first-class unavailable state
  without wiping contacts. Icons must render under Odoo 20’s Material Symbols / `oi` system.

**Testing**

- Exercised by Woo webhook suite + live soft-delete of partner **81** binding (state `deleted`,
  contact remained). Browser confirmed instance smart-button `data-icon` / `::before` ligatures
  for checklist/article/link on instance **9**.

### 2026-10-29 — Emipro expansion support (version bump)

**What changed**

- `__manifest__.py` — version `20.0.1.6.0` (consumed by WooCommerce connector expansion).
- No new base business models. Soft-delete binding state from prior entry remains the
  platform hook for product/order/coupon remote deletes.

**Notes for future agents**

- Keep platform business logic (catalog, coupons, gateways, refunds, webhooks) in
  `kits_odoo_*_connector` modules. Base stays framework-only.
- Woo automated suite after expansion: **53/53** on `odoo20-connectors-woo-tests`.

### 2026-10-29 — Woo production-fix support (version bump)

**What changed**

- `__manifest__.py` — version `20.0.1.7.0` (companion to Woo `20.0.1.8.0`).
- No new base models. Soft-delete bindings remain the remote-delete hook for Woo
  product/order/coupon/customer webhooks.

**Notes for future agents**

- Woo suite after tax/coupon/refund/allowlist fixes: **64/64** on
  `odoo20-connectors-woo-tests`.
- Odoo 20 system parameters use `get_str` / `set_str` (not `get_param` / `set_param`).
- Woo webhook allowlist key: `kits_woo.webhook_db_allowlist` (platform module).

### 2026-10-30 — Local deletion state on bindings

**What changed**

- `models/kits_connector_binding.py` — `sync_state` value `deleted_local` (“Deleted Local”);
  `_kits_mark_deleted_local()` and `_kits_mark_odoo_records_deleted_local()`.
  `_kits_mark_records_not_synced()` no longer touches `deleted` or `deleted_local` bindings.
- `views/kits_connector_binding_views.xml` — muted badge and “Deleted Local” filter.
- `__manifest__.py` — version `20.0.1.8.0`.

**Why**

- Deleting an Odoo record must leave a historical, instance-scoped mapping and must not be treated
  as “needs export”. Remote deletion stays `deleted`. Neither state deletes the other platform’s record.

**How it works**

- Platform `unlink` calls `_kits_mark_odoo_records_deleted_local` before `super().unlink()`.
  The search is company-scoped to the current user’s companies. It does not call a remote API.
- Successful `_kits_bind_record` still sets `synced`, so an explicit re-link can revive a mapping.

**Testing**

- Woo suite on `odoo20-connectors-woo-tests` after upgrade:
  **0 failed, 0 error(s) of 72 tests** (2026-10-30). Includes local product delete → `deleted_local`
  and a later automatic import that does not recreate the product.

### 2026-11-02 — No connector menus in the base module

**Version:** `20.0.1.9.0`.

The base module is a backend framework. It does not register an app menu.

**Menu ownership**

- Shared models, views, and unfiltered window actions stay here: instances, sync jobs, logs, bindings.
- `application` is `False`.
- `views/kits_connector_menus.xml` is removed. Upgrade `20.0.1.9.0` deletes those menu records once they have no children (`hooks.kits_remove_connector_app_menus`).
- Platform modules own their root menus and open the shared views through their own actions, filtered by `platform_type`.

**Files**

- `__manifest__.py`
- `hooks.py`
- `migrations/20.0.1.9.0/end-migrate.py`
- deleted `views/kits_connector_menus.xml`
- `ai.md`

### 2026-11-02 — WooCommerce dedicated bindings (no base code change)

**Version:** base stays `20.0.1.9.0`. WooCommerce connector `20.0.1.13.0`.

`kits.connector.binding` remains the shared map for orders, coupons, categories, tags, attributes, refunds, and other records that do not have their own model.

WooCommerce customers, product templates, and product variants no longer use this generic model. They use `kits.woo.customer.binding`, `kits.woo.product.binding`, and `kits.woo.variant.binding` in `kits_odoo_woocommerce_connector`. The WooCommerce upgrade copies the old generic rows for those three models and then removes them. One Odoo contact or product may still have one mapping per store.

No base Python, view, security, or migration file was changed for this split.

### 2026-11-03 — WooCommerce operations stay in the Woo module

**Version:** base stays `20.0.1.9.0`. WooCommerce connector `20.0.1.14.0`.

The 20-operation wizard, shipment status, tag export, product mapping, publish/unpublish, remote cancel, and credit-note refund are implemented in `kits_odoo_woocommerce_connector`. They use the existing job, log, and binding helpers. No base Python, view, security, or migration file was changed.

### 2026-11-02 — Handover document reconciled with 20.0.1.9.0

Docs only. No code change and no version bump.

The living sections now say this module has no app menu, that WooCommerce customers, templates, and variants use their own binding models, and that the 2026-10-27 PostgreSQL outage is historical. Later upgrades on `odoo20-connectors` are recorded in the validation table.

### 2026-11-03 — Redact OAuth query strings from HTTP errors (`20.0.1.9.1`)

**Version:** `20.0.1.9.1`.

`ConnectorHttpClient._raise_for_status` logged `response.url`, which for WooCommerce HTTP OAuth includes `oauth_consumer_key` and `oauth_signature`. `_safe_url_for_log` now strips the query string from error messages. Response body truncation is unchanged.

**Files:** `services/http_client.py`, `__manifest__.py`, `ai.md`.
