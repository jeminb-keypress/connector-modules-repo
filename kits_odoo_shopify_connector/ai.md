# kits_odoo_shopify_connector — AI Handover Document

> **Mandatory for future AI agents:** Read this file and `kits_odoo_base_connector/ai.md` before modifying this module. After every code change, append a dated changelog entry below. Never erase previous history. Never write access tokens or other secrets into this file, logs, or tests output.

---

## 1. Module purpose

Odoo 20 Shopify connector. Sibling of `kits_odoo_woocommerce_connector` under `custome/20.0/`. Does not depend on WooCommerce and does not change the base module.

Phase 1 is connection setup only: shop domain, Admin API access token, API version, GraphQL client, and Test Connection.

Depends on `kits_odoo_base_connector` for instances, the HTTP client, logs, company rules, and connector groups.

---

## 2. Architecture

```
kits_odoo_shopify_connector
    │
    ├─ extends kits.connector.instance
    │     platform_type shopify, domain, token, API version
    │     _kits_get_base_url / _kits_get_auth_headers / _kits_test_connection
    └─ services/shopify_api.py
          POST {shop}/admin/api/{version}/graphql.json
          GraphQL errors, 429, and THROTTLED retries
                 │
                 ▼
        kits_odoo_base_connector (HTTP, logs, jobs, bindings, security)
```

Business sync is not implemented. Do not add product, image, customer, order, inventory, or cron code until that phase is approved.

### Reuse

- `selection_add` on the base list selection `platform_type`.
- `ConnectorHttpClient` via `_kits_http_request_json`.
- `action_test_connection` (base) updates `state`, `last_error`, and `kits.connector.log`.
- Credential group `kits_odoo_base_connector.group_kits_connector_manager` and `widget="password"`.
- Multi-company rules on the base instance. No new model, so no extra ACL file.

Shopify-only behavior stays in this module: GraphQL `errors` on HTTP 200, cost throttling, `X-Shopify-Access-Token`, and myshopify.com domain checks.

---

## 3. File responsibilities

| Path | Responsibility |
|------|----------------|
| `__manifest__.py` | Depends only on `kits_odoo_base_connector`. Version `20.0.1.0.0`. Not an application. |
| `services/shopify_api.py` | GraphQL POST, cursor-ready client, throttle retry, redact token from errors |
| `models/kits_connector_instance.py` | Shopify fields, URL, auth header, connection test, empty-token write guard |
| `views/kits_connector_instance_views.xml` | Store form, list, search, Shopify Stores action |
| `views/kits_shopify_menus.xml` | Menus under the base Connectors app |
| `tests/` | Mocked TransactionCase tests, tag `kits_shopify` |
| `README.md` | Install and connection steps |
| `ai.md` | This document |

---

## 4. Configuration

Per Shopify instance:

- **Shop Domain** → `shopify_shop_domain`. Stored as `name.myshopify.com`. A bare store name is completed. Custom storefront hosts are rejected.
- **API Version** → `shopify_api_version`. Selection: `2026-01`, `2026-04`, `2026-07`, `2026-10` (default).
- **Admin API Access Token** → `shopify_access_token`. Manager group only. Not tracked, not copied.
- **Base URL** is derived: `https://{domain}/admin/api/{version}`. Hidden on the Shopify form.

Auth header: `X-Shopify-Access-Token`. The generic Bearer `api_key` is not sent for Shopify.

Empty password-widget submits (`''`, `False`, whitespace) do not clear a saved token.

### API version (verified 2026-10-29)

Official GraphQL Admin API **2026-10** (released 1 Oct 2026, supported until at least 1 Oct 2027):

- Endpoint: `https://{shop}.myshopify.com/admin/api/2026-10/graphql.json`
- Header: `X-Shopify-Access-Token`
- Connection query: `query { shop { name } }`

Custom and merchant-created apps are outside the public-app expiring-offline-token rule. This module stores that admin token. It does not implement public-app OAuth or refresh tokens.

### Scopes

Phase 1 only needs a token that can read `shop`. Create the token with the scopes later phases will use, because Shopify shows the token once:

`read_products`, `write_products`, `read_files`, `write_files`, `read_publications`, `write_publications`, `read_customers`, `write_customers`, `read_orders`.

The connection test does not check that each scope is granted.

---

## 5. Connection test

1. Require domain, API version, and token. Missing values raise before any HTTP call.
2. `ShopifyGraphQLClient.execute` POSTs `/graphql.json`.
3. HTTP 401/403 become an authentication message. Response text is redacted.
4. HTTP 200 with GraphQL `errors` fails unless the error is `THROTTLED`.
5. HTTP 429 and `THROTTLED` retry up to 3 attempts, sleeping at most 2 seconds using `extensions.cost.throttleStatus` when present.
6. Success requires `data.shop.name`. Base `action_test_connection` sets `connected` and writes a log.
7. Token text is stripped from messages and log details (`[redacted]`). Headers are not logged.

---

## 6. Known limitations

- No product, image, customer, order, inventory, or scheduled sync.
- No public-app OAuth and no webhook endpoint.
- No live Shopify call has been made.
- Token storage matches the base connector: plaintext, restricted by group. There is no extra encryption.
- Adding an API version outside the 2026 quarterly list requires a selection update after checking Shopify's version table.
- GraphQL `userErrors` on mutations are not handled yet because this phase has no mutations.
- The client retries throttle errors only. Other HTTP failures are not retried.

---

## 7. Validation results

| Check | Result |
|-------|--------|
| `python3 -m compileall` on the module | **Passed** (2026-10-29) |
| XML `ElementTree` parse of both view files | **Passed** |
| Module install on database `odoo20-shopify-phase1` | **Passed** (`-i kits_odoo_shopify_connector --stop-after-init`) |
| `--test-tags kits_shopify` | **Passed** — `0 failed, 0 error(s) of 9 tests` |
| Live Shopify Test Connection | **Not run** |

The first install command used `--test-enable` without a tag, so Odoo also ran unrelated core tests. Those reported existing environment failures (`phone_validation`, missing `rlPyCairo`). They are not part of this module. The tagged rerun is the Shopify result.

---

## 8. Change log

### 2026-10-29 — Phase 1 connection module

**Created** (all under `custome/20.0/kits_odoo_shopify_connector/`)

- Module scaffold and manifest
- `services/shopify_api.py`
- Instance extension: domain, token, API version, auth, connection test
- Views and Connectors menus
- Mocked connection tests
- `README.md`, `ai.md`

**Modified**

- None in `kits_odoo_base_connector` or `kits_odoo_woocommerce_connector`

**Deleted**

- None

**Reason**

- Phase 1 Shopify connector: configure a store and test the Admin GraphQL API on the existing base instance.
