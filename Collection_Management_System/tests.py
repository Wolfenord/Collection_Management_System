"""Automated regression tests for the Collection Management System.

Covers the full feature set: auth, collections + row-level permissions, dynamic
fields, item CRUD with per-"Art" required fields and file uploads, filtering,
QR/barcode, Excel export, statistics, sharing and "copy structure from template".
"""

import io
import json
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from . import imports, lookup_providers
from .models import (
    Collection,
    CollectionShare,
    FieldDefinition,
    FieldType,
    Item,
    ItemType,
    Loan,
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
    def test_register_creates_pending_user_awaiting_approval(self):
        # Registration now puts the account on the admin-approval whitelist:
        # it is created locked and the user is NOT logged in. See
        # accounts/tests.py for the full approval-workflow coverage.
        resp = self.client.post(reverse('register'), {
            'username': 'neo', 'email': 'neo@e.de',
            'password1': 'Sehr-Sicher-123', 'password2': 'Sehr-Sicher-123',
        })
        self.assertRedirects(resp, reverse('login'))
        user = User.objects.get(username='neo')
        self.assertFalse(user.is_active)
        self.assertEqual(user.approval_status, User.APPROVAL_PENDING)
        self.assertNotIn('_auth_user_id', self.client.session)

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

    def test_item_form_offers_camera_capture_for_file_fields(self):
        resp = self.client.get(reverse('item_create', args=[self.col.pk]))
        self.assertContains(resp, 'data-capture="image"')  # Bild
        self.assertContains(resp, 'data-capture="file"')  # Beleg
        self.assertContains(resp, 'id="captureModal"')
        self.assertContains(resp, 'js/capture.js')

    def test_autofill_cover_url_is_downloaded_into_asset(self):
        with mock.patch.object(lookup_providers, 'fetch_cover', return_value=(b'IMG', 'jpg')) as m:
            resp = self.client.post(reverse('item_create', args=[self.col.pk]), {
                '__item_type': str(self.dvd.pk), 'name': 'Matrix', 'genre': 'Action',
                'bild__cover_url': 'https://portal.dnb.de/opac/mvb/cover?isbn=9783608963762',
            })
        self.assertEqual(resp.status_code, 302)
        m.assert_called_once()
        item = Item.objects.get()
        self.assertTrue(item.assets.filter(field_key='bild').exists())
        self.assertIn('url', item.values['bild'])

    def test_uploaded_file_wins_over_autofill_cover(self):
        with mock.patch.object(lookup_providers, 'fetch_cover') as m:
            resp = self.client.post(reverse('item_create', args=[self.col.pk]), {
                '__item_type': str(self.dvd.pk), 'name': 'Matrix', 'genre': 'Action',
                'bild__cover_url': 'https://portal.dnb.de/opac/mvb/cover?isbn=1',
                'bild': SimpleUploadedFile('eigen.png', make_png(), content_type='image/png'),
            })
        self.assertEqual(resp.status_code, 302)
        m.assert_not_called()
        self.assertEqual(Item.objects.get().assets.get(field_key='bild').original_name, 'eigen.png')


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


# --- External-database auto-fill (QR/ISBN scan) -------------------------------

OPENLIBRARY_PAYLOAD = {
    'ISBN:9780132350884': {
        'title': 'Clean Code',
        'authors': [{'name': 'Robert C. Martin'}],
        'publishers': [{'name': 'Prentice Hall'}],
        'publish_date': '2008',
        'number_of_pages': 464,
        'subjects': [{'name': 'Software engineering'}],
        'identifiers': {'isbn_13': ['9780132350884']},
        'cover': {'large': 'https://covers.example/large.jpg'},
    }
}

GOOGLE_PAYLOAD = {
    'totalItems': 1,
    'items': [{
        'volumeInfo': {
            'title': 'Clean Code',
            'authors': ['Robert C. Martin'],
            'publisher': 'Prentice Hall',
            'publishedDate': '2008-08-01',
            'pageCount': 464,
            'description': 'A handbook of agile software craftsmanship.',
            'categories': ['Computers'],
            'language': 'en',
            'industryIdentifiers': [{'type': 'ISBN_13', 'identifier': '9780132350884'}],
            'imageLinks': {'thumbnail': 'https://books.example/thumb.jpg'},
        }
    }],
}


DNB_PAYLOAD = """<?xml version="1.0" encoding="UTF-8"?>
<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/"><version>1.1</version>
<numberOfRecords>1</numberOfRecords><records><record><recordSchema>oai_dc</recordSchema>
<recordPacking>xml</recordPacking><recordData>
<dc xmlns="http://www.openarchives.org/OAI/2.0/oai_dc/" xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>[Brief answers to the big questions] ; Kurze Antworten auf große Fragen / Stephen Hawking</dc:title>
  <dc:creator>Hawking, Stephen W. [Verfasser]</dc:creator>
  <dc:creator>Kober, Hainer [Übersetzer]</dc:creator>
  <dc:publisher>Stuttgart : Klett-Cotta</dc:publisher>
  <dc:date>2018</dc:date>
  <dc:language>ger</dc:language>
  <dc:identifier xsi:type="tel:ISBN">978-3-608-96376-2 Festeinband : circa EUR 16.00 (DE)</dc:identifier>
  <dc:identifier xsi:type="tel:ISBN">3-608-96376-6</dc:identifier>
  <dc:subject>500 Naturwissenschaften</dc:subject>
  <dc:format>252 Seiten</dc:format>
</dc></recordData></record></records></searchRetrieveResponse>"""

DNB_EMPTY_PAYLOAD = """<?xml version="1.0" encoding="UTF-8"?>
<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/">
<version>1.1</version><numberOfRecords>0</numberOfRecords></searchRetrieveResponse>"""

DNB_SEARCH_PAYLOAD = """<?xml version="1.0" encoding="UTF-8"?>
<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/"><version>1.1</version>
<numberOfRecords>2</numberOfRecords><records>
<record><recordData>
<dc xmlns="http://www.openarchives.org/OAI/2.0/oai_dc/" xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>Kurze Antworten auf große Fragen / Stephen Hawking</dc:title>
  <dc:creator>Hawking, Stephen W. [Verfasser]</dc:creator>
  <dc:date>2018</dc:date>
  <dc:identifier xsi:type="tel:ISBN">978-3-608-96376-2</dc:identifier>
</dc></recordData></record>
<record><recordData>
<dc xmlns="http://www.openarchives.org/OAI/2.0/oai_dc/" xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>Eine kurze Geschichte der Zeit / Stephen Hawking</dc:title>
  <dc:creator>Hawking, Stephen W. [Verfasser]</dc:creator>
  <dc:date>1988</dc:date>
  <dc:identifier xsi:type="tel:ISBN">3-498-02884-7</dc:identifier>
</dc></recordData></record>
</records></searchRetrieveResponse>"""

OPENLIBRARY_SEARCH_PAYLOAD = {
    'docs': [
        {'title': 'Clean Code', 'author_name': ['Robert C. Martin'], 'first_publish_year': 2008,
         'publisher': ['Prentice Hall'], 'isbn': ['0132350882', '9780132350884'],
         'cover_i': 123, 'number_of_pages_median': 464},
        {'title': 'The Clean Coder', 'author_name': ['Robert C. Martin'], 'first_publish_year': 2011},
    ],
}


class ProviderTests(TestCase):
    """The pre-configured providers normalise external data to the shared vocab."""

    def test_openlibrary_normalises_and_strips_isbn_hyphens(self):
        with mock.patch.object(lookup_providers, '_http_get_json', return_value=OPENLIBRARY_PAYLOAD) as m:
            data = lookup_providers.get_provider('openlibrary').fetch('978-0-13-235088-4')
        # hyphenated ISBN must reach the API as bare digits
        self.assertIn('9780132350884', m.call_args[0][0])
        self.assertEqual(data['title'], 'Clean Code')
        self.assertEqual(data['authors'], 'Robert C. Martin')
        self.assertEqual(data['publisher'], 'Prentice Hall')
        self.assertEqual(data['year'], '2008')
        self.assertEqual(data['pages'], 464)
        self.assertEqual(data['isbn'], '9780132350884')
        self.assertTrue(data['cover_url'].endswith('large.jpg'))

    def test_googlebooks_normalises_and_adds_description(self):
        with mock.patch.object(lookup_providers, '_http_get_json', return_value=GOOGLE_PAYLOAD):
            data = lookup_providers.get_provider('googlebooks').fetch('9780132350884')
        self.assertEqual(data['title'], 'Clean Code')
        self.assertEqual(data['year'], '2008')
        self.assertEqual(data['language'], 'en')
        self.assertIn('agile', data['description'])

    def test_dnb_parses_sru_xml_and_cleans_german_records(self):
        with mock.patch.object(lookup_providers, '_http_get_text', return_value=DNB_PAYLOAD) as m:
            data = lookup_providers.get_provider('dnb').fetch('978-3-608-96376-2')
        self.assertIn('9783608963762', m.call_args[0][0])  # bare digits reach the API
        self.assertEqual(data['title'], 'Kurze Antworten auf große Fragen')
        self.assertEqual(data['authors'], 'Stephen W. Hawking')  # only [Verfasser], reordered
        self.assertEqual(data['publisher'], 'Klett-Cotta')  # place stripped
        self.assertEqual(data['year'], '2018')
        self.assertEqual(data['pages'], 252)
        self.assertEqual(data['language'], 'Deutsch')
        self.assertEqual(data['isbn'], '9783608963762')  # ISBN-13 preferred, price stripped
        self.assertEqual(data['categories'], 'Naturwissenschaften')  # DDC number stripped
        self.assertIn('9783608963762', data['cover_url'])

    def test_dnb_no_match_and_bad_xml_return_empty(self):
        with mock.patch.object(lookup_providers, '_http_get_text', return_value=DNB_EMPTY_PAYLOAD):
            self.assertEqual(lookup_providers.get_provider('dnb').fetch('9783608963762'), {})
        with mock.patch.object(lookup_providers, '_http_get_text', return_value='not xml <'):
            self.assertEqual(lookup_providers.get_provider('dnb').fetch('9783608963762'), {})
        with mock.patch.object(lookup_providers, '_http_get_text', return_value=None):
            self.assertEqual(lookup_providers.get_provider('dnb').fetch('9783608963762'), {})

    def test_auto_provider_merges_chain_results(self):
        # DNB answers first (its values win), Google adds the description DNB lacks.
        with mock.patch.object(lookup_providers, '_http_get_text', return_value=DNB_PAYLOAD), \
                mock.patch.object(lookup_providers, '_http_get_json',
                                  side_effect=lambda url: GOOGLE_PAYLOAD if 'googleapis' in url else {}):
            data = lookup_providers.get_provider('auto').fetch('9783608963762')
        self.assertEqual(data['title'], 'Kurze Antworten auf große Fragen')
        self.assertIn('agile', data['description'])

    def test_auto_provider_falls_back_when_first_source_misses(self):
        with mock.patch.object(lookup_providers, '_http_get_text', return_value=DNB_EMPTY_PAYLOAD), \
                mock.patch.object(lookup_providers, '_http_get_json',
                                  side_effect=lambda url: GOOGLE_PAYLOAD if 'googleapis' in url else {}):
            data = lookup_providers.get_provider('auto').fetch('9780132350884')
        self.assertEqual(data['title'], 'Clean Code')

    def test_cover_download_is_restricted_to_provider_hosts(self):
        with mock.patch.object(lookup_providers, '_http_get_bytes') as m:
            self.assertIsNone(lookup_providers.fetch_cover('https://evil.example/cover.jpg'))
            self.assertIsNone(lookup_providers.fetch_cover('file:///etc/passwd'))
            m.assert_not_called()
        with mock.patch.object(lookup_providers, '_http_get_bytes',
                               return_value=(b'IMG', 'image/jpeg')):
            self.assertEqual(lookup_providers.fetch_cover(
                'https://portal.dnb.de/opac/mvb/cover?isbn=1'), (b'IMG', 'jpg'))
        with mock.patch.object(lookup_providers, '_http_get_bytes',
                               return_value=(b'<html>', 'text/html')):
            self.assertIsNone(lookup_providers.fetch_cover('https://portal.dnb.de/opac/mvb/cover?isbn=1'))

    def test_dnb_search_builds_word_query_and_parses_records(self):
        with mock.patch.object(lookup_providers, '_http_get_text', return_value=DNB_SEARCH_PAYLOAD) as m:
            results = lookup_providers.get_provider('dnb').search('kurze antworten hawking')
        url = m.call_args[0][0]
        self.assertIn('WOE%3Dkurze+and+WOE%3Dantworten+and+WOE%3Dhawking', url)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['title'], 'Kurze Antworten auf große Fragen')
        self.assertEqual(results[0]['isbn'], '9783608963762')
        self.assertEqual(results[1]['year'], '1988')

    def test_openlibrary_search_normalises_docs(self):
        with mock.patch.object(lookup_providers, '_http_get_json', return_value=OPENLIBRARY_SEARCH_PAYLOAD):
            results = lookup_providers.get_provider('openlibrary').search('clean code')
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['title'], 'Clean Code')
        self.assertEqual(results[0]['isbn'], '9780132350884')  # ISBN-13 preferred
        self.assertIn('covers.openlibrary.org/b/id/123', results[0]['cover_url'])
        self.assertNotIn('isbn', results[1])  # missing data stays absent

    def test_google_search_returns_candidates(self):
        with mock.patch.object(lookup_providers, '_http_get_json', return_value=GOOGLE_PAYLOAD):
            results = lookup_providers.get_provider('googlebooks').search('clean code')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['title'], 'Clean Code')

    def test_auto_search_falls_back_to_next_source(self):
        with mock.patch.object(lookup_providers, '_http_get_text', return_value=DNB_EMPTY_PAYLOAD), \
                mock.patch.object(lookup_providers, '_http_get_json',
                                  side_effect=lambda url: GOOGLE_PAYLOAD if 'googleapis' in url else {}):
            results = lookup_providers.get_provider('auto').search('clean code')
        self.assertEqual(results[0]['title'], 'Clean Code')

    def test_no_match_returns_empty(self):
        with mock.patch.object(lookup_providers, '_http_get_json', return_value={}):
            self.assertEqual(lookup_providers.get_provider('openlibrary').fetch('000'), {})

    def test_network_failure_returns_none_then_empty(self):
        with mock.patch.object(lookup_providers, '_http_get_json', return_value=None):
            self.assertEqual(lookup_providers.get_provider('googlebooks').fetch('9780132350884'), {})


class LookupViewTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.viewer = User.objects.create_user('viewer', 'v@e.de', 'pw')
        self.coll = Collection.objects.create(owner=self.owner, name='Bücher', lookup_provider='openlibrary')
        add_field(self.coll, 'titel', 'Titel', FieldType.TEXT, order=0, config={'lookup_attribute': 'title'})
        add_field(self.coll, 'autor', 'Autor', FieldType.TEXT, order=1, config={'lookup_attribute': 'authors'})
        add_field(self.coll, 'isbn', 'ISBN', FieldType.ISBN, order=2, config={'lookup_attribute': 'isbn'})
        add_field(self.coll, 'notiz', 'Notiz', FieldType.TEXT, order=3)  # unmapped → never filled
        self.url = reverse('item_lookup', args=[self.coll.pk])

    def test_lookup_maps_provider_data_to_field_keys(self):
        self.client.force_login(self.owner)
        with mock.patch.object(lookup_providers, '_http_get_json', return_value=OPENLIBRARY_PAYLOAD):
            resp = self.client.get(self.url, {'q': '9780132350884'})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'] and data['found'])
        self.assertEqual(data['fields']['titel'], 'Clean Code')
        self.assertEqual(data['fields']['autor'], 'Robert C. Martin')
        self.assertEqual(data['fields']['isbn'], '9780132350884')
        self.assertNotIn('notiz', data['fields'])  # unmapped field untouched

    def test_lookup_requires_provider(self):
        self.coll.lookup_provider = ''
        self.coll.save()
        self.client.force_login(self.owner)
        resp = self.client.get(self.url, {'q': '123'})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()['ok'])

    def test_lookup_requires_query(self):
        self.client.force_login(self.owner)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 400)

    def test_lookup_needs_edit_permission(self):
        CollectionShare.objects.create(collection=self.coll, user=self.viewer, permission='view')
        self.client.force_login(self.viewer)
        resp = self.client.get(self.url, {'q': '9780132350884'})
        self.assertEqual(resp.status_code, 403)

    def test_item_form_exposes_autofill_hook(self):
        self.client.force_login(self.owner)
        resp = self.client.get(reverse('item_create', args=[self.coll.pk]))
        self.assertContains(resp, 'id="autofill"')
        self.assertContains(resp, 'data-query-key="isbn"')

    def test_text_query_field_gets_scan_attribute(self):
        # Even a plain TEXT field mapped to the provider's query attribute must
        # offer camera scanning (scanner.js hooks onto data-scan).
        coll = Collection.objects.create(owner=self.owner, name='B2', lookup_provider='openlibrary')
        add_field(coll, 'code', 'Code', FieldType.TEXT, order=0, config={'lookup_attribute': 'isbn'})
        self.client.force_login(self.owner)
        resp = self.client.get(reverse('item_create', args=[coll.pk]))
        self.assertContains(resp, 'data-scan="isbn"')

    def test_lookup_flags_duplicate_isbn_but_not_the_item_itself(self):
        item = Item.objects.create(collection=self.coll,
                                   values={'name': 'Vorhanden', 'isbn': '978-0-13-235088-4'})
        self.client.force_login(self.owner)
        with mock.patch.object(lookup_providers, '_http_get_json', return_value=OPENLIBRARY_PAYLOAD):
            resp = self.client.get(self.url, {'q': '9780132350884'})
        duplicate = resp.json()['duplicate']
        self.assertEqual(duplicate['name'], 'Vorhanden')  # hyphens vs. digits normalised
        self.assertIn(str(item.pk), duplicate['url'])
        # Editing that very item must not warn about itself.
        with mock.patch.object(lookup_providers, '_http_get_json', return_value=OPENLIBRARY_PAYLOAD):
            resp = self.client.get(self.url, {'q': '9780132350884', 'exclude': str(item.pk)})
        self.assertIsNone(resp.json()['duplicate'])


