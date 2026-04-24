"""Tests for the admin-shared-PDF + DB-backed comment overlay feature."""

import json
from io import BytesIO
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from openpyxl import load_workbook
from pdf import forms
from pdf.models.pdf_models import Pdf, SharedPdfComment
from pdf.views import pdf_views
from users.service import get_demo_pdf


def make_user(email: str, *, is_admin: bool = False) -> User:
    user = User.objects.create_user(username=email, password='pw12345', email=email)
    if is_admin:
        user.is_staff = True
        user.is_superuser = True
        user.save()
    return user


class SharedPdfMixin:
    def login(self, user: User) -> Client:
        client = Client()
        client.login(username=user.username, password='pw12345')
        return client


@mock.patch('pdf.views.pdf_views.pdf_services.PdfProcessingServices.set_highlights_and_comments')
@mock.patch('pdf.views.pdf_views.pdf_services.PdfProcessingServices.process_with_pypdfium')
@mock.patch('pdf.forms.magic.from_buffer', return_value='application/pdf')
class TestAdminSharedUpload(SharedPdfMixin, TestCase):
    def test_admin_upload_with_share_marks_shared_master(self, *_):
        admin = make_user('admin@a.com', is_admin=True)
        client = self.login(admin)
        # warm-up to instantiate profile
        client.get(reverse('pdf_overview'))

        form = forms.AddForm(
            data={
                'name': 'shared.pdf',
                'collection': admin.profile.current_collection.id,
                'share_with_all': 'on',
            },
            profile=admin.profile,
            files={'file': get_demo_pdf()},
        )
        request = client.get(reverse('pdf_overview')).wsgi_request
        pdf_views.AddPdfMixin.obj_save(form, request, None)

        pdf = Pdf.objects.get(name='shared.pdf')
        self.assertTrue(pdf.is_shared_master)

    def test_non_admin_cannot_create_shared_master(self, *_):
        user = make_user('user@a.com')
        client = self.login(user)
        client.get(reverse('pdf_overview'))

        form = forms.AddForm(
            data={
                'name': 'fake-shared.pdf',
                'collection': user.profile.current_collection.id,
                'share_with_all': 'on',
            },
            profile=user.profile,
            files={'file': get_demo_pdf()},
        )
        request = client.get(reverse('pdf_overview')).wsgi_request
        pdf_views.AddPdfMixin.obj_save(form, request, None)

        pdf = Pdf.objects.get(name='fake-shared.pdf')
        self.assertFalse(pdf.is_shared_master)


