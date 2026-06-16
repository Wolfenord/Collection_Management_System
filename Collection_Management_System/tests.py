"""Automated regression tests for the Collection Management System.

Covers the full feature set: auth, collections + row-level permissions, dynamic
fields, item CRUD with per-"Art" required fields and file uploads, filtering,
QR/barcode, Excel export, statistics, sharing and "copy structure from template".
"""

import io
import json
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from .models import (
    Collection,
    CollectionShare,
    FieldDefinition,
    FieldType,
    Item,
    ItemType,
)
from .services import create_default_fields

User = get_user_model()

MEDIA = tempfile.mkdtemp()


def make_png(color='red') -> bytes:
    buf = io.BytesIO()
    Image.new('RGB', (8, 8), color).save(buf, 'PNG')
    return buf.getvalue()


def add_field(collection, key, label, field_type, order=0, required=False, config=None):
    return FieldDefinition.objects.create(
        collection=collection, key=key, label=label, field_type=field_type,
        order=order, required=required, config=config or {},
    )


class AuthTests(TestCase):
    def test_register_creates_user_and_logs_in(self):
        resp = self.client.post(reverse('register'), {
            'username': 'neo', 'email': 'neo@e.de',
            'password1': 'Sehr-Sicher-123', 'password2': 'Sehr-Sicher-123',
        })
        self.assertRedirects(resp, reverse('dashboard'))
        self.assertTrue(User.objects.filter(username='neo').exists())
        self.assertIn('_auth_user_id', self.client.session)

    def test_dashboard_requires_login(self):
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('login'), resp['Location'])


class CollectionTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.stranger = User.objects.create_user('stranger', 's@e.de', 'pw')

    def test_create_seeds_default_fields(self):
        self.client.force_login(self.owner)
        resp = self.client.post(reverse('collection_create'), {'name': 'Filme', 'description': ''})
        col = Collection.objects.get(name='Filme')
        self.assertRedirects(resp, reverse('collection_detail', args=[col.pk]))
        self.assertEqual(col.owner, self.owner)
        self.assertEqual(
            sorted(col.fields.values_list('key', flat=True)),
            ['beleg', 'bild', 'kaufdatum', 'name', 'ort', 'preis'],
        )

    def test_stranger_cannot_view(self):
        col = Collection.objects.create(owner=self.owner, name='Privat')
        self.client.force_login(self.stranger)
        resp = self.client.get(reverse('collection_detail', args=[col.pk]))
        self.assertEqual(resp.status_code, 403)


class FieldTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Filme')
        self.client.force_login(self.owner)

    def test_create_choice_field_stores_choices(self):
        self.client.post(reverse('field_create', args=[self.col.pk]), {
            'label': 'Genre', 'key': '', 'field_type': 'choice', 'order': 1,
            'choices_text': 'Action\nDrama',
        })
        field = FieldDefinition.objects.get(collection=self.col, key='genre')
        self.assertEqual(field.config.get('choices'), ['Action', 'Drama'])

    def test_delete_field_removes_values_everywhere(self):
        field = add_field(self.col, 'genre', 'Genre', FieldType.CHOICE, config={'choices': ['A']})
        item = Item.objects.create(collection=self.col, values={'genre': 'A', 'name': 'X'})
        self.client.post(reverse('field_delete', args=[self.col.pk, field.pk]))
        item.refresh_from_db()
        self.assertNotIn('genre', item.values)
        self.assertFalse(FieldDefinition.objects.filter(pk=field.pk).exists())


class FieldReorderTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.viewer = User.objects.create_user('viewer', 'v@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Filme')
        self.a = add_field(self.col, 'a', 'A', FieldType.TEXT, order=0)
        self.b = add_field(self.col, 'b', 'B', FieldType.TEXT, order=1)
        self.c = add_field(self.col, 'c', 'C', FieldType.TEXT, order=2)
        self.url = reverse('field_reorder', args=[self.col.pk])

    def test_reorder_persists_new_order(self):
        self.client.force_login(self.owner)
        resp = self.client.post(
            self.url,
            data=json.dumps({'order': [str(self.c.pk), str(self.a.pk), str(self.b.pk)]}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(list(self.col.fields.values_list('key', flat=True)), ['c', 'a', 'b'])

    def test_get_not_allowed(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_viewer_cannot_reorder(self):
        CollectionShare.objects.create(collection=self.col, user=self.viewer, permission='view')
        self.client.force_login(self.viewer)
        resp = self.client.post(self.url, data=json.dumps({'order': []}), content_type='application/json')
        self.assertEqual(resp.status_code, 403)


@override_settings(MEDIA_ROOT=MEDIA)
class ItemTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.viewer = User.objects.create_user('viewer', 'v@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Filme')
        create_default_fields(self.col)
        add_field(self.col, 'genre', 'Genre', FieldType.CHOICE, order=10, config={'choices': ['Action', 'Drama']})
        self.dvd = ItemType.objects.create(collection=self.col, name='DVD')
        self.dvd.required_fields.set([self.col.fields.get(key='genre')])
        self.client.force_login(self.owner)

    def test_per_type_required_blocks_missing_value(self):
        resp = self.client.post(reverse('item_create', args=[self.col.pk]),
                                {'__item_type': str(self.dvd.pk), 'name': 'Matrix'})
        self.assertEqual(resp.status_code, 200)  # re-render with error
        self.assertEqual(Item.objects.count(), 0)

    def test_create_with_file_upload_creates_asset(self):
        resp = self.client.post(reverse('item_create', args=[self.col.pk]), {
            '__item_type': str(self.dvd.pk), 'name': 'Matrix', 'genre': 'Action',
            'preis': '9.99', 'kaufdatum': '2024-05-01',
            'bild': SimpleUploadedFile('p.png', make_png(), content_type='image/png'),
        })
        self.assertEqual(resp.status_code, 302)
        item = Item.objects.get()
        self.assertEqual(item.values['name'], 'Matrix')
        self.assertEqual(item.values['preis'], 9.99)  # stored as number
        self.assertTrue(item.assets.filter(field_key='bild').exists())
        self.assertIn('url', item.values['bild'])

    def test_edit_keeps_existing_file_when_not_replaced(self):
        item = Item.objects.create(collection=self.col, item_type=self.dvd,
                                   values={'name': 'Matrix', 'genre': 'Action'})
        item.assets.create(field_key='bild', file=SimpleUploadedFile('p.png', make_png()))
        resp = self.client.post(reverse('item_edit', args=[self.col.pk, item.pk]),
                                {'__item_type': str(self.dvd.pk), 'name': 'Reloaded', 'genre': 'Drama'})
        self.assertEqual(resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.values['name'], 'Reloaded')
        self.assertTrue(item.assets.filter(field_key='bild').exists())

    def test_viewer_cannot_create(self):
        CollectionShare.objects.create(collection=self.col, user=self.viewer, permission='view')
        self.client.force_login(self.viewer)
        resp = self.client.post(reverse('item_create', args=[self.col.pk]), {'name': 'x'})
        self.assertEqual(resp.status_code, 403)


class FilterTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Filme')
        add_field(self.col, 'name', 'Name', FieldType.TEXT, order=0)
        add_field(self.col, 'genre', 'Genre', FieldType.CHOICE, order=1, config={'choices': ['Action', 'Drama']})
        add_field(self.col, 'jahr', 'Jahr', FieldType.YEAR, order=2)
        Item.objects.create(collection=self.col, values={'name': 'Matrix', 'genre': 'Action', 'jahr': 1999})
        Item.objects.create(collection=self.col, values={'name': 'Amelie', 'genre': 'Drama', 'jahr': 2001})
        self.client.force_login(self.owner)

    def _count(self, query):
        return self.client.get(reverse('collection_detail', args=[self.col.pk]), query).context['result_count']

    def test_text_choice_and_range_filters(self):
        self.assertEqual(self._count({'q': 'matrix'}), 1)
        self.assertEqual(self._count({'f_genre': 'Action'}), 1)
        self.assertEqual(self._count({'min_jahr': '2000'}), 1)
        self.assertEqual(self._count({'min_jahr': '1990', 'max_jahr': '2010'}), 2)

    def test_multichoice_membership_including_non_ascii(self):
        add_field(self.col, 'tags', 'Tags', FieldType.MULTICHOICE, order=3,
                  config={'choices': ['Action', 'Komödie']})
        Item.objects.create(collection=self.col, values={'name': 'X', 'tags': ['Action', 'Komödie']})
        Item.objects.create(collection=self.col, values={'name': 'Y', 'tags': ['Komödie']})
        self.assertEqual(self._count({'f_tags': 'Action'}), 1)
        self.assertEqual(self._count({'f_tags': 'Komödie'}), 2)  # non-ASCII must match too


class CodeTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.stranger = User.objects.create_user('stranger', 's@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Filme')
        add_field(self.col, 'name', 'Name', FieldType.TEXT)
        self.item = Item.objects.create(collection=self.col, values={'name': 'Matrix'})

    def test_qr_and_barcode_return_png(self):
        self.client.force_login(self.owner)
        for url in [
            reverse('collection_qr', args=[self.col.pk]),
            reverse('item_qr', args=[self.col.pk, self.item.pk]),
            reverse('item_barcode', args=[self.col.pk, self.item.pk]),
        ]:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp['Content-Type'], 'image/png')
            self.assertTrue(resp.content.startswith(b'\x89PNG'))

    def test_codes_require_access(self):
        self.client.force_login(self.stranger)
        resp = self.client.get(reverse('item_qr', args=[self.col.pk, self.item.pk]))
        self.assertEqual(resp.status_code, 403)


class LabelTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.stranger = User.objects.create_user('stranger', 's@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Filme')
        add_field(self.col, 'name', 'Name', FieldType.TEXT)
        Item.objects.create(collection=self.col, values={'name': 'Matrix'})

    def test_labels_pdf_download(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse('collection_labels', args=[self.col.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/pdf')
        self.assertTrue(resp.content.startswith(b'%PDF'))
        self.assertIn('attachment', resp['Content-Disposition'])

    def test_labels_handles_empty_filter_result(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse('collection_labels', args=[self.col.pk]), {'f_name': 'nichts'})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.content.startswith(b'%PDF'))

    def test_labels_require_access(self):
        self.client.force_login(self.stranger)
        resp = self.client.get(reverse('collection_labels', args=[self.col.pk]))
        self.assertEqual(resp.status_code, 403)


class ExportTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Filme')
        add_field(self.col, 'name', 'Name', FieldType.TEXT, order=0)
        add_field(self.col, 'genre', 'Genre', FieldType.CHOICE, order=1, config={'choices': ['Action', 'Drama']})
        Item.objects.create(collection=self.col, values={'name': 'Matrix', 'genre': 'Action'})
        Item.objects.create(collection=self.col, values={'name': 'Amelie', 'genre': 'Drama'})
        self.client.force_login(self.owner)

    def test_export_headers_and_filter(self):
        from openpyxl import load_workbook
        resp = self.client.get(reverse('collection_export', args=[self.col.pk]))
        self.assertEqual(resp.status_code, 200)
        ws = load_workbook(io.BytesIO(resp.content)).active
        self.assertEqual([c.value for c in ws[1]], ['ID', 'Art', 'Name', 'Genre'])
        self.assertEqual(ws.max_row, 3)  # header + 2 items

        resp = self.client.get(reverse('collection_export', args=[self.col.pk]), {'f_genre': 'Action'})
        ws = load_workbook(io.BytesIO(resp.content)).active
        self.assertEqual(ws.max_row, 2)  # header + 1 item


class StatisticsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Filme')
        add_field(self.col, 'preis', 'Preis', FieldType.PRICE, order=0, config={'currency': 'EUR'})
        add_field(self.col, 'genre', 'Genre', FieldType.CHOICE, order=1, config={'choices': ['Action', 'Drama']})
        Item.objects.create(collection=self.col, values={'preis': 10.0, 'genre': 'Action'})
        Item.objects.create(collection=self.col, values={'preis': 20.0, 'genre': 'Action'})
        Item.objects.create(collection=self.col, values={'preis': 15.0, 'genre': 'Drama'})
        self.client.force_login(self.owner)

    def test_numeric_total_and_filtered(self):
        resp = self.client.get(reverse('collection_statistics', args=[self.col.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['total_value'], 45.0)
        self.assertEqual(resp.context['stats']['total_items'], 3)
        price = next(n for n in resp.context['stats']['numeric'] if n['field'].key == 'preis')
        self.assertEqual((price['sum'], price['avg'], price['min'], price['max']), (45.0, 15.0, 10.0, 20.0))

        resp = self.client.get(reverse('collection_statistics', args=[self.col.pk]), {'f_genre': 'Action'})
        self.assertEqual(resp.context['total_value'], 30.0)


class ShareTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.anna = User.objects.create_user('anna', 'anna@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Filme')
        add_field(self.col, 'name', 'Name', FieldType.TEXT)
        self.url = reverse('collection_shares', args=[self.col.pk])

    def test_share_grants_view_then_upgrade_to_edit(self):
        self.client.force_login(self.owner)
        self.client.post(self.url, {'identifier': 'anna@e.de', 'permission': 'view'})
        self.assertEqual(self.col.shares.count(), 1)

        self.client.force_login(self.anna)
        self.assertEqual(self.client.get(reverse('collection_detail', args=[self.col.pk])).status_code, 200)
        self.assertEqual(self.client.post(reverse('item_create', args=[self.col.pk]), {'name': 'x'}).status_code, 403)

        # upgrade by username -> still a single share, now editable
        self.client.force_login(self.owner)
        self.client.post(self.url, {'identifier': 'anna', 'permission': 'edit'})
        self.assertEqual(self.col.shares.count(), 1)
        self.client.force_login(self.anna)
        self.assertEqual(self.client.post(reverse('item_create', args=[self.col.pk]), {'name': 'x'}).status_code, 302)

    def test_non_owner_cannot_manage_shares(self):
        self.client.force_login(self.anna)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_share_errors(self):
        self.client.force_login(self.owner)
        resp = self.client.post(self.url, {'identifier': 'nobody', 'permission': 'view'})
        self.assertTrue(resp.context['form'].errors)
        resp = self.client.post(self.url, {'identifier': 'owner', 'permission': 'view'})
        self.assertTrue(resp.context['form'].errors)


class CopyStructureTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.other = User.objects.create_user('other', 'p@e.de', 'pw')
        self.src = Collection.objects.create(owner=self.owner, name='Vorlage')
        add_field(self.src, 'name', 'Name', FieldType.TEXT, order=0)
        add_field(self.src, 'genre', 'Genre', FieldType.CHOICE, order=1, config={'choices': ['A']})
        dvd = ItemType.objects.create(collection=self.src, name='DVD')
        dvd.required_fields.set([self.src.fields.get(key='genre')])
        Item.objects.create(collection=self.src, values={'name': 'Matrix'})
        self.client.force_login(self.owner)

    def test_copy_fields_and_types_not_items(self):
        self.client.post(reverse('collection_create'),
                         {'name': 'Neu', 'description': '', 'template': str(self.src.pk)})
        neu = Collection.objects.get(name='Neu')
        self.assertEqual(sorted(neu.fields.values_list('key', flat=True)), ['genre', 'name'])
        new_type = neu.item_types.get(name='DVD')
        self.assertEqual(list(new_type.required_fields.values_list('key', flat=True)), ['genre'])
        self.assertEqual(new_type.required_fields.first().collection_id, neu.pk)
        self.assertEqual(neu.items.count(), 0)

    def test_cannot_use_inaccessible_collection_as_template(self):
        private = Collection.objects.create(owner=self.other, name='Privat')
        resp = self.client.post(reverse('collection_create'),
                                {'name': 'Hack', 'template': str(private.pk)})
        self.assertTrue(resp.context['form'].errors.get('template'))
        self.assertFalse(Collection.objects.filter(name='Hack').exists())