class SearchViewTests(TestCase):
    """Free-text search: suggest external records and link one to the item."""

    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.viewer = User.objects.create_user('viewer', 'v@e.de', 'pw')
        self.coll = Collection.objects.create(owner=self.owner, name='Bücher',
                                              lookup_provider='openlibrary')
        add_field(self.coll, 'titel', 'Titel', FieldType.TEXT, order=0, config={'lookup_attribute': 'title'})
        add_field(self.coll, 'autor', 'Autor', FieldType.TEXT, order=1, config={'lookup_attribute': 'authors'})
        add_field(self.coll, 'isbn', 'ISBN', FieldType.ISBN, order=2, config={'lookup_attribute': 'isbn'})
        self.url = reverse('item_search', args=[self.coll.pk])
        self.client.force_login(self.owner)

    def test_search_maps_candidates_to_field_keys(self):
        with mock.patch.object(lookup_providers, '_http_get_json',
                               return_value=OPENLIBRARY_SEARCH_PAYLOAD):
            resp = self.client.get(self.url, {'q': 'clean code martin'})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(len(data['results']), 2)
        first = data['results'][0]
        self.assertEqual(first['fields']['titel'], 'Clean Code')
        self.assertEqual(first['fields']['isbn'], '9780132350884')  # link = ISBN filled too
        self.assertIn('Clean Code', first['label'])
        self.assertIn('Robert C. Martin', first['label'])
        self.assertIn('covers.openlibrary.org', first['cover'])

    def test_search_requires_provider_and_query(self):
        self.assertEqual(self.client.get(self.url).status_code, 400)
        self.coll.lookup_provider = ''
        self.coll.save()
        self.assertEqual(self.client.get(self.url, {'q': 'x'}).status_code, 400)

    def test_search_needs_edit_permission(self):
        CollectionShare.objects.create(collection=self.coll, user=self.viewer, permission='view')
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(self.url, {'q': 'x'}).status_code, 403)

    def test_item_form_exposes_search_hook(self):
        resp = self.client.get(reverse('item_create', args=[self.coll.pk]))
        self.assertContains(resp, 'data-search-url="%s"' % self.url)


class FindTests(TestCase):
    """Scan-to-find: a scanned code opens the matching item."""

    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Bücher')
        add_field(self.col, 'isbn', 'ISBN', FieldType.ISBN, order=0)
        self.item = Item.objects.create(collection=self.col,
                                        values={'name': 'Buch', 'isbn': '978-3-16-148410-0'})
        self.client.force_login(self.owner)
        self.url = reverse('item_find', args=[self.col.pk])
        self.detail = reverse('item_detail', args=[self.col.pk, self.item.pk])

    def test_find_by_label_barcode_short_code(self):
        resp = self.client.get(self.url, {'code': self.item.id.hex[:12]})
        self.assertRedirects(resp, self.detail)

    def test_find_by_item_qr_url(self):
        resp = self.client.get(self.url, {'code': 'http://testserver' + self.detail})
        self.assertRedirects(resp, self.detail)

    def test_find_by_isbn_value_normalises_hyphens(self):
        resp = self.client.get(self.url, {'code': '9783161484100'})
        self.assertRedirects(resp, self.detail)

    def test_unknown_code_shows_warning(self):
        resp = self.client.get(self.url, {'code': 'gibtsnicht'}, follow=True)
        self.assertContains(resp, 'Kein Gegenstand mit Code')

    def test_detail_page_has_scan_button(self):
        resp = self.client.get(reverse('collection_detail', args=[self.col.pk]))
        self.assertContains(resp, 'id="scanFind"')
        self.assertContains(resp, 'js/scanner.js')

    def test_find_requires_access(self):
        stranger = User.objects.create_user('fremd', 'f@e.de', 'pw')
        self.client.force_login(stranger)
        resp = self.client.get(self.url, {'code': 'x'})
        self.assertEqual(resp.status_code, 403)


class CollectionSettingsTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.coll = Collection.objects.create(owner=self.owner, name='S')
        self.client.force_login(self.owner)

    def test_set_and_clear_provider(self):
        url = reverse('collection_settings', args=[self.coll.pk])
        self.client.post(url, {'lookup_provider': 'openlibrary'})
        self.coll.refresh_from_db()
        self.assertEqual(self.coll.lookup_provider, 'openlibrary')
        self.client.post(url, {'lookup_provider': ''})
        self.coll.refresh_from_db()
        self.assertEqual(self.coll.lookup_provider, '')

    def test_unknown_provider_rejected(self):
        self.client.post(reverse('collection_settings', args=[self.coll.pk]), {'lookup_provider': 'nope'})
        self.coll.refresh_from_db()
        self.assertEqual(self.coll.lookup_provider, '')


class BookPresetTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.client.force_login(self.owner)

    def test_create_with_book_preset_sets_provider_and_mapping(self):
        self.client.post(reverse('collection_create'),
                         {'name': 'Meine Bücher', 'description': '', 'preset': 'books'})
        coll = Collection.objects.get(name='Meine Bücher')
        self.assertEqual(coll.lookup_provider, 'openlibrary')
        isbn = coll.fields.get(key='isbn')
        self.assertEqual(isbn.config.get('lookup_attribute'), 'isbn')
        self.assertEqual(coll.fields.get(key='titel').config.get('lookup_attribute'), 'title')


# --- Internationalisation (German default + English) --------------------------

class I18nTests(TestCase):
    def test_catalogue_translates_strings_to_english(self):
        from django.utils import translation
        with translation.override('en'):
            self.assertEqual(translation.gettext('Sammlungen'), 'Collections')
            self.assertEqual(translation.gettext('Speichern'), 'Save')
        with translation.override('de'):
            # German is the source language: msgid is returned unchanged.
            self.assertEqual(translation.gettext('Sammlungen'), 'Sammlungen')

    def test_login_page_renders_in_german_by_default(self):
        resp = self.client.get(reverse('login'))
        self.assertContains(resp, 'Anmelden')

    def test_set_language_switches_ui_to_english(self):
        # Persist English via the set_language endpoint, then a page renders EN.
        resp = self.client.post(reverse('set_language'),
                                {'language': 'en', 'next': reverse('login')},
                                follow=True)
        self.assertContains(resp, 'Sign in')
        self.assertNotContains(resp, 'Registrieren')

    def test_language_switcher_present_in_page(self):
        resp = self.client.get(reverse('login'))
        self.assertContains(resp, reverse('set_language'))


class LoanTests(TestCase):
    """Lending: mark items as lent, return them, list open loans."""

    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.viewer = User.objects.create_user('viewer', 'v@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Bücher')
        self.item = Item.objects.create(collection=self.col, values={'name': 'Dune'})
        CollectionShare.objects.create(collection=self.col, user=self.viewer, permission='view')
        self.client.force_login(self.owner)
        self.lend_url = reverse('item_lend', args=[self.col.pk, self.item.pk])
        self.return_url = reverse('item_return', args=[self.col.pk, self.item.pk])

    def test_lend_and_return(self):
        resp = self.client.post(self.lend_url, {'borrower': 'Anna', 'note': 'bis August'})
        self.assertEqual(resp.status_code, 302)
        loan = self.item.active_loan
        self.assertEqual(loan.borrower, 'Anna')
        self.assertIsNone(loan.returned_at)
        self.client.post(self.return_url)
        loan.refresh_from_db()
        self.assertIsNotNone(loan.returned_at)
        self.assertIsNone(self.item.active_loan)

    def test_double_lend_blocked(self):
        self.client.post(self.lend_url, {'borrower': 'Anna'})
        self.client.post(self.lend_url, {'borrower': 'Bernd'})
        self.assertEqual(self.item.loans.count(), 1)

    def test_borrower_required(self):
        self.client.post(self.lend_url, {'borrower': '  '})
        self.assertEqual(self.item.loans.count(), 0)

    def test_viewer_cannot_lend_but_can_see_list(self):
        self.client.force_login(self.viewer)
        resp = self.client.post(self.lend_url, {'borrower': 'X'})
        self.assertEqual(resp.status_code, 403)
        Loan.objects.create(item=self.item, borrower='Anna')
        resp = self.client.get(reverse('collection_loans', args=[self.col.pk]))
        self.assertContains(resp, 'Anna')
        self.assertNotContains(resp, 'Rückgabe vermerken')

    def test_item_detail_shows_loan_state(self):
        Loan.objects.create(item=self.item, borrower='Anna')
        resp = self.client.get(reverse('item_detail', args=[self.col.pk, self.item.pk]))
        self.assertContains(resp, 'Verliehen an')
        self.assertContains(resp, 'Anna')

    def test_detail_toolbar_shows_open_loan_badge(self):
        Loan.objects.create(item=self.item, borrower='Anna')
        resp = self.client.get(reverse('collection_detail', args=[self.col.pk]))
        self.assertContains(resp, 'Ausleihen')
        self.assertContains(resp, 'text-bg-warning">1</span>')

    def test_dashboard_lists_open_loans_and_flags_overdue(self):
        from datetime import timedelta
        from django.utils import timezone
        Loan.objects.create(item=self.item, borrower='Anna',
                            lent_at=timezone.localdate() - timedelta(days=40))
        resp = self.client.get(reverse('dashboard'))
        self.assertContains(resp, 'Offene Ausleihen')
        self.assertContains(resp, 'Anna')
        self.assertContains(resp, 'überfällig')
        # returned loans disappear from the dashboard
        Loan.objects.update(returned_at=timezone.localdate())
        resp = self.client.get(reverse('dashboard'))
        self.assertNotContains(resp, 'Offene Ausleihen')


