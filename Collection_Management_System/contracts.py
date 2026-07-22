"""Printable loan agreement (Leihvertrag) as a PDF for a single loan.

A simple, self-contained one-page contract: parties (lender from the site's
legal settings, borrower from the loan), the loaned object, the agreed dates,
standard conditions and signature lines. Rendered with reportlab like the QR
label sheets (see :mod:`labels`) — no extra dependency.
"""

from __future__ import annotations

from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

from django.utils import timezone
from django.utils.translation import gettext as _

from . import codes

MARGIN = 20 * mm
LINE = 5.4 * mm


class _Writer:
    """Tiny top-down text cursor over a reportlab canvas."""

    def __init__(self, c: canvas.Canvas):
        self.c = c
        self.width, self.height = A4
        self.x = MARGIN
        self.y = self.height - MARGIN
        self.right = self.width - MARGIN

    def space(self, factor: float = 1.0) -> None:
        self.y -= LINE * factor

    def heading(self, text: str, size: int = 16) -> None:
        self.c.setFont('Helvetica-Bold', size)
        self.c.drawString(self.x, self.y, text)
        self.space(1.4)

    def label(self, text: str) -> None:
        self.c.setFont('Helvetica-Bold', 10)
        self.c.drawString(self.x, self.y, text)
        self.space()

    def paragraph(self, text: str, size: int = 10, bold: bool = False, indent: float = 0) -> None:
        font = 'Helvetica-Bold' if bold else 'Helvetica'
        self.c.setFont(font, size)
        max_width = self.right - self.x - indent
        for raw_line in (text or '').split('\n'):
            for line in _wrap(raw_line, font, size, max_width):
                self.c.drawString(self.x + indent, self.y, line)
                self.space()

    def rule(self) -> None:
        self.space(0.4)
        self.c.setStrokeGray(0.75)
        self.c.line(self.x, self.y, self.right, self.y)
        self.space(0.8)

    def signatures(self, left: str, right: str) -> None:
        mid = self.x + (self.right - self.x) / 2
        gap = 8 * mm
        self.space(3)
        self.c.setStrokeGray(0.4)
        self.c.line(self.x, self.y, mid - gap, self.y)
        self.c.line(mid + gap, self.y, self.right, self.y)
        self.space(0.9)
        self.c.setFont('Helvetica', 9)
        self.c.drawString(self.x, self.y, left)
        self.c.drawString(mid + gap, self.y, right)
        self.space()


def _wrap(text: str, font: str, size: int, max_width: float) -> list[str]:
    if not text:
        return ['']
    words = text.split(' ')
    lines, current = [], ''
    for word in words:
        trial = f'{current} {word}'.strip()
        if stringWidth(trial, font, size) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _lender_lines() -> list[str]:
    from .runtime_settings import get_setting
    operator = (get_setting('legal_operator') or '').strip()
    address = (get_setting('legal_address') or '').strip()
    email = (get_setting('legal_email') or '').strip()
    lines = []
    if operator:
        lines.append(operator)
    if address:
        lines.extend(address.splitlines())
    if email:
        lines.append(email)
    return lines or [_('(Verleiher im Systemeinstellungen → Impressum hinterlegen)')]


def build_loan_agreement_pdf(collection, item, loan) -> bytes:
    """Render a one-page Leihvertrag PDF for ``loan`` and return the bytes."""
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(_('Leihvertrag'))
    w = _Writer(c)

    w.heading(_('Leihvertrag'))
    w.paragraph(_('über die leihweise Überlassung des unten bezeichneten Gegenstands.'))
    w.rule()

    # Parties.
    w.label(_('Verleiher'))
    w.paragraph('\n'.join(_lender_lines()))
    w.space(0.6)
    w.label(_('Entleiher'))
    w.paragraph(loan.borrower or '—')
    if loan.borrower_contact:
        w.paragraph(loan.borrower_contact)
    w.rule()

    # Object.
    w.label(_('Leihgegenstand'))
    w.paragraph(_('Sammlung: %(name)s') % {'name': collection.name})
    w.paragraph(_('Bezeichnung: %(name)s') % {'name': str(item)})
    if item.item_type:
        w.paragraph(_('Art: %(type)s') % {'type': item.item_type})
    w.paragraph(_('Kennung: %(code)s') % {'code': codes.item_short_code(item)})
    w.paragraph(_('ID: %(id)s') % {'id': item.id})
    w.rule()

    # Terms.
    w.label(_('Konditionen'))
    w.paragraph(_('Verliehen am: %(date)s') % {'date': loan.lent_at})
    if loan.due_at:
        w.paragraph(_('Rückgabe vereinbart bis: %(date)s') % {'date': loan.due_at})
    else:
        w.paragraph(_('Rückgabe: nach Vereinbarung.'))
    if loan.note:
        w.paragraph(_('Notiz: %(note)s') % {'note': loan.note})
    w.space(0.6)
    w.paragraph(_('Der Entleiher bestätigt, den Gegenstand in einwandfreiem Zustand '
                 'erhalten zu haben, und verpflichtet sich, ihn pfleglich zu behandeln '
                 'und fristgerecht sowie im ursprünglichen Zustand zurückzugeben. Für '
                 'Verlust oder Beschädigung während der Leihdauer haftet der Entleiher.'))
    w.rule()

    # Signatures.
    place_date = _('Ort, Datum: ______________________, %(today)s') % {
        'today': timezone.localdate()}
    w.paragraph(place_date)
    w.signatures(_('Unterschrift Verleiher'), _('Unterschrift Entleiher'))

    c.showPage()
    c.save()
    return buf.getvalue()
