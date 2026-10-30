# Kits Odoo Shopify Connector

Connect Shopify stores to Odoo 20 Community through the Kits base connector. This version stores the shop connection and tests the Admin GraphQL API. Product, customer, and order sync are not included yet.

## Install

1. Confirm `custome/20.0` is on the Odoo addons path.
2. Update the Apps list.
3. Install **Kits Odoo Shopify Connector**. That also installs **Kits Odoo Base Connector** if it is not already installed.

## Shopify app

Use a custom app or a merchant-created app on the store. Public-app OAuth is not implemented. Those apps can use a non-expiring Admin API access token; the public-app expiring-token rule does not apply to them.

Create the app in the Shopify Dev Dashboard (or Shopify admin → Settings → Apps and sales channels → Develop apps), then:

1. Set the Admin API scopes the connector will need:
   - `read_products`, `write_products`
   - `read_files`, `write_files`
   - `read_publications`, `write_publications`
   - `read_customers`, `write_customers`
   - `read_orders`
2. Install the app on the store.
3. Reveal the Admin API access token once and copy it. Do not send that token in chat, tickets, or log files.

Phase 1 only calls `shop { name }`. The scopes above are the ones later sync will require, so include them before the token is issued.

## Configure the store in Odoo

1. Open **Connectors → Shopify Stores**.
2. Create a store.
3. **Shop Domain:** `your-store.myshopify.com`, or only the store name. A custom storefront domain is rejected.
4. **API Version:** `2026-10` (current stable Admin API, supported until at least 1 October 2027). `2026-01`, `2026-04`, and `2026-07` are also available.
5. **Admin API Access Token:** paste the token and save.
6. Click **Test Connection**.

The token field is a password field. After a reload it looks empty. Saving again does not erase the saved token unless you type a new one.

A successful test sets the store to **Connected**. Authentication errors, GraphQL errors, and a missing domain or token set the store to **Error** and write a connector log. The access token is not written into that log.

Jobs, operation logs, and bindings stay under the Connectors app. Company rules and connector user/administrator groups come from the base module. Only connector administrators can read the token.
