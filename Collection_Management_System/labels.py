"""Printable QR-code label sheets (PDF) for a collection's items.

Produces an A4 grid of labels — each with the item's QR code (linking to its
detail page), its short code and its name — ready to print and stick onto the
physical objects / shelves.
"""

from __future__ import annotations

from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from django.utils.translation import gettext as _

from . import codes

COLS, ROWS = 3, 8          # 24 labels per A4 page
MARGIN = 10 * mm
QR_SIZE = 22 * mm


def build_label_pdf(items, build_uri, detail_path_for) -> bytes:
    """Render labels for ``items``. ``detail_path_for(item)`` -> item URL path."""
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    cell_w = (width - 2 * MARGIN) / COLS
    cell_h = (height - 2 * MARGIN) / ROWS
    per_page = COLS * ROWS

    if not items:
        c.setFont('Helvetica', 12)
        c.drawCentredString(width / 2, height / 2, _('Keine Gegenstände zum Drucken.'))
        c.showPage()
        c.save()
        return buf.getvalue()

    for idx, item in enumerate(items):
        if idx and idx % per_page == 0:
            c.showPage()
        pos = idx % per_page
        col = pos % COLS
        row = pos // COLS
        x = MARGIN + col * cell_w
        top = height - MARGIN - row * cell_h

        url = build_uri(detail_path_for(item))
        qr = ImageReader(BytesIO(codes.qr_png(url)))
        c.drawImage(qr, x + (cell_w - QR_SIZE) / 2, top - QR_SIZE - 2 * mm,
                    QR_SIZE, QR_SIZE, preserveAspectRatio=True)

        c.setFont('Helvetica-Bold', 8)
        c.drawCentredString(x + cell_w / 2, top - QR_SIZE - 6 * mm, codes.item_short_code(item))
        c.setFont('Helvetica', 7)
        name = (str(item) or '')[:26]
        c.drawCentredString(x + cell_w / 2, top - QR_SIZE - 10 * mm, name)

    c.showPage()
    c.save()
    return buf.getvalue()
