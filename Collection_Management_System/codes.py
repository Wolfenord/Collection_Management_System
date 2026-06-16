"""QR-code and barcode image generation.

Two uses from the concept:
  * QR codes that encode a (filtered) collection URL — scanning jumps straight to
    "all items in shelf 3" etc. The shareable filter URL is the QR payload.
  * Per-item QR (-> item detail page) and a Code128 barcode of a short item code,
    for printing labels.
"""

from __future__ import annotations

from io import BytesIO

import barcode
import qrcode
from barcode.writer import ImageWriter


def qr_png(data: str) -> bytes:
    """Return a PNG QR code encoding ``data``."""
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = BytesIO()
    img.save(buf, 'PNG')
    return buf.getvalue()


def barcode_png(value: str) -> bytes:
    """Return a PNG Code128 barcode encoding ``value``."""
    code = barcode.get('code128', value, writer=ImageWriter())
    buf = BytesIO()
    code.write(buf)
    return buf.getvalue()


def item_short_code(item) -> str:
    """A compact, scannable identifier for an item (first 12 hex of its UUID)."""
    return item.id.hex[:12].upper()