class TestSharedVisibilityAndPermissions(SharedPdfMixin, TestCase):
    @mock.patch('pdf.views.pdf_views.pdf_services.PdfProcessingServices.set_highlights_and_comments')
    @mock.patch('pdf.views.pdf_views.pdf_services.PdfProcessingServices.process_with_pypdfium')
    @mock.patch('pdf.forms.magic.from_buffer', return_value='application/pdf')
    def setUp(self, *_):
        self.admin = make_user('admin@a.com', is_admin=True)
        self.user = make_user('user@a.com')

        admin_client = self.login(self.admin)
        admin_client.get(reverse('pdf_overview'))
        form = forms.AddForm(
            data={
                'name': 'shared.pdf',
                'collection': self.admin.profile.current_collection.id,
                'share_with_all': 'on',
            },
            profile=self.admin.profile,
            files={'file': get_demo_pdf()},
        )
        request = admin_client.get(reverse('pdf_overview')).wsgi_request
        pdf_views.AddPdfMixin.obj_save(form, request, None)
        self.master = Pdf.objects.get(name='shared.pdf')

    def test_non_admin_can_see_shared_pdf(self):
        self.assertIn(self.master, self.user.profile.all_pdfs)
        # in the 'all' collection view, shared masters are surfaced
        self.user.profile.current_collection_id = 'all'
        self.user.profile.save()
        self.assertIn(self.master, self.user.profile.current_pdfs)

    def test_admin_does_not_see_other_admins_shared_via_extra_filter(self):
        # admin sees their own master via the regular collection path
        self.assertIn(self.master, self.admin.profile.all_pdfs)

    def test_non_admin_blocked_from_editing_shared(self):
        client = self.login(self.user)
        resp = client.post(
            reverse('edit_pdf', kwargs={'identifier': self.master.id, 'field_name': 'name'}),
            data={'name': 'hacked'},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(resp.status_code, 403)
        self.master.refresh_from_db()
        self.assertEqual(self.master.name, 'shared.pdf')

    def test_non_admin_blocked_from_deleting_shared(self):
        client = self.login(self.user)
        resp = client.delete(
            reverse('delete_pdf', kwargs={'identifier': self.master.id}),
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Pdf.objects.filter(id=self.master.id).exists())

    def test_non_admin_blocked_from_update_pdf(self):
        client = self.login(self.user)
        resp = client.post(
            reverse('update_pdf'),
            data={'pdf_id': str(self.master.id)},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(resp.status_code, 403)


class TestSharedCommentsApi(SharedPdfMixin, TestCase):
    @mock.patch('pdf.views.pdf_views.pdf_services.PdfProcessingServices.set_highlights_and_comments')
    @mock.patch('pdf.views.pdf_views.pdf_services.PdfProcessingServices.process_with_pypdfium')
    @mock.patch('pdf.forms.magic.from_buffer', return_value='application/pdf')
    def setUp(self, *_):
        self.admin = make_user('admin@a.com', is_admin=True)
        self.alice = make_user('alice@a.com')
        self.bob = make_user('bob@a.com')

        admin_client = self.login(self.admin)
        admin_client.get(reverse('pdf_overview'))
        form = forms.AddForm(
            data={
                'name': 'shared.pdf',
                'collection': self.admin.profile.current_collection.id,
                'share_with_all': 'on',
            },
            profile=self.admin.profile,
            files={'file': get_demo_pdf()},
        )
        request = admin_client.get(reverse('pdf_overview')).wsgi_request
        pdf_views.AddPdfMixin.obj_save(form, request, None)
        self.master = Pdf.objects.get(name='shared.pdf')
        self.list_url = reverse('shared_comments_list', kwargs={'pdf_id': self.master.id})

    def _post(self, client, **payload):
        return client.post(self.list_url, data=json.dumps(payload), content_type='application/json')

    def test_create_list_for_self(self):
        client = self.login(self.alice)
        resp = self._post(client, page=1, x=0.5, y=0.25, text='hi')
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body['text'], 'hi')
        self.assertTrue(body['is_mine'])

        resp = client.get(self.list_url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()['comments']), 1)

    def test_other_user_cannot_modify(self):
        a = self.login(self.alice)
        created = self._post(a, page=1, x=0.5, y=0.25, text='alice').json()
        detail = reverse('shared_comment_detail', kwargs={
            'pdf_id': self.master.id, 'comment_id': created['id'],
        })

        b = self.login(self.bob)
        resp = b.patch(detail, data=json.dumps({'text': 'tampered'}), content_type='application/json')
        self.assertEqual(resp.status_code, 403)
        resp = b.delete(detail)
        self.assertEqual(resp.status_code, 403)

    def test_admin_can_modify_any(self):
        a = self.login(self.alice)
        created = self._post(a, page=1, x=0.5, y=0.25, text='alice').json()
        detail = reverse('shared_comment_detail', kwargs={
            'pdf_id': self.master.id, 'comment_id': created['id'],
        })

        admin_client = self.login(self.admin)
        resp = admin_client.patch(detail, data=json.dumps({'text': 'admin edit'}), content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(SharedPdfComment.objects.get(id=created['id']).text, 'admin edit')

        resp = admin_client.delete(detail)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(SharedPdfComment.objects.filter(id=created['id']).exists())

    def test_create_rejects_invalid_coords(self):
        a = self.login(self.alice)
        resp = self._post(a, page=1, x=1.5, y=0.25, text='oops')
        self.assertEqual(resp.status_code, 400)

    def test_create_rejected_for_non_shared_pdf(self):
        # a regular (non-shared) PDF created in admin's workspace should not accept comments
        admin_client = self.login(self.admin)
        admin_client.get(reverse('pdf_overview'))

        with mock.patch('pdf.views.pdf_views.pdf_services.PdfProcessingServices.set_highlights_and_comments'), \
             mock.patch('pdf.views.pdf_views.pdf_services.PdfProcessingServices.process_with_pypdfium'), \
             mock.patch('pdf.forms.magic.from_buffer', return_value='application/pdf'):
            form = forms.AddForm(
                data={
                    'name': 'private.pdf',
                    'collection': self.admin.profile.current_collection.id,
                },
                profile=self.admin.profile,
                files={'file': get_demo_pdf()},
            )
            request = admin_client.get(reverse('pdf_overview')).wsgi_request
            pdf_views.AddPdfMixin.obj_save(form, request, None)
            private = Pdf.objects.get(name='private.pdf')

        url = reverse('shared_comments_list', kwargs={'pdf_id': private.id})
        resp = admin_client.post(
            url, data=json.dumps({'page': 1, 'x': 0.5, 'y': 0.5, 'text': 'x'}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 404)


class TestExportSharedAnnotations(SharedPdfMixin, TestCase):
    @mock.patch('pdf.views.pdf_views.pdf_services.PdfProcessingServices.set_highlights_and_comments')
    @mock.patch('pdf.views.pdf_views.pdf_services.PdfProcessingServices.process_with_pypdfium')
    @mock.patch('pdf.forms.magic.from_buffer', return_value='application/pdf')
    def setUp(self, *_):
        self.admin = make_user('admin@a.com', is_admin=True)
        self.alice = make_user('alice@a.com')
        self.bob = make_user('bob@a.com')

        admin_client = self.login(self.admin)
        admin_client.get(reverse('pdf_overview'))
        form = forms.AddForm(
            data={
                'name': 'doc.pdf',
                'collection': self.admin.profile.current_collection.id,
                'share_with_all': 'on',
            },
            profile=self.admin.profile,
            files={'file': get_demo_pdf()},
        )
        request = admin_client.get(reverse('pdf_overview')).wsgi_request
        pdf_views.AddPdfMixin.obj_save(form, request, None)
        self.master = Pdf.objects.get(name='doc.pdf')

        SharedPdfComment.objects.create(pdf=self.master, user=self.alice, page=2, x=0.1, y=0.2, text='alice-1')
        SharedPdfComment.objects.create(pdf=self.master, user=self.alice, page=3, x=0.1, y=0.2, text='alice-2')
        SharedPdfComment.objects.create(pdf=self.master, user=self.bob, page=1, x=0.1, y=0.2, text='bob-1')

    def test_non_admin_blocked(self):
        client = self.login(self.alice)
        resp = client.get(reverse('export_shared_annotations'))
        self.assertEqual(resp.status_code, 404)

    def test_admin_export_xlsx_contents(self):
        client = self.login(self.admin)
        resp = client.get(reverse('export_shared_annotations'))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('spreadsheet', resp['Content-Type'])

        buf = BytesIO(b''.join(resp.streaming_content))
        wb = load_workbook(buf)
        self.assertIn('Summary', wb.sheetnames)
        self.assertIn('doc.pdf', wb.sheetnames)

        ws = wb['doc.pdf']
        rows = list(ws.iter_rows(values_only=True))
        # header row
        self.assertEqual(rows[0][:2], ('User Email', 'Page'))
        # find user group rows
        flattened_text = [r[2] for r in rows[1:] if r and len(r) > 2 and r[2]]
        self.assertIn('alice-1', flattened_text)
        self.assertIn('alice-2', flattened_text)
        self.assertIn('bob-1', flattened_text)

        # summary row for the file: 2 users, 3 comments
        summary_rows = list(wb['Summary'].iter_rows(values_only=True))
        data_row = next(r for r in summary_rows[1:] if r and r[0] == 'doc.pdf')
        self.assertEqual(data_row[1], 2)
        self.assertEqual(data_row[2], 3)