class ImportTests(TestCase):
    """Excel/CSV import: headers map to field labels, 'Art' creates ItemTypes."""

    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.viewer = User.objects.create_user('viewer', 'v@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Filme')
        add_field(self.col, 'name', 'Name', FieldType.TEXT, order=0)
        add_field(self.col, 'preis', 'Preis', FieldType.PRICE, order=1)
        add_field(self.col, 'gesehen', 'Gesehen', FieldType.BOOLEAN, order=2)
        add_field(self.col, 'kaufdatum', 'Kaufdatum', FieldType.DATE, order=3)
        add_field(self.col, 'genre', 'Genre', FieldType.CHOICE, order=4,
                  config={'choices': ['Action', 'Drama']})
        CollectionShare.objects.create(collection=self.col, user=self.viewer, permission='view')
        self.client.force_login(self.owner)
        self.url = reverse('collection_import', args=[self.col.pk])

    def _xlsx(self, rows) -> SimpleUploadedFile:
        from openpyxl import Workbook
        wb = Workbook()
        for row in rows:
            wb.active.append(row)
        buf = io.BytesIO()
        wb.save(buf)
        return SimpleUploadedFile('daten.xlsx', buf.getvalue())

    def test_import_xlsx_creates_items_and_art(self):
        upload = self._xlsx([
            ['Art', 'Name', 'Preis', 'Gesehen', 'Kaufdatum', 'Genre', 'Unbekannt'],
            ['DVD', 'Matrix', '9,99', 'Ja', '01.05.2024', 'action', 'x'],
            ['Blu-ray', 'Dune', 19.5, 'Nein', '2024-06-01', 'Drama', ''],
        ])
        resp = self.client.post(self.url, {'file': upload})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.col.items.count(), 2)
        matrix = self.col.items.get(values__name='Matrix')
        self.assertEqual(matrix.item_type.name, 'DVD')
        self.assertEqual(matrix.values['preis'], 9.99)
        self.assertIs(matrix.values['gesehen'], True)
        self.assertEqual(matrix.values['kaufdatum'], '2024-05-01')
        self.assertEqual(matrix.values['genre'], 'Action')  # case-insensitive match
        self.assertContains(resp, 'Unbekannt')  # reported as ignored column

    def test_import_csv_with_semicolon(self):
        upload = SimpleUploadedFile(
            'daten.csv', 'Name;Preis\nMatrix;9,99\nDune;19.5\n'.encode('utf-8-sig'))
        resp = self.client.post(self.url, {'file': upload})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.col.items.count(), 2)

    def test_invalid_cells_are_skipped_with_warning(self):
        result = imports.import_table(self.col, [
            ['Name', 'Preis'],
            ['Matrix', 'teuer'],
        ])
        self.assertEqual(result['created'], 1)  # row imported, bad cell dropped
        self.assertEqual(len(result['warnings']), 1)
        self.assertNotIn('preis', self.col.items.get().values)

    def test_import_requires_edit_permission(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_wrong_file_rejected(self):
        resp = self.client.post(self.url, {'file': SimpleUploadedFile('x.txt', b'nope')},
                                follow=True)
        self.assertContains(resp, '.xlsx- oder .csv-Datei')
        self.assertEqual(self.col.items.count(), 0)


class PwaTests(TestCase):
    def test_service_worker_served_from_root(self):
        resp = self.client.get('/sw.js')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/javascript')
        self.assertContains(resp, "caches.open")

    def test_manifest_served(self):
        resp = self.client.get('/manifest.webmanifest')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/manifest+json')
        data = json.loads(resp.content)
        self.assertEqual(data['short_name'], 'CMS')
        self.assertEqual(len(data['icons']), 2)

    def test_base_template_links_manifest(self):
        resp = self.client.get(reverse('login'))
        self.assertContains(resp, 'rel="manifest"')


class CollectionEditDeleteTests(TestCase):
    """Renaming/describing and deleting whole collections."""

    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.editor = User.objects.create_user('editor', 'e@e.de', 'pw')
        self.viewer = User.objects.create_user('viewer', 'v@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Alt', description='x')
        CollectionShare.objects.create(collection=self.col, user=self.editor, permission='edit')
        CollectionShare.objects.create(collection=self.col, user=self.viewer, permission='view')
        self.edit_url = reverse('collection_edit', args=[self.col.pk])
        self.delete_url = reverse('collection_delete', args=[self.col.pk])

    def test_owner_can_rename(self):
        self.client.force_login(self.owner)
        resp = self.client.post(self.edit_url, {'name': 'Neu', 'description': 'y'})
        self.assertRedirects(resp, reverse('collection_detail', args=[self.col.pk]))
        self.col.refresh_from_db()
        self.assertEqual(self.col.name, 'Neu')
        self.assertEqual(self.col.description, 'y')

    def test_edit_form_hides_preset_and_template(self):
        self.client.force_login(self.owner)
        resp = self.client.get(self.edit_url)
        self.assertNotContains(resp, 'name="preset"')
        self.assertNotContains(resp, 'name="template"')

    def test_editor_can_rename_viewer_cannot(self):
        self.client.force_login(self.editor)
        self.client.post(self.edit_url, {'name': 'Vom Editor', 'description': ''})
        self.col.refresh_from_db()
        self.assertEqual(self.col.name, 'Vom Editor')
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(self.edit_url).status_code, 403)

    def test_only_owner_can_delete(self):
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(self.delete_url).status_code, 403)
        self.client.force_login(self.owner)
        resp = self.client.post(self.delete_url)
        self.assertRedirects(resp, reverse('dashboard'))
        self.assertFalse(Collection.objects.filter(pk=self.col.pk).exists())

    def test_delete_shows_confirmation_first(self):
        self.client.force_login(self.owner)
        resp = self.client.get(self.delete_url)
        self.assertContains(resp, 'löschen')
        self.assertTrue(Collection.objects.filter(pk=self.col.pk).exists())


class SortPaginationTests(TestCase):
    """Sortable columns and pagination of the item table."""

    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Bücher')
        add_field(self.col, 'name', 'Name', FieldType.TEXT, order=0)
        self.client.force_login(self.owner)
        self.url = reverse('collection_detail', args=[self.col.pk])

    def test_sort_by_field_asc_and_desc(self):
        for name in ('Charlie', 'Alpha', 'Bravo'):
            Item.objects.create(collection=self.col, values={'name': name})
        resp = self.client.get(self.url, {'sort': 'name', 'dir': 'asc'})
        names = [r['item'].values['name'] for r in resp.context['rows']]
        self.assertEqual(names, ['Alpha', 'Bravo', 'Charlie'])
        resp = self.client.get(self.url, {'sort': 'name', 'dir': 'desc'})
        names = [r['item'].values['name'] for r in resp.context['rows']]
        self.assertEqual(names, ['Charlie', 'Bravo', 'Alpha'])

    def test_unknown_sort_key_is_ignored(self):
        Item.objects.create(collection=self.col, values={'name': 'A'})
        resp = self.client.get(self.url, {'sort': 'nope"; drop', 'dir': 'desc'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context['rows']), 1)

    def test_items_are_paginated(self):
        from .runtime_settings import get_setting
        per_page = get_setting('items_per_page')
        for i in range(per_page + 5):
            Item.objects.create(collection=self.col, values={'name': f'Item {i:03d}'})
        resp = self.client.get(self.url)
        self.assertEqual(len(resp.context['rows']), per_page)
        self.assertContains(resp, 'pagination')
        resp = self.client.get(self.url, {'page': 2})
        self.assertEqual(len(resp.context['rows']), 5)

    def test_page_size_is_configurable_at_runtime(self):
        from django.core.cache import cache
        from .runtime_settings import _CACHE_KEY, set_setting
        # The settings cache outlives the test transaction: clean it up so the
        # rolled-back override can't leak into other tests.
        self.addCleanup(cache.delete, _CACHE_KEY)
        set_setting('items_per_page', 5)
        for i in range(7):
            Item.objects.create(collection=self.col, values={'name': f'Item {i:03d}'})
        resp = self.client.get(self.url)
        self.assertEqual(len(resp.context['rows']), 5)

    def test_pagination_keeps_filters(self):
        for i in range(60):
            Item.objects.create(collection=self.col, values={'name': f'Buch {i:03d}'})
        resp = self.client.get(self.url, {'q': 'Buch', 'page': 2})
        self.assertEqual(resp.context['result_count'], 60)
        self.assertEqual(len(resp.context['rows']), 10)


@override_settings(MEDIA_ROOT=MEDIA)
class ItemDuplicateTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.viewer = User.objects.create_user('viewer', 'v@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Bücher')
        add_field(self.col, 'name', 'Name', FieldType.TEXT, order=0)
        add_field(self.col, 'bild', 'Bild', FieldType.IMAGE, order=1)
        CollectionShare.objects.create(collection=self.col, user=self.viewer, permission='view')
        self.client.force_login(self.owner)

    def test_duplicate_copies_values_type_and_assets(self):
        art = ItemType.objects.create(collection=self.col, name='Roman')
        resp = self.client.post(reverse('item_create', args=[self.col.pk]), {
            '__item_type': art.pk, 'name': 'Dune',
            'bild': SimpleUploadedFile('cover.png', make_png(), content_type='image/png'),
        })
        self.assertEqual(resp.status_code, 302)
        original = self.col.items.get()
        resp = self.client.post(reverse('item_duplicate', args=[self.col.pk, original.pk]))
        self.assertEqual(self.col.items.count(), 2)
        copy = self.col.items.exclude(pk=original.pk).get()
        self.assertRedirects(resp, reverse('item_edit', args=[self.col.pk, copy.pk]))
        self.assertEqual(copy.values['name'], 'Dune')
        self.assertEqual(copy.item_type, art)
        self.assertEqual(copy.assets.count(), 1)
        # The copy owns its own file — not a reference to the original's asset.
        self.assertNotEqual(copy.assets.get().file.name, original.assets.get().file.name)
        self.assertEqual(copy.values['bild']['asset_id'], str(copy.assets.get().id))

    def test_viewer_cannot_duplicate(self):
        item = Item.objects.create(collection=self.col, values={'name': 'Dune'})
        self.client.force_login(self.viewer)
        resp = self.client.post(reverse('item_duplicate', args=[self.col.pk, item.pk]))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.col.items.count(), 1)


class LoanDueDateTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Bücher')
        self.item = Item.objects.create(collection=self.col, values={'name': 'Dune'})
        self.client.force_login(self.owner)
        self.lend_url = reverse('item_lend', args=[self.col.pk, self.item.pk])

    def test_lend_with_due_date(self):
        self.client.post(self.lend_url, {'borrower': 'Anna', 'due_at': '2026-08-01'})
        loan = self.item.active_loan
        self.assertEqual(str(loan.due_at), '2026-08-01')

    def test_invalid_due_date_is_ignored(self):
        self.client.post(self.lend_url, {'borrower': 'Anna', 'due_at': 'kein-datum'})
        loan = self.item.active_loan
        self.assertIsNotNone(loan)
        self.assertIsNone(loan.due_at)

    def test_overdue_uses_due_date(self):
        from datetime import timedelta
        from django.utils import timezone
        today = timezone.localdate()
        loan = Loan.objects.create(item=self.item, borrower='Anna',
                                   due_at=today - timedelta(days=1))
        self.assertTrue(loan.is_overdue)
        loan.due_at = today + timedelta(days=1)
        self.assertFalse(loan.is_overdue)
        # Returned loans are never overdue.
        loan.due_at = today - timedelta(days=1)
        loan.returned_at = today
        self.assertFalse(loan.is_overdue)


class GlobalSearchTests(TestCase):
    """The navbar search spans all accessible collections and their items."""

    def setUp(self):
        self.user = User.objects.create_user('anna', 'a@e.de', 'pw')
        self.other = User.objects.create_user('bernd', 'b@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.user, name='Science-Fiction',
                                             description='Romane und Filme')
        add_field(self.col, 'name', 'Name', FieldType.TEXT, order=0)
        self.item = Item.objects.create(collection=self.col, values={'name': 'Dune'})
        # A foreign collection that must never appear in the results.
        foreign = Collection.objects.create(owner=self.other, name='Dune-Sammlung')
        add_field(foreign, 'name', 'Name', FieldType.TEXT, order=0)
        Item.objects.create(collection=foreign, values={'name': 'Dune Geheim'})
        self.client.force_login(self.user)
        self.url = reverse('global_search')

    def test_requires_login(self):
        self.client.logout()
        resp = self.client.get(self.url, {'q': 'Dune'})
        self.assertEqual(resp.status_code, 302)

    def test_finds_collections_by_name_and_description(self):
        resp = self.client.get(self.url, {'q': 'science'})
        self.assertEqual([c.pk for c in resp.context['collection_hits']], [self.col.pk])
        resp = self.client.get(self.url, {'q': 'Romane'})
        self.assertEqual([c.pk for c in resp.context['collection_hits']], [self.col.pk])

    def test_finds_items_only_in_accessible_collections(self):
        resp = self.client.get(self.url, {'q': 'dune'})
        self.assertEqual([i.pk for i in resp.context['item_hits']], [self.item.pk])
        self.assertNotContains(resp, 'Dune Geheim')

    def test_finds_items_by_type_name(self):
        art = ItemType.objects.create(collection=self.col, name='Taschenbuch')
        self.item.item_type = art
        self.item.save()
        resp = self.client.get(self.url, {'q': 'taschenbuch'})
        self.assertEqual([i.pk for i in resp.context['item_hits']], [self.item.pk])

    def test_shared_collections_are_searched(self):
        self.client.force_login(self.other)
        CollectionShare.objects.create(collection=self.col, user=self.other, permission='view')
        resp = self.client.get(self.url, {'q': 'dune'})
        self.assertIn(self.item.pk, [i.pk for i in resp.context['item_hits']])

    def test_empty_query_shows_hint(self):
        resp = self.client.get(self.url)
        self.assertContains(resp, 'Suchbegriff')

    def test_navbar_contains_search_form(self):
        resp = self.client.get(reverse('dashboard'))
        self.assertContains(resp, reverse('global_search'))


class BulkActionTests(TestCase):
    """Bulk delete / bulk "Art" assignment from the item table."""

    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.viewer = User.objects.create_user('viewer', 'v@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Bücher')
        add_field(self.col, 'name', 'Name', FieldType.TEXT, order=0)
        CollectionShare.objects.create(collection=self.col, user=self.viewer, permission='view')
        self.items = [Item.objects.create(collection=self.col, values={'name': f'Buch {i}'})
                      for i in range(3)]
        self.client.force_login(self.owner)
        self.url = reverse('items_bulk', args=[self.col.pk])

    def test_bulk_delete(self):
        resp = self.client.post(self.url, {
            'action': 'delete',
            'items': [str(self.items[0].pk), str(self.items[1].pk)],
        })
        self.assertRedirects(resp, reverse('collection_detail', args=[self.col.pk]))
        self.assertEqual(self.col.items.count(), 1)

    def test_bulk_set_and_clear_type(self):
        art = ItemType.objects.create(collection=self.col, name='Roman')
        self.client.post(self.url, {
            'action': 'set_type', 'item_type': str(art.pk),
            'items': [str(i.pk) for i in self.items],
        })
        self.assertEqual(self.col.items.filter(item_type=art).count(), 3)
        self.client.post(self.url, {
            'action': 'set_type', 'item_type': '',
            'items': [str(self.items[0].pk)],
        })
        self.assertEqual(self.col.items.filter(item_type=art).count(), 2)

    def test_foreign_items_and_invalid_ids_are_ignored(self):
        foreign_col = Collection.objects.create(owner=self.viewer, name='Fremd')
        foreign = Item.objects.create(collection=foreign_col, values={})
        self.client.post(self.url, {
            'action': 'delete',
            'items': [str(foreign.pk), 'kein-uuid'],
        })
        self.assertTrue(Collection.objects.get(pk=foreign_col.pk).items.filter(pk=foreign.pk).exists())
        self.assertEqual(self.col.items.count(), 3)

    def test_type_from_other_collection_rejected(self):
        foreign_col = Collection.objects.create(owner=self.owner, name='Andere')
        foreign_art = ItemType.objects.create(collection=foreign_col, name='Fremd')
        resp = self.client.post(self.url, {
            'action': 'set_type', 'item_type': str(foreign_art.pk),
            'items': [str(self.items[0].pk)],
        })
        self.assertEqual(resp.status_code, 404)
        self.assertIsNone(self.col.items.get(pk=self.items[0].pk).item_type)

    def test_viewer_cannot_bulk_edit(self):
        self.client.force_login(self.viewer)
        resp = self.client.post(self.url, {'action': 'delete',
                                           'items': [str(self.items[0].pk)]})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.col.items.count(), 3)

    def test_checkboxes_only_rendered_for_editors(self):
        resp = self.client.get(reverse('collection_detail', args=[self.col.pk]))
        self.assertContains(resp, 'data-bulk-item')
        self.client.force_login(self.viewer)
        resp = self.client.get(reverse('collection_detail', args=[self.col.pk]))
        self.assertNotContains(resp, 'data-bulk-item')


class RuntimeSettingsTests(TestCase):
    """Database-backed runtime settings: precedence, validation, caching."""

    def setUp(self):
        from django.core.cache import cache
        from . import runtime_settings
        self.rs = runtime_settings
        cache.delete(runtime_settings._CACHE_KEY)
        self.addCleanup(cache.delete, runtime_settings._CACHE_KEY)

    def test_code_default_without_any_override(self):
        self.assertEqual(self.rs.get_setting('items_per_page'), 50)
        self.assertFalse(self.rs.get_setting('registration_auto_approve'))

    def test_db_override_wins(self):
        self.rs.set_setting('items_per_page', 25)
        self.assertEqual(self.rs.get_setting('items_per_page'), 25)

    def test_ini_fallback_used_when_no_db_row(self):
        with mock.patch.dict('cms.conf._INI_VALUES', {'ITEMS_PER_PAGE': '75'}):
            self.assertEqual(self.rs.get_setting('items_per_page'), 75)

    def test_db_override_beats_ini(self):
        self.rs.set_setting('items_per_page', 25)
        with mock.patch.dict('cms.conf._INI_VALUES', {'ITEMS_PER_PAGE': '75'}):
            self.assertEqual(self.rs.get_setting('items_per_page'), 25)

    def test_set_setting_validates(self):
        with self.assertRaises(ValueError):
            self.rs.set_setting('items_per_page', 1)  # below min_value=5
        with self.assertRaises(ValueError):
            self.rs.set_setting('items_per_page', 'keine Zahl')

    def test_bool_and_str_coercion(self):
        self.rs.set_setting('registration_auto_approve', 'true')
        self.assertIs(self.rs.get_setting('registration_auto_approve'), True)
        self.rs.set_setting('default_currency', 'USD')
        self.assertEqual(self.rs.get_setting('default_currency'), 'USD')

    def test_corrupt_db_value_falls_back_to_default(self):
        from .models import SiteSetting
        SiteSetting.objects.create(key='items_per_page', value='kaputt')
        self.assertEqual(self.rs.get_setting('items_per_page'), 50)

    def test_default_currency_applies_to_new_collections(self):
        self.rs.set_setting('default_currency', 'CHF')
        owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        col = Collection.objects.create(owner=owner, name='Münzen')
        create_default_fields(col)
        preis = col.fields.get(key='preis')
        self.assertEqual(preis.config.get('currency'), 'CHF')

    def test_loan_overdue_days_configurable(self):
        from datetime import timedelta
        from django.utils import timezone
        owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        col = Collection.objects.create(owner=owner, name='Bücher')
        item = Item.objects.create(collection=col, values={'name': 'Dune'})
        loan = Loan.objects.create(
            item=item, borrower='Anna',
            lent_at=timezone.localdate() - timedelta(days=10),
        )
        self.assertFalse(loan.is_overdue)  # default: 30 days
        self.rs.set_setting('loan_overdue_days', 7)
        self.assertTrue(loan.is_overdue)


