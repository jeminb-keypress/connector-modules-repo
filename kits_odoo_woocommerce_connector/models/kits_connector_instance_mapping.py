# -*- coding: utf-8 -*-
import base64
import csv
import io

from odoo import models
from odoo.exceptions import UserError
from odoo.tools.binary import BinaryValue

try:
    from openpyxl import load_workbook
except ImportError:  # pragma: no cover - openpyxl ships with Odoo 20
    load_workbook = None


_HEADER_ALIASES = {
    'odoo_id': 'odoo_id',
    'product_id': 'odoo_id',
    'template_id': 'odoo_id',
    'sku': 'sku',
    'default_code': 'sku',
    'internal_reference': 'sku',
    'woo_id': 'woo_id',
    'woo_product_id': 'woo_id',
    'external_id': 'woo_id',
    'woocommerce_id': 'woo_id',
}


class KitsConnectorInstanceMapping(models.Model):
    _inherit = 'kits.connector.instance'

    def _woo_map_products(self, content, filename):
        """Link existing Odoo products to existing WooCommerce ids. Create nothing."""
        self.ensure_one()
        rows = self._woo_parse_product_map(content, filename)
        if not rows:
            raise UserError(self.env._('The mapping file has no data rows.'))
        prepared, errors = self._woo_prepare_product_map(rows)
        applied = 0
        notes = []
        for row in prepared:
            self._woo_bind(
                'product.template',
                row['template'].id,
                row['woo_id'],
                external_data={'title': row['template'].name},
            )
            template = row['template']
            if len(template.product_variant_ids) == 1:
                variant = template.product_variant_id
                current = self._woo_record_binding('product.product', odoo_record_id=variant.id)
                if current and str(current.external_id) != str(row['woo_id']):
                    notes.append(self.env._(
                        'Row %(row)s: template linked. Variant %(sku)s kept its existing WooCommerce id %(woo)s.',
                        row=row['row'],
                        sku=variant.default_code or variant.display_name,
                        woo=current.external_id,
                    ))
                elif not current:
                    self._woo_bind(
                        'product.product',
                        variant.id,
                        row['woo_id'],
                        external_data={'sku': variant.default_code, 'parent_id': False},
                    )
            else:
                notes.append(self.env._(
                    'Row %(row)s: linked the product. Variant ids were not guessed for a product with several variants.',
                    row=row['row'],
                ))
            applied += 1
            self._kits_log_operation(
                operation='woo_map_products',
                status='success',
                level='info',
                message=self.env._(
                    'Linked %(product)s to WooCommerce product %(woo)s.',
                    product=template.display_name,
                    woo=row['woo_id'],
                ),
                details=f'product.template,{template.id}',
            )
        for message in errors:
            self._kits_log_operation(
                operation='woo_map_products',
                status='failure',
                level='warning',
                message=message,
            )
        summary = self.env._(
            'Product mapping finished: linked %(ok)s, failed %(failed)s.',
            ok=applied,
            failed=len(errors),
        )
        details = errors[:12] + notes[:8]
        if details:
            summary = summary + '\n' + '\n'.join(details)
        notify_type = 'success' if applied and not errors else ('warning' if applied else 'danger')
        return self._woo_notify(summary, notify_type=notify_type, sticky=bool(errors or notes))

    def _woo_mapping_file_bytes(self, content):
        """Return raw file bytes from a wizard Binary field or legacy base64 input.

        Odoo 20 Binary fields expose ``BinaryValue`` (decoded bytes). Older call
        sites and unit tests still pass base64-encoded ``bytes`` or ``str``.
        """
        if content in (None, False, ''):
            return b''
        if isinstance(content, BinaryValue):
            return bytes(content)
        if isinstance(content, str):
            return base64.b64decode(content)
        return base64.b64decode(content or b'')

    def _woo_parse_product_map(self, content, filename):
        raw = self._woo_mapping_file_bytes(content)
        name = (filename or '').lower()
        if name.endswith('.xlsx'):
            return self._woo_parse_product_map_xlsx(raw)
        if name.endswith('.xls'):
            raise UserError(self.env._('Save the workbook as .xlsx or .csv and upload it again.'))
        return self._woo_parse_product_map_csv(raw)

    def _woo_parse_product_map_csv(self, raw):
        text = raw.decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise UserError(self.env._('The CSV file needs a header row.'))
        headers = self._woo_map_headers(reader.fieldnames)
        rows = []
        for index, source in enumerate(reader, start=2):
            row = {key: (source.get(header) or '').strip() for key, header in headers.items()}
            if not any(row.values()):
                continue
            row['row'] = index
            rows.append(row)
        return rows

    def _woo_parse_product_map_xlsx(self, raw):
        if load_workbook is None:
            raise UserError(self.env._('Excel support is unavailable. Upload a CSV file.'))
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        sheet = workbook.active
        iterator = sheet.iter_rows(values_only=True)
        try:
            header_row = next(iterator)
        except StopIteration as exc:
            raise UserError(self.env._('The workbook is empty.')) from exc
        headers = self._woo_map_headers([cell if cell is not None else '' for cell in header_row])
        positions = {key: index for index, key in enumerate(
            self._woo_header_key(cell) for cell in header_row
        ) if key}
        rows = []
        for index, values in enumerate(iterator, start=2):
            row = {}
            for key, position in positions.items():
                value = values[position] if position < len(values) else ''
                row[key] = '' if value is None else str(value).strip()
            if not any(row.get(key) for key in ('odoo_id', 'sku', 'woo_id')):
                continue
            row['row'] = index
            rows.append(row)
        return rows

    def _woo_map_headers(self, fieldnames):
        found = {}
        for name in fieldnames:
            key = self._woo_header_key(name)
            if key:
                found[key] = name
        if 'woo_id' not in found or not ({'odoo_id', 'sku'} & set(found)):
            raise UserError(self.env._(
                'The header must include a WooCommerce product id column '
                '(woo_id or woo_product_id) and either odoo_id or sku.'
            ))
        return found

    @staticmethod
    def _woo_header_key(name):
        text = str(name or '').strip().lower().replace(' ', '_').replace('-', '_')
        return _HEADER_ALIASES.get(text)

    def _woo_prepare_product_map(self, rows):
        """Validate every row before any binding is written."""
        self.ensure_one()
        errors = []
        candidates = []
        woo_rows = {}
        template_rows = {}
        for row in rows:
            template, message = self._woo_resolve_map_template(row)
            woo_text = (row.get('woo_id') or '').strip()
            try:
                woo_id = int(float(woo_text)) if woo_text else 0
            except (TypeError, ValueError):
                woo_id = 0
            if not woo_id:
                message = message or self.env._('Row %(row)s: WooCommerce product id is missing or invalid.', row=row['row'])
            if template and woo_id:
                woo_rows.setdefault(woo_id, []).append(row['row'])
                template_rows.setdefault(template.id, []).append(row['row'])
                candidates.append({'row': row['row'], 'template': template, 'woo_id': woo_id})
            elif message:
                errors.append(message)
            else:
                errors.append(self.env._('Row %(row)s: the product could not be resolved.', row=row['row']))
        duplicate_woo = {woo_id for woo_id, indexes in woo_rows.items() if len(indexes) > 1}
        duplicate_templates = {template_id for template_id, indexes in template_rows.items() if len(indexes) > 1}
        prepared = []
        for row in candidates:
            if row['woo_id'] in duplicate_woo:
                errors.append(self.env._(
                    'Row %(row)s: WooCommerce product %(woo)s is repeated in the file.',
                    row=row['row'],
                    woo=row['woo_id'],
                ))
                continue
            if row['template'].id in duplicate_templates:
                errors.append(self.env._(
                    'Row %(row)s: Odoo product %(product)s is repeated in the file.',
                    row=row['row'],
                    product=row['template'].display_name,
                ))
                continue
            by_woo = self.env['kits.woo.product.binding']._find(self, external_id=str(row['woo_id']))
            by_template = self.env['kits.woo.product.binding']._find(
                self, odoo_record_id=row['template'].id,
            )
            if by_woo and by_woo.product_tmpl_id != row['template']:
                errors.append(self.env._(
                    'Row %(row)s: WooCommerce product %(woo)s is already linked to %(product)s.',
                    row=row['row'],
                    woo=row['woo_id'],
                    product=by_woo.product_tmpl_id.display_name,
                ))
                continue
            if by_template and str(by_template.external_id) != str(row['woo_id']):
                errors.append(self.env._(
                    'Row %(row)s: %(product)s is already linked to WooCommerce product %(woo)s.',
                    row=row['row'],
                    product=row['template'].display_name,
                    woo=by_template.external_id,
                ))
                continue
            prepared.append(row)
        return prepared, errors

    def _woo_resolve_map_template(self, row):
        self.ensure_one()
        Template = self.env['product.template']
        Product = self.env['product.product']
        company_domain = [('company_id', 'in', [False, self.company_id.id])]
        template = Template.browse()
        sku = (row.get('sku') or '').strip()
        odoo_text = (row.get('odoo_id') or '').strip()
        if sku:
            variants = Product.search(company_domain + [('default_code', '=', sku)])
            if not variants:
                return Template.browse(), self.env._(
                    'Row %(row)s: no product has SKU %(sku)s.',
                    row=row['row'],
                    sku=sku,
                )
            if len(variants) > 1:
                return Template.browse(), self.env._(
                    'Row %(row)s: SKU %(sku)s matches %(count)s Odoo variants.',
                    row=row['row'],
                    sku=sku,
                    count=len(variants),
                )
            template = variants.product_tmpl_id
        if odoo_text:
            try:
                odoo_id = int(float(odoo_text))
            except (TypeError, ValueError):
                return Template.browse(), self.env._(
                    'Row %(row)s: Odoo id %(value)s is not a number.',
                    row=row['row'],
                    value=odoo_text,
                )
            by_template = Template.search(company_domain + [('id', '=', odoo_id)], limit=1)
            by_variant = Product.search(company_domain + [('id', '=', odoo_id)], limit=1)
            chosen = by_template or by_variant.product_tmpl_id
            if not chosen:
                return Template.browse(), self.env._(
                    'Row %(row)s: Odoo product %(value)s was not found.',
                    row=row['row'],
                    value=odoo_id,
                )
            if template and chosen != template:
                return Template.browse(), self.env._(
                    'Row %(row)s: SKU %(sku)s and Odoo id %(value)s point to different products.',
                    row=row['row'],
                    sku=sku,
                    value=odoo_id,
                )
            template = chosen
        if not sku and not odoo_text:
            return Template.browse(), self.env._(
                'Row %(row)s: enter an Odoo id or a SKU.',
                row=row['row'],
            )
        return template, False
