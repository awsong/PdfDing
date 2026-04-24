import re
from io import BytesIO

from base import base_views
from django.conf import settings
from django.contrib.auth.mixins import UserPassesTestMixin
from django.contrib.auth.models import User
from django.db.models import QuerySet
from django.db.models.functions import Lower
from django.http import FileResponse, Http404, HttpRequest
from django.shortcuts import redirect, render
from django.views import View
from django_htmx.http import HttpResponseClientRefresh
from openpyxl import Workbook
from openpyxl.styles import Font
from pdf.models.pdf_models import Pdf, SharedPdfComment


class BaseAdminRequiredMixin(UserPassesTestMixin):
    def test_func(self):
        if self.request.user.is_superuser and self.request.user.is_staff:
            return True
        else:
            raise Http404("Given query not found...")


class BaseAdminMixin:
    obj_name = 'user'


class OverviewMixin(BaseAdminMixin):
    overview_page_name = 'user_overview_page'

    @staticmethod
    def get_sorting(request: HttpRequest):
        """Get the sorting of the overview page."""

        profile = request.user.profile

        sorting_dict = {
            'Newest': '-date_joined',
            'Oldest': 'date_joined',
            'Email_asc': Lower('email'),
            'Email_desc': Lower('email').desc(),
        }

        return sorting_dict[profile.user_sorting]

    @staticmethod
    def filter_objects(request: HttpRequest) -> QuerySet:
        """
        Filter the PDFs when performing a search in the overview.
        """

        users = User.objects.all()

        search = request.GET.get('search', '')
        tags = request.GET.get('tags', [])
        if tags:
            tags = tags.split(' ')

        if 'admin' in tags:
            users = users.filter(is_superuser=True)

        if search:
            users = users.filter(email__icontains=search)

        return users

    @staticmethod
    def get_extra_context(request: HttpRequest) -> dict:
        """get further information that needs to be passed to the template."""

        tag_query = request.GET.get('tags', [])
        if tag_query:
            tag_query = tag_query.split(' ')

        extra_context = {
            'search_query': request.GET.get('search', ''),
            'tag_query': tag_query,
            'page': 'user_overview',
        }

        return extra_context


class AdminMixin(BaseAdminMixin):
    @staticmethod
    def get_object(_, identifier: str):
        user = User.objects.get(id=identifier)

        return user


class Overview(BaseAdminRequiredMixin, OverviewMixin, base_views.BaseOverview):
    """
    View for the user overview page. This view performs the searching and sorting of the users. It's also responsible
    for paginating the users.
    """


class OverviewQuery(base_views.BaseOverviewQuery):
    """View for performing searches and sorting on the user overview page."""

    obj_name = 'user'


class DeleteProfile(BaseAdminRequiredMixin, AdminMixin, base_views.BaseDelete):
    """View for deleting a user profile"""


class AdjustAdminRights(BaseAdminRequiredMixin, View):
    """View for adjusting the admin rights"""

    def post(self, request: HttpRequest, identifier: str):
        """Delete the user"""

        if request.htmx:
            user = User.objects.get(id=identifier)

            if user.is_staff and user.is_superuser:
                user.is_staff = False
                user.is_superuser = False
            else:
                user.is_staff = True
                user.is_superuser = True

            user.save()

            return HttpResponseClientRefresh()

        return redirect('user_overview')


class ExportSharedAnnotations(BaseAdminRequiredMixin, View):
    """Admin-only XLSX export of all DB-backed comments on admin-shared PDFs."""

    INVALID_SHEET_CHARS = re.compile(r'[\\/*?:\[\]]')

    def get(self, request: HttpRequest):
        wb = Workbook()
        # remove the default sheet; we add per-PDF sheets below
        wb.remove(wb.active)

        masters = Pdf.objects.filter(is_shared_master=True).order_by('name')
        bold = Font(bold=True)

        used_titles = set()
        summary_rows = []

        for master in masters:
            comments = (
                SharedPdfComment.objects.filter(pdf=master)
                .select_related('user')
                .order_by('user__email', 'page', 'creation_date')
            )

            title = self._make_unique_sheet_title(master.name, used_titles)
            ws = wb.create_sheet(title=title)
            headers = ['User Email', 'Page', 'Text', 'Created', 'Modified']
            ws.append(headers)
            for cell in ws[1]:
                cell.font = bold

            current_user = None
            for c in comments:
                email = c.user.email or f'user#{c.user_id}'
                if email != current_user:
                    current_user = email
                    ws.append([f'User: {email}'])
                    ws.cell(row=ws.max_row, column=1).font = bold
                ws.append(
                    [
                        email,
                        c.page,
                        c.text,
                        c.creation_date.replace(tzinfo=None),
                        c.modification_date.replace(tzinfo=None),
                    ]
                )

            ws.column_dimensions['A'].width = 32
            ws.column_dimensions['B'].width = 6
            ws.column_dimensions['C'].width = 80
            ws.column_dimensions['D'].width = 22
            ws.column_dimensions['E'].width = 22

            summary_rows.append(
                [
                    master.name,
                    comments.values('user').distinct().count(),
                    comments.count(),
                ]
            )

        summary = wb.create_sheet(title='Summary', index=0)
        summary.append(['PDF', 'Users', 'Comments'])
        for cell in summary[1]:
            cell.font = bold
        for row in summary_rows:
            summary.append(row)
        summary.column_dimensions['A'].width = 50
        summary.column_dimensions['B'].width = 10
        summary.column_dimensions['C'].width = 12

        if not masters.exists():
            summary.append(['(no admin-shared PDFs)', 0, 0])

        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        return FileResponse(
            buf, as_attachment=True, filename='shared_annotations.xlsx',
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )

    @classmethod
    def _make_unique_sheet_title(cls, name: str, used: set) -> str:
        title = cls.INVALID_SHEET_CHARS.sub('_', name).strip() or 'PDF'
        title = title[:31]
        candidate = title
        i = 2
        while candidate.lower() in used:
            suffix = f'_{i}'
            candidate = (title[: 31 - len(suffix)] + suffix)
            i += 1
        used.add(candidate.lower())
        return candidate


class Information(View):  # pragma: no cover
    """View for getting instance information"""

    def get(self, request: HttpRequest):
        """Get instance information"""

        number_of_users = User.objects.all().count()
        number_of_pdfs = Pdf.objects.all().count()

        context = {
            'number_of_users': number_of_users,
            'number_of_pdfs': number_of_pdfs,
            'current_version': settings.VERSION,
        }

        return render(request, 'information.html', context=context)