class SiteSettingsPageTests(TestCase):
    """The staff-only settings page (form generated from the registry)."""

    def setUp(self):
        from django.core.cache import cache
        from . import runtime_settings
        cache.delete(runtime_settings._CACHE_KEY)
        self.addCleanup(cache.delete, runtime_settings._CACHE_KEY)
        self.staff = User.objects.create_user('chef', 'c@e.de', 'pw', is_staff=True)
        self.user = User.objects.create_user('normal', 'n@e.de', 'pw')
        self.url = reverse('site_settings')

    def test_requires_staff(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('login'), resp['Location'])

    def test_staff_can_view_and_save(self):
        from . import runtime_settings
        self.client.force_login(self.staff)
        resp = self.client.get(self.url)
        self.assertContains(resp, 'items_per_page')

        data = {key: runtime_settings.get_setting(key)
                for key, d in runtime_settings.REGISTRY.items() if d.kind != 'bool'}
        data['items_per_page'] = 20
        data['registration_auto_approve'] = 'on'
        resp = self.client.post(self.url, data)
        self.assertRedirects(resp, self.url)
        self.assertEqual(runtime_settings.get_setting('items_per_page'), 20)
        self.assertTrue(runtime_settings.get_setting('registration_auto_approve'))

    def test_invalid_value_rejected(self):
        from . import runtime_settings
        self.client.force_login(self.staff)
        data = {key: runtime_settings.get_setting(key)
                for key, d in runtime_settings.REGISTRY.items() if d.kind != 'bool'}
        data['items_per_page'] = 100000  # above max_value
        resp = self.client.post(self.url, data)
        self.assertEqual(resp.status_code, 200)  # re-rendered with errors
        self.assertEqual(runtime_settings.get_setting('items_per_page'), 50)

    def test_nav_link_only_for_staff(self):
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse('dashboard')), self.url)
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(reverse('dashboard')), self.url)


@override_settings(MEDIA_ROOT=MEDIA)
class UploadLimitTests(TestCase):
    """Runtime-configurable upload limits (size and allowed extensions)."""

    def setUp(self):
        from django.core.cache import cache
        from . import runtime_settings
        self.rs = runtime_settings
        cache.delete(runtime_settings._CACHE_KEY)
        self.addCleanup(cache.delete, runtime_settings._CACHE_KEY)
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Dokumente')
        add_field(self.col, 'name', 'Name', FieldType.TEXT, order=0)
        add_field(self.col, 'beleg', 'Beleg', FieldType.FILE, order=1)
        self.client.force_login(self.owner)
        self.url = reverse('item_create', args=[self.col.pk])

    def test_oversized_file_rejected(self):
        self.rs.set_setting('upload_max_mb', 1)
        big = SimpleUploadedFile('beleg.pdf', b'x' * (1024 * 1024 + 1))
        resp = self.client.post(self.url, {'name': 'Quittung', 'beleg': big})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'zu groß')
        self.assertEqual(self.col.items.count(), 0)

    def test_file_within_limit_accepted(self):
        self.rs.set_setting('upload_max_mb', 1)
        small = SimpleUploadedFile('beleg.pdf', b'x' * 1024)
        resp = self.client.post(self.url, {'name': 'Quittung', 'beleg': small})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.col.items.count(), 1)

    def test_disallowed_extension_rejected(self):
        self.rs.set_setting('upload_allowed_extensions', 'pdf, png')
        exe = SimpleUploadedFile('virus.exe', b'MZ...')
        resp = self.client.post(self.url, {'name': 'Böse', 'beleg': exe})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'nicht erlaubt')
        self.assertEqual(self.col.items.count(), 0)

    def test_allowed_extension_accepted(self):
        self.rs.set_setting('upload_allowed_extensions', '.PDF')  # dot/case normalised
        pdf = SimpleUploadedFile('beleg.pdf', b'%PDF-1.4')
        resp = self.client.post(self.url, {'name': 'Quittung', 'beleg': pdf})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.col.items.count(), 1)

    def test_empty_extension_list_allows_everything(self):
        anything = SimpleUploadedFile('daten.xyz', b'ok')
        resp = self.client.post(self.url, {'name': 'Frei', 'beleg': anything})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.col.items.count(), 1)


class SiteSettingAuditTests(TestCase):
    """Saved settings record who changed them (``updated_by``)."""

    def setUp(self):
        from django.core.cache import cache
        from . import runtime_settings
        cache.delete(runtime_settings._CACHE_KEY)
        self.addCleanup(cache.delete, runtime_settings._CACHE_KEY)

    def test_settings_page_records_editor(self):
        from . import runtime_settings
        from .models import SiteSetting
        staff = User.objects.create_user('chef', 'c@e.de', 'pw', is_staff=True)
        self.client.force_login(staff)
        data = {key: runtime_settings.get_setting(key)
                for key, d in runtime_settings.REGISTRY.items() if d.kind != 'bool'}
        data['registration_enabled'] = 'on'
        data['items_per_page'] = 30
        resp = self.client.post(reverse('site_settings'), data)
        self.assertRedirects(resp, reverse('site_settings'))
        row = SiteSetting.objects.get(key='items_per_page')
        self.assertEqual(row.updated_by, staff)


class AnnouncementBannerTests(TestCase):
    """Site-wide announcement banner driven by runtime settings."""

    def setUp(self):
        from django.core.cache import cache
        from . import runtime_settings
        self.rs = runtime_settings
        cache.delete(runtime_settings._CACHE_KEY)
        self.addCleanup(cache.delete, runtime_settings._CACHE_KEY)
        self.user = User.objects.create_user('u', 'u@e.de', 'pw')
        self.client.force_login(self.user)

    def test_no_banner_by_default(self):
        resp = self.client.get(reverse('dashboard'))
        self.assertNotContains(resp, 'bi-megaphone')

    def test_banner_shown_with_configured_style(self):
        self.rs.set_setting('announcement_text', 'Wartung am Sonntag!')
        self.rs.set_setting('announcement_style', 'danger')
        resp = self.client.get(reverse('dashboard'))
        self.assertContains(resp, 'Wartung am Sonntag!')
        self.assertContains(resp, 'alert-danger')

    def test_banner_also_on_login_page(self):
        self.rs.set_setting('announcement_text', 'Wartung am Sonntag!')
        self.client.logout()
        self.assertContains(self.client.get(reverse('login')), 'Wartung am Sonntag!')

    def test_invalid_style_rejected(self):
        with self.assertRaises(ValueError):
            self.rs.set_setting('announcement_style', 'evil"><script>')


class SettingsExportTests(TestCase):
    """INI export of the effective runtime settings (staff only)."""

    def setUp(self):
        from django.core.cache import cache
        from . import runtime_settings
        cache.delete(runtime_settings._CACHE_KEY)
        self.addCleanup(cache.delete, runtime_settings._CACHE_KEY)
        self.staff = User.objects.create_user('chef', 'c@e.de', 'pw', is_staff=True)
        self.url = reverse('site_settings_export')

    def test_requires_staff(self):
        self.client.force_login(User.objects.create_user('normal', 'n@e.de', 'pw'))
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_export_is_valid_ini_with_current_values(self):
        import configparser
        from . import runtime_settings
        runtime_settings.set_setting('items_per_page', 33)
        self.client.force_login(self.staff)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn('attachment', resp['Content-Disposition'])
        parser = configparser.ConfigParser()
        parser.read_string(resp.content.decode())
        self.assertEqual(parser['app-defaults']['items_per_page'], '33')
        self.assertIn('registration_enabled', parser['app-defaults'])

    def test_export_round_trips_through_conf_layer(self):
        """The exported values, fed back through the INI layer, resolve identically."""
        from . import runtime_settings
        runtime_settings.set_setting('items_per_page', 44)
        runtime_settings.set_setting('registration_auto_approve', True)
        self.client.force_login(self.staff)
        body = self.client.get(self.url).content.decode()

        import configparser
        parser = configparser.ConfigParser()
        parser.read_string(body)
        ini_values = {k.upper(): v for k, v in parser.items('app-defaults')}
        from .models import SiteSetting
        SiteSetting.objects.all().delete()
        from django.core.cache import cache
        cache.delete(runtime_settings._CACHE_KEY)
        with mock.patch.dict('cms.conf._INI_VALUES', ini_values):
            self.assertEqual(runtime_settings.get_setting('items_per_page'), 44)
            self.assertIs(runtime_settings.get_setting('registration_auto_approve'), True)


class MaintenanceModeTests(TestCase):
    """Runtime-toggleable maintenance mode (middleware)."""

    def setUp(self):
        from django.core.cache import cache
        from . import runtime_settings
        self.rs = runtime_settings
        cache.delete(runtime_settings._CACHE_KEY)
        self.addCleanup(cache.delete, runtime_settings._CACHE_KEY)
        self.user = User.objects.create_user('u', 'u@e.de', 'pw')
        self.staff = User.objects.create_user('chef', 'c@e.de', 'pw', is_staff=True)

    def test_off_by_default(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)

    def test_non_staff_sees_503_maintenance_page(self):
        self.rs.set_setting('maintenance_mode', True)
        self.client.force_login(self.user)
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.status_code, 503)
        self.assertContains(resp, 'Wartungsarbeiten', status_code=503)

    def test_staff_keeps_access_and_sees_notice(self):
        self.rs.set_setting('maintenance_mode', True)
        self.client.force_login(self.staff)
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Wartungsmodus aktiv')

    def test_login_stays_reachable(self):
        self.rs.set_setting('maintenance_mode', True)
        self.assertEqual(self.client.get(reverse('login')).status_code, 200)

    def test_register_page_is_blocked(self):
        self.rs.set_setting('maintenance_mode', True)
        self.assertEqual(self.client.get(reverse('register')).status_code, 503)

    def test_anonymous_gets_maintenance_page(self):
        self.rs.set_setting('maintenance_mode', True)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 503)


