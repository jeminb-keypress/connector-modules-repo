# kits_odoo_base_connector — AI Handover Document

> **Mandatory for future AI agents:** Read this file before modifying the module. After every code change, append a dated changelog entry below. Never erase previous history.

---

## 1. Module purpose

Reusable Odoo 20 integration foundation for marketplace/ERP connectors. Inspired by Emipro’s Common Connector Library architecture (connection instances, sync jobs, logging, bindings) but implemented independently — no proprietary code was copied.

This module provides **shared infrastructure only**. It does **not** implement WooCommerce/Shopify/Magento/Amazon/QuickBooks business sync or platform APIs.

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
    └── kits.connector.binding    (external ↔ Odoo ID map)
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
| `views/*` | List/form/search views and Connectors app menu |
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
| Security | **Done** | Groups, ACL CRUD split user/manager, company rules, credential groups |
| Simple UI | **Done** | Menus, list/form/search, stat buttons; no dashboards |
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

```python
env['kits.connector.binding']._kits_bind_record(
    instance, 'product.product', product.id, external_id='123'
)
```

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

- Versioned as `20.0.1.0.0`
- Validated against local Odoo `20.0` tree under `odoo/odoo20`
- Config warnings observed (environment pre-existing, not introduced by this module): unknown options `longpolling_port`, `xmlrpc_interface`, `netrpc_interface` in `odoo20.conf`
- Privilege UX lives under **Settings → Users → Productivity → Kits Connectors**

---

## 8. Bugs fixed / known limitations / pending tasks

### Known limitations

- Full module **installation was not executed**: PostgreSQL cluster `18/main` is **down** and cannot be started in this environment (`sudo` blocked; connection refused on `localhost:5432`).
- Generic `_kits_test_connection` does not perform a network call (by design).
- No webhook receiver / controller in base.
- No automated Odoo `TransactionCase` tests yet.
- Generic credential fields are convenience slots; platforms should prefer their own named fields with proper `groups=`.

### Pending tasks (suggested)

- [ ] Install/upgrade on a live PostgreSQL-backed DB and smoke-test menus/ACL.
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
| `odoo-bin -i kits_odoo_base_connector --stop-after-init` | **Not completed** — `psycopg2.OperationalError: Connection refused` (PostgreSQL down) |

Do **not** claim DB install tests passed until they are re-run successfully.

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
