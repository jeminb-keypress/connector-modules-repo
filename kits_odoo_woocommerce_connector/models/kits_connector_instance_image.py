# -*- coding: utf-8 -*-
"""Product image import/export helpers for WooCommerce.

Gallery images use ``product.image`` / ``product_template_image_ids`` from
``website_sale`` (required dependency). Videos are out of scope.
Images are never deleted solely because they are missing from a partial API response.

Export note: modern WooCommerce rejects ``data:`` URIs (``esc_url_raw`` strips them)
and blocks private/loopback sideload URLs. New images are therefore uploaded to the
WordPress media library via XML-RPC (``metaWeblog.newMediaObject``) using optional
WP credentials in ``config_json`` (``woo_wp_username`` / ``woo_wp_password``), then
attached by media id.
"""

import base64
import logging
import mimetypes
from xmlrpc import client as xmlrpc_client

from odoo import models

_logger = logging.getLogger(__name__)


class KitsConnectorInstanceImage(models.Model):
    _inherit = 'kits.connector.instance'

    def _woo_has_product_gallery(self):
        """True when the native product.image gallery model is available."""
        return 'product.image' in self.env and 'product_template_image_ids' in self.env['product.template']._fields

    def _woo_sync_template_images(self, template, woo_images, binding=None):
        """Import main + gallery images from WooCommerce product payload."""
        self.ensure_one()
        if not self.woo_import_images or not woo_images:
            return

        images = [img for img in woo_images if isinstance(img, dict) and img.get('src')]
        if not images:
            return

        meta = dict((binding.external_data or {}) if binding else {})
        image_meta = dict(meta.get('images') or {})
        main = images[0]
        main_id = str(main.get('id') or 'main')
        previous_hash = (image_meta.get(main_id) or {}).get('hash')
        image_b64, digest = self._woo_download_image(main.get('src'))
        if image_b64 and digest != previous_hash:
            template.write({'image_1920': image_b64})
            image_meta[main_id] = {
                'hash': digest,
                'src': main.get('src'),
                'role': 'main',
            }
        elif image_b64 and digest == previous_hash:
            image_meta[main_id] = {
                'hash': digest,
                'src': main.get('src'),
                'role': 'main',
            }

        if self._woo_has_product_gallery() and len(images) > 1:
            self._woo_sync_gallery_images(template, images[1:], image_meta)

        if binding:
            meta['images'] = image_meta
            binding.write({'external_data': meta})
        elif image_meta:
            # Persist on template binding if present
            tmpl_binding = self._woo_binding()._kits_find_binding(
                self, 'product.template', odoo_record_id=template.id,
            )
            if tmpl_binding:
                data = dict(tmpl_binding.external_data or {})
                data['images'] = image_meta
                tmpl_binding.write({'external_data': data})

    def _woo_sync_gallery_images(self, template, gallery_images, image_meta):
        """Upsert gallery rows on product.image without deleting missing remotes."""
        self.ensure_one()
        ProductImage = self.env['product.image']
        existing_by_woo_id = {}
        for img in template.product_template_image_ids:
            # Name convention: woo:<id> — avoids wiping unrelated local gallery rows
            if img.name and img.name.startswith('woo:'):
                existing_by_woo_id[img.name[4:]] = img

        sequence = 10
        for woo_img in gallery_images:
            woo_id = str(woo_img.get('id') or '')
            if not woo_id:
                continue
            previous_hash = (image_meta.get(woo_id) or {}).get('hash')
            image_b64, digest = self._woo_download_image(woo_img.get('src'))
            if not image_b64:
                continue
            name = f'woo:{woo_id}'
            vals = {
                'name': name,
                'sequence': sequence,
                'product_tmpl_id': template.id,
            }
            sequence += 10
            if digest != previous_hash:
                vals['image_1920'] = image_b64
            existing = existing_by_woo_id.get(woo_id)
            if existing:
                existing.write(vals)
            else:
                ProductImage.with_context(skip_update_main_image=True).create(vals)
            image_meta[woo_id] = {
                'hash': digest,
                'src': woo_img.get('src'),
                'role': 'gallery',
            }

    def _woo_sync_variant_image(self, variant, woo_image, binding=None):
        """Import a variation-specific image onto product.product.image_variant_1920."""
        self.ensure_one()
        if not self.woo_import_images or not woo_image:
            return
        src = woo_image.get('src') if isinstance(woo_image, dict) else False
        if not src:
            return
        woo_id = str((woo_image.get('id') if isinstance(woo_image, dict) else '') or 'variant')
        meta = dict((binding.external_data or {}) if binding else {})
        image_meta = dict(meta.get('images') or {})
        previous_hash = (image_meta.get(woo_id) or {}).get('hash')
        image_b64, digest = self._woo_download_image(src)
        if image_b64 and digest != previous_hash:
            variant.write({'image_variant_1920': image_b64})
        if image_b64:
            image_meta[woo_id] = {'hash': digest, 'src': src, 'role': 'variant'}
            if binding:
                meta['images'] = image_meta
                binding.write({'external_data': meta})

    def _woo_prepare_export_images(self, template):
        """Build WooCommerce images payload from Odoo main + gallery images."""
        self.ensure_one()
        images = []
        binding = self._woo_binding()._kits_find_binding(
            self, 'product.template', odoo_record_id=template.id,
        )
        image_meta = dict((binding.external_data or {}).get('images') or {}) if binding else {}

        if template.image_1920:
            entry = {'position': 0}
            main_remote = next(
                (k for k, v in image_meta.items() if v.get('role') == 'main' and k.isdigit()),
                None,
            )
            if main_remote:
                entry['id'] = int(main_remote)
                images.append(entry)
            else:
                uploaded = self._woo_upload_image_media(
                    template.image_1920,
                    filename=f'product-{template.id}.jpg',
                )
                if uploaded and uploaded.get('id'):
                    entry['id'] = int(uploaded['id'])
                    images.append(entry)
                elif uploaded and uploaded.get('url'):
                    entry['src'] = uploaded['url']
                    images.append(entry)

        if self._woo_has_product_gallery():
            position = 1
            for img in template.product_template_image_ids.sorted('sequence'):
                if not img.image_1920:
                    continue
                entry = {'position': position}
                woo_id = None
                if img.name and img.name.startswith('woo:') and img.name[4:].isdigit():
                    woo_id = int(img.name[4:])
                if woo_id:
                    entry['id'] = woo_id
                    images.append(entry)
                    position += 1
                else:
                    uploaded = self._woo_upload_image_media(
                        img.image_1920,
                        filename=f'product-{template.id}-gallery-{img.id}.jpg',
                    )
                    if uploaded and uploaded.get('id'):
                        entry['id'] = int(uploaded['id'])
                        images.append(entry)
                        position += 1
                    elif uploaded and uploaded.get('url'):
                        entry['src'] = uploaded['url']
                        images.append(entry)
                        position += 1
        return images

    def _woo_prepare_variant_export_image(self, variant):
        self.ensure_one()
        if not variant.image_variant_1920:
            return None
        binding = self._woo_binding()._kits_find_binding(
            self, 'product.product', odoo_record_id=variant.id,
        )
        image_meta = dict((binding.external_data or {}).get('images') or {}) if binding else {}
        remote = next(
            (k for k, v in image_meta.items() if v.get('role') == 'variant' and str(k).isdigit()),
            None,
        )
        if remote:
            return {'id': int(remote)}
        uploaded = self._woo_upload_image_media(
            variant.image_variant_1920,
            filename=f'variant-{variant.id}.jpg',
        )
        if uploaded and uploaded.get('id'):
            return {'id': int(uploaded['id'])}
        if uploaded and uploaded.get('url'):
            return {'src': uploaded['url']}
        return None

    def _woo_get_wp_media_credentials(self):
        """Return (username, password) for WordPress media upload via XML-RPC."""
        self.ensure_one()
        cfg = dict(self.sudo().config_json or {})
        user = (cfg.get('woo_wp_username') or '').strip()
        password = (cfg.get('woo_wp_password') or '').strip()
        return user, password

    def _woo_image_raw_bytes(self, image_value):
        """Return (raw_bytes, mimetype) from an Odoo Binary/Image field value."""
        if not image_value:
            return False, None
        mime = 'image/jpeg'
        if hasattr(image_value, 'content'):
            raw = image_value.content
            mime = getattr(image_value, 'mimetype', None) or mime
            return raw, mime
        if hasattr(image_value, 'to_base64'):
            raw = base64.b64decode(image_value.to_base64())
            mime = getattr(image_value, 'mimetype', None) or mime
            return raw, mime
        if isinstance(image_value, memoryview):
            return bytes(image_value), mime
        if isinstance(image_value, bytes):
            # ASCII base64 or raw binary
            try:
                decoded = base64.b64decode(image_value, validate=True)
                # Heuristic: if it decodes and looks like an image header, use decoded
                if decoded[:3] == b'\xff\xd8\xff' or decoded[:8] == b'\x89PNG\r\n\x1a\n':
                    return decoded, mime
            except Exception:  # noqa: BLE001
                pass
            return image_value, mime
        if isinstance(image_value, str):
            payload = image_value
            if payload.startswith('data:'):
                _, _, payload = payload.partition(',')
            return base64.b64decode(payload), mime
        return False, None

    def _woo_upload_image_media(self, image_value, filename='product.jpg'):
        """Upload image bytes to the WordPress media library; return {id, url} or None.

        Modern WooCommerce cannot accept data-URI ``src`` values. Uploading via
        XML-RPC and attaching by media id avoids sideload URL restrictions.
        Credentials: ``config_json['woo_wp_username']`` / ``woo_wp_password``.
        """
        self.ensure_one()
        user, password = self._woo_get_wp_media_credentials()
        if not user or not password:
            _logger.warning(
                'WooCommerce image export skipped upload: set config_json '
                'woo_wp_username / woo_wp_password for store %s',
                self.id,
            )
            return None
        raw, mime = self._woo_image_raw_bytes(image_value)
        if not raw:
            return None
        mime = mime or mimetypes.guess_type(filename)[0] or 'image/jpeg'
        store = (self.base_url or '').rstrip('/')
        if not store:
            return None
        endpoint = f'{store}/xmlrpc.php'
        try:
            proxy = xmlrpc_client.ServerProxy(endpoint, allow_none=True)
            result = proxy.metaWeblog.newMediaObject(
                1,
                user,
                password,
                {
                    'name': filename,
                    'type': mime,
                    'bits': xmlrpc_client.Binary(raw),
                },
            )
        except Exception:  # noqa: BLE001
            _logger.exception(
                'WordPress media upload failed for store %s via %s', self.id, endpoint,
            )
            return None
        if not isinstance(result, dict):
            return None
        media_id = result.get('id') or result.get('attachment_id')
        url = result.get('url') or result.get('link')
        if not media_id and not url:
            return None
        return {
            'id': int(media_id) if media_id else False,
            'url': url or False,
        }

    @staticmethod
    def _woo_image_data_uri(image_b64):
        """Build a data URI (legacy; modern WooCommerce rejects these on export).

        Odoo 20 Binary/Image fields return ``BinaryValue`` /
        ``BinaryValueAttachment`` objects (not raw base64 strings). Those must
        be converted via ``to_base64()``; stringifying them yields a useless
        repr like ``BinaryValueAttachment(id=N)``.
        """
        if not image_b64:
            return False
        mime = 'image/jpeg'
        if hasattr(image_b64, 'to_base64'):
            payload = image_b64.to_base64()
            mime = getattr(image_b64, 'mimetype', None) or mime
        elif isinstance(image_b64, memoryview):
            payload = base64.b64encode(bytes(image_b64)).decode('ascii')
        elif isinstance(image_b64, bytes):
            try:
                payload = image_b64.decode('ascii')
            except UnicodeDecodeError:
                payload = base64.b64encode(image_b64).decode('ascii')
        else:
            payload = image_b64
        if isinstance(payload, str) and payload.startswith('data:'):
            return payload
        if not payload:
            return False
        return f'data:{mime};base64,{payload}'