class SettingChangeHistoryTests(TestCase):
    """Audit history of runtime-setting changes."""

    def setUp(self):
        from django.core.cache import cache
        from . import runtime_settings
        self.rs = runtime_settings
        cache.delete(runtime_settings._CACHE_KEY)
        self.addCleanup(cache.delete, runtime_settings._CACHE_KEY)
        self.staff = User.objects.create_user('chef', 'c@e.de', 'pw', is_staff=True)

    def test_change_is_recorded_with_old_and_new_value(self):
        from .models import SettingChange
        self.rs.set_setting('items_per_page', 25, user=self.staff)
        change = SettingChange.objects.get()
        self.assertEqual((change.key, change.old_value, change.new_value),
                         ('items_per_page', 50, 25))
        self.assertEqual(change.changed_by, self.staff)

    def test_noop_save_is_not_recorded(self):
        from .models import SettingChange
        self.rs.set_setting('items_per_page', 50)  # equals the default
        self.assertEqual(SettingChange.objects.count(), 0)

    def test_settings_page_shows_history(self):
        self.rs.set_setting('items_per_page', 25, user=self.staff)
        self.client.force_login(self.staff)
        resp = self.client.get(reverse('site_settings'))
        self.assertContains(resp, 'Letzte Änderungen')
        self.assertContains(resp, '<code>50</code>', html=True)
        self.assertContains(resp, '<code>25</code>', html=True)

    def test_form_save_records_only_actual_changes(self):
        from . import runtime_settings
        from .models import SettingChange
        self.client.force_login(self.staff)
        data = {key: runtime_settings.get_setting(key)
                for key, d in runtime_settings.REGISTRY.items() if d.kind != 'bool'}
        data['registration_enabled'] = 'on'
        data['items_per_page'] = 20
        self.client.post(reverse('site_settings'), data)
        changed_keys = set(SettingChange.objects.values_list('key', flat=True))
        self.assertEqual(changed_keys, {'items_per_page'})


