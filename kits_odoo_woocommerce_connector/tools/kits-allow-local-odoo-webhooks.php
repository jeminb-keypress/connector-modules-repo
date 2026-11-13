<?php
/**
 * Plugin Name: Kits — Allow local Odoo webhook delivery
 * Description: Lets WooCommerce deliver webhooks to local Odoo (127.0.0.1 / localhost).
 *              WordPress rejects loopback/private URLs by default (wp_http_validate_url).
 *
 * Install (once):
 *   sudo mkdir -p /var/www/html/wordpress/wp-content/mu-plugins
 *   sudo cp kits-allow-local-odoo-webhooks.php /var/www/html/wordpress/wp-content/mu-plugins/
 *   sudo chown www-data:www-data /var/www/html/wordpress/wp-content/mu-plugins/kits-allow-local-odoo-webhooks.php
 */

defined('ABSPATH') || exit;

add_filter('http_request_args', function ($args, $url) {
    if (!is_string($url) || $url === '') {
        return $args;
    }
    $path = parse_url($url, PHP_URL_PATH) ?: '';
    if (strpos($path, '/kits_woo/webhook/') === false) {
        return $args;
    }
    $host = strtolower((string) parse_url($url, PHP_URL_HOST));
    if (in_array($host, ['127.0.0.1', 'localhost', '::1'], true)) {
        $args['reject_unsafe_urls'] = false;
    }
    return $args;
}, 10, 2);

add_filter('woocommerce_webhook_http_args', function ($http_args, $arg, $webhook_id) {
    $url = is_array($http_args) ? ($http_args['url'] ?? '') : '';
    if (!is_string($url) || strpos($url, '/kits_woo/webhook/') === false) {
        return $http_args;
    }
    $host = strtolower((string) parse_url($url, PHP_URL_HOST));
    if (in_array($host, ['127.0.0.1', 'localhost', '::1'], true)) {
        $http_args['reject_unsafe_urls'] = false;
    }
    return $http_args;
}, 10, 3);