class UserPreferenceTests(TestCase):
    """Per-user overrides of per_user runtime settings via the profile page."""

    def setUp(self):
        from django.core.cache import cache
        from . import runtime_settings
        self.rs = runtime_settings
        cache.delete(runtime_settings._CACHE_KEY)
        self.addCleanup(cache.delete, runtime_settings._CACHE_KEY)
        self.user = User.objects.create_user('u', 'u@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.user, name='Bücher')
        add_field(self.col, 'name', 'Name', FieldType.TEXT, order=0)
        self.client.force_login(self.user)

    def test_get_setting_for_prefers_user_override(self):
        self.user.preferences = {'items_per_page': 7}
        self.assertEqual(self.rs.get_setting_for(self.user, 'items_per_page'), 7)
        self.assertEqual(self.rs.get_setting('items_per_page'), 50)

    def test_non_per_user_setting_ignores_preferences(self):
        self.user.preferences = {'loan_overdue_days': 1}
        self.assertEqual(self.rs.get_setting_for(self.user, 'loan_overdue_days'), 30)

    def test_corrupt_preference_falls_back(self):
        self.user.preferences = {'items_per_page': 'kaputt'}
        self.assertEqual(self.rs.get_setting_for(self.user, 'items_per_page'), 50)

    def test_profile_saves_and_clears_override(self):
        resp = self.client.post(reverse('profile'), {
            'display_name': 'Udo', 'email': 'u@e.de', 'items_per_page': 10,
        })
        self.assertRedirects(resp, reverse('profile'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.preferences, {'items_per_page': 10})
        self.assertEqual(self.user.display_name, 'Udo')

        resp = self.client.post(reverse('profile'), {
            'display_name': 'Udo', 'email': 'u@e.de', 'items_per_page': '',
        })
        self.assertRedirects(resp, reverse('profile'))
        self.user.refresh_from_db()
        self.assertEqual(self.user.preferences, {})

    def test_item_table_uses_personal_page_size(self):
        self.user.preferences = {'items_per_page': 5}
        self.user.save(update_fields=['preferences'])
        for i in range(7):
            Item.objects.create(collection=self.col, values={'name': f'Item {i}'})
        resp = self.client.get(reverse('collection_detail', args=[self.col.pk]))
        self.assertEqual(len(resp.context['rows']), 5)

    def test_other_users_unaffected_by_personal_override(self):
        self.user.preferences = {'items_per_page': 5}
        self.user.save(update_fields=['preferences'])
        other = User.objects.create_user('other', 'x@e.de', 'pw')
        CollectionShare.objects.create(collection=self.col, user=other, permission='view')
        for i in range(7):
            Item.objects.create(collection=self.col, values={'name': f'Item {i}'})
        self.client.force_login(other)
        resp = self.client.get(reverse('collection_detail', args=[self.col.pk]))
        self.assertEqual(len(resp.context['rows']), 7)

    def test_profile_rejects_out_of_range_value(self):
        resp = self.client.post(reverse('profile'), {
            'display_name': '', 'email': 'u@e.de', 'items_per_page': 1,
        })
        self.assertEqual(resp.status_code, 200)  # re-rendered with error
        self.user.refresh_from_db()
        self.assertEqual(self.user.preferences, {})


@override_settings(MEDIA_ROOT=MEDIA)
class TrashTests(TestCase):
    """Soft delete: items move to a per-collection trash and can be restored."""

    def setUp(self):
        from django.core.cache import cache
        from . import runtime_settings
        self.rs = runtime_settings
        cache.delete(runtime_settings._CACHE_KEY)
        self.addCleanup(cache.delete, runtime_settings._CACHE_KEY)
        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.viewer = User.objects.create_user('viewer', 'v@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Bücher')
        add_field(self.col, 'name', 'Name', FieldType.TEXT, order=0)
        CollectionShare.objects.create(collection=self.col, user=self.viewer, permission='view')
        self.item = Item.objects.create(collection=self.col, values={'name': 'Dune'})
        self.client.force_login(self.owner)

    def test_delete_view_moves_item_to_trash(self):
        resp = self.client.post(reverse('item_delete', args=[self.col.pk, self.item.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.col.items.count(), 0)  # default manager hides it
        self.assertEqual(Item.all_objects.filter(collection=self.col).count(), 1)
        # Trashed items are unreachable through the normal views.
        resp = self.client.get(reverse('item_detail', args=[self.col.pk, self.item.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_bulk_delete_moves_to_trash(self):
        second = Item.objects.create(collection=self.col, values={'name': 'Hyperion'})
        self.client.post(reverse('items_bulk', args=[self.col.pk]), {
            'action': 'delete', 'items': [str(self.item.pk), str(second.pk)],
        })
        self.assertEqual(self.col.items.count(), 0)
        self.assertEqual(Item.all_objects.filter(collection=self.col,
                                                 deleted_at__isnull=False).count(), 2)

    def test_trash_page_lists_and_restores(self):
        self.item.soft_delete()
        resp = self.client.get(reverse('collection_trash', args=[self.col.pk]))
        self.assertContains(resp, 'Dune')
        resp = self.client.post(reverse('item_restore', args=[self.col.pk, self.item.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.col.items.count(), 1)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.deleted_at)

    def test_purge_deletes_item_and_files(self):
        import os
        resp = self.client.post(reverse('item_create', args=[self.col.pk]), {'name': 'MitBild'})
        item = self.col.items.get(values__name='MitBild')
        from .models import ItemAsset
        from django.core.files.base import ContentFile
        asset = ItemAsset.objects.create(item=item, field_key='bild',
                                         file=ContentFile(make_png(), name='bild.png'))
        path = asset.file.path
        self.assertTrue(os.path.exists(path))
        item.soft_delete()
        resp = self.client.post(reverse('item_purge', args=[self.col.pk, item.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Item.all_objects.filter(pk=item.pk).exists())
        self.assertFalse(os.path.exists(path))

    def test_empty_trash(self):
        self.item.soft_delete()
        Item.objects.create(collection=self.col, values={'name': 'Bleibt'})
        self.client.post(reverse('trash_empty', args=[self.col.pk]))
        self.assertEqual(Item.all_objects.filter(collection=self.col).count(), 1)
        self.assertEqual(self.col.items.count(), 1)

    def test_retention_purges_old_items_on_trash_open(self):
        from datetime import timedelta
        from django.utils import timezone
        self.item.soft_delete()
        Item.all_objects.filter(pk=self.item.pk).update(
            deleted_at=timezone.now() - timedelta(days=31))
        self.client.get(reverse('collection_trash', args=[self.col.pk]))
        self.assertFalse(Item.all_objects.filter(pk=self.item.pk).exists())

    def test_retention_keeps_recent_items(self):
        self.item.soft_delete()
        self.client.get(reverse('collection_trash', args=[self.col.pk]))
        self.assertTrue(Item.all_objects.filter(pk=self.item.pk).exists())

    def test_viewer_cannot_access_trash_or_restore(self):
        self.item.soft_delete()
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(
            reverse('collection_trash', args=[self.col.pk])).status_code, 403)
        self.assertEqual(self.client.post(
            reverse('item_restore', args=[self.col.pk, self.item.pk])).status_code, 403)

    def test_dashboard_count_excludes_trash(self):
        self.item.soft_delete()
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.context['total_items'], 0)

    def test_purge_trash_command(self):
        from datetime import timedelta
        from django.core.management import call_command
        from django.utils import timezone
        self.item.soft_delete()
        Item.all_objects.filter(pk=self.item.pk).update(
            deleted_at=timezone.now() - timedelta(days=31))
        recent = Item.objects.create(collection=self.col, values={'name': 'Frisch'})
        recent.soft_delete()
        call_command('purge_trash', stdout=io.StringIO())
        self.assertFalse(Item.all_objects.filter(pk=self.item.pk).exists())
        self.assertTrue(Item.all_objects.filter(pk=recent.pk).exists())


class LoanReminderTests(TestCase):
    """The send_loan_reminders management command."""

    def setUp(self):
        from django.core.cache import cache
        from . import runtime_settings
        self.rs = runtime_settings
        cache.delete(runtime_settings._CACHE_KEY)
        self.addCleanup(cache.delete, runtime_settings._CACHE_KEY)
        self.owner = User.objects.create_user('owner', 'owner@example.com', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Bücher')
        self.item = Item.objects.create(collection=self.col, values={'name': 'Dune'})

    def _overdue_loan(self, **kwargs):
        from datetime import timedelta
        from django.utils import timezone
        defaults = {'item': self.item, 'borrower': 'Anna',
                    'lent_at': timezone.localdate() - timedelta(days=5),
                    'due_at': timezone.localdate() - timedelta(days=1)}
        defaults.update(kwargs)
        return Loan.objects.create(**defaults)

    def _run(self):
        from django.core.management import call_command
        call_command('send_loan_reminders', stdout=__import__('io').StringIO())

    def test_disabled_sends_nothing(self):
        from django.core import mail
        self._overdue_loan()
        self._run()
        self.assertEqual(len(mail.outbox), 0)

    def test_overdue_loan_mails_owner_and_stamps(self):
        from django.core import mail
        self.rs.set_setting('loan_reminders_enabled', True)
        loan = self._overdue_loan()
        self._run()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['owner@example.com'])
        self.assertIn('Dune', mail.outbox[0].body)
        self.assertIn('Anna', mail.outbox[0].body)
        loan.refresh_from_db()
        self.assertIsNotNone(loan.reminder_sent_at)

    def test_loan_without_due_date_uses_overdue_days(self):
        from datetime import timedelta
        from django.core import mail
        from django.utils import timezone
        self.rs.set_setting('loan_reminders_enabled', True)
        self._overdue_loan(due_at=None,
                           lent_at=timezone.localdate() - timedelta(days=31))
        self._run()
        self.assertEqual(len(mail.outbox), 1)

    def test_not_overdue_not_mailed(self):
        from datetime import timedelta
        from django.core import mail
        from django.utils import timezone
        self.rs.set_setting('loan_reminders_enabled', True)
        self._overdue_loan(due_at=timezone.localdate() + timedelta(days=3))
        self._run()
        self.assertEqual(len(mail.outbox), 0)

    def test_no_resend_within_interval_but_after(self):
        from datetime import timedelta
        from django.core import mail
        from django.utils import timezone
        self.rs.set_setting('loan_reminders_enabled', True)
        loan = self._overdue_loan()
        self._run()
        self._run()  # immediately again: still 1 mail
        self.assertEqual(len(mail.outbox), 1)
        Loan.objects.filter(pk=loan.pk).update(
            reminder_sent_at=timezone.now() - timedelta(days=8))
        self._run()
        self.assertEqual(len(mail.outbox), 2)

    def test_returned_and_trashed_loans_skipped(self):
        from django.core import mail
        from django.utils import timezone
        self.rs.set_setting('loan_reminders_enabled', True)
        self._overdue_loan(returned_at=timezone.localdate())
        other_item = Item.objects.create(collection=self.col, values={'name': 'Weg'})
        self._overdue_loan(item=other_item)
        other_item.soft_delete()
        self._run()
        self.assertEqual(len(mail.outbox), 0)

    def test_one_digest_per_owner(self):
        from django.core import mail
        self.rs.set_setting('loan_reminders_enabled', True)
        second = Item.objects.create(collection=self.col, values={'name': 'Hyperion'})
        self._overdue_loan()
        self._overdue_loan(item=second, borrower='Ben')
        self._run()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Dune', mail.outbox[0].body)
        self.assertIn('Hyperion', mail.outbox[0].body)


class ApiTests(TestCase):
    """The token-authenticated JSON API (gated by api_enabled)."""

    def setUp(self):
        from django.core.cache import cache
        from accounts.models import ApiToken
        from . import runtime_settings
        self.rs = runtime_settings
        cache.delete(runtime_settings._CACHE_KEY)
        self.addCleanup(cache.delete, runtime_settings._CACHE_KEY)
        self.rs.set_setting('api_enabled', True)

        self.owner = User.objects.create_user('owner', 'o@e.de', 'pw')
        self.viewer = User.objects.create_user('viewer', 'v@e.de', 'pw')
        self.col = Collection.objects.create(owner=self.owner, name='Bücher')
        add_field(self.col, 'name', 'Name', FieldType.TEXT, order=0, required=True)
        add_field(self.col, 'ort', 'Ort', FieldType.TEXT, order=1)
        CollectionShare.objects.create(collection=self.col, user=self.viewer, permission='view')
        self.token = ApiToken.objects.create(user=self.owner, name='Test')
        self.viewer_token = ApiToken.objects.create(user=self.viewer, name='Viewer')
        self.auth = {'headers': {'Authorization': f'Bearer {self.token.key}'}}
        self.items_url = reverse('api_items', args=[self.col.pk])

    def test_disabled_api_rejects_valid_token(self):
        self.rs.set_setting('api_enabled', False)
        resp = self.client.get(reverse('api_collections'), **self.auth)
        self.assertEqual(resp.status_code, 403)

    def test_missing_and_invalid_token(self):
        self.assertEqual(self.client.get(reverse('api_collections')).status_code, 401)
        resp = self.client.get(reverse('api_collections'),
                               headers={'Authorization': 'Bearer falsch'})
        self.assertEqual(resp.status_code, 401)

    def test_inactive_user_token_rejected(self):
        self.owner.is_active = False
        self.owner.save(update_fields=['is_active'])
        self.assertEqual(self.client.get(reverse('api_collections'), **self.auth).status_code, 401)

    def test_list_collections_with_permission(self):
        resp = self.client.get(reverse('api_collections'), **self.auth)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['results']
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['name'], 'Bücher')
        self.assertEqual(data[0]['permission'], 'owner')

    def test_foreign_collection_forbidden(self):
        stranger = User.objects.create_user('fremd', 'f@e.de', 'pw')
        foreign = Collection.objects.create(owner=stranger, name='Privat')
        resp = self.client.get(reverse('api_collection_detail', args=[foreign.pk]), **self.auth)
        self.assertEqual(resp.status_code, 403)

    def test_collection_schema(self):
        ItemType.objects.create(collection=self.col, name='Roman')
        resp = self.client.get(reverse('api_collection_detail', args=[self.col.pk]), **self.auth)
        data = resp.json()
        self.assertEqual([f['key'] for f in data['fields']], ['name', 'ort'])
        self.assertTrue(data['fields'][0]['required'])
        self.assertEqual(data['item_types'][0]['name'], 'Roman')

    def test_items_list_filter_and_pagination(self):
        for name in ('Dune', 'Hyperion'):
            Item.objects.create(collection=self.col, values={'name': name})
        resp = self.client.get(self.items_url, {'q': 'dune'}, **self.auth)
        data = resp.json()
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['page'], 1)
        self.assertEqual(data['results'][0]['values']['name'], 'Dune')

    def test_create_item(self):
        art = ItemType.objects.create(collection=self.col, name='Roman')
        resp = self.client.post(
            self.items_url, data='{"values": {"name": "Dune"}, "item_type": %d}' % art.pk,
            content_type='application/json', **self.auth)
        self.assertEqual(resp.status_code, 201)
        item = self.col.items.get()
        self.assertEqual(item.values['name'], 'Dune')
        self.assertEqual(item.item_type, art)
        self.assertEqual(item.created_by, self.owner)

    def test_create_validates_required_fields(self):
        resp = self.client.post(self.items_url, data='{"values": {"ort": "Regal"}}',
                                content_type='application/json', **self.auth)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('name', resp.json()['fields'])
        self.assertEqual(self.col.items.count(), 0)

    def test_invalid_json_rejected(self):
        resp = self.client.post(self.items_url, data='kein json',
                                content_type='application/json', **self.auth)
        self.assertEqual(resp.status_code, 400)

    def test_patch_merges_put_replaces(self):
        item = Item.objects.create(collection=self.col,
                                   values={'name': 'Dune', 'ort': 'Regal A'})
        url = reverse('api_item', args=[self.col.pk, item.pk])
        resp = self.client.patch(url, data='{"values": {"ort": "Regal B"}}',
                                 content_type='application/json', **self.auth)
        self.assertEqual(resp.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.values['name'], 'Dune')       # kept (merge)
        self.assertEqual(item.values['ort'], 'Regal B')

        resp = self.client.put(url, data='{"values": {"name": "Duna"}}',
                               content_type='application/json', **self.auth)
        self.assertEqual(resp.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.values['name'], 'Duna')
        self.assertFalse(item.values.get('ort'))            # replaced (PUT)

    def test_delete_moves_to_trash(self):
        item = Item.objects.create(collection=self.col, values={'name': 'Weg'})
        url = reverse('api_item', args=[self.col.pk, item.pk])
        resp = self.client.delete(url, **self.auth)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.col.items.count(), 0)
        self.assertTrue(Item.all_objects.filter(pk=item.pk,
                                                deleted_at__isnull=False).exists())

    def test_viewer_can_read_but_not_write(self):
        Item.objects.create(collection=self.col, values={'name': 'Dune'})
        viewer_auth = {'headers': {'Authorization': f'Bearer {self.viewer_token.key}'}}
        self.assertEqual(self.client.get(self.items_url, **viewer_auth).status_code, 200)
        resp = self.client.post(self.items_url, data='{"values": {"name": "Nein"}}',
                                content_type='application/json', **viewer_auth)
        self.assertEqual(resp.status_code, 403)

    def test_token_use_is_stamped(self):
        self.assertIsNone(self.token.last_used_at)
        self.client.get(reverse('api_collections'), **self.auth)
        self.token.refresh_from_db()
        self.assertIsNotNone(self.token.last_used_at)
