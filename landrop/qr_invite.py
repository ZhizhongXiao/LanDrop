"""Local-only QR rendering for one-time LAN invitations."""

from __future__ import annotations

import base64
from io import BytesIO

import qrcode
from qrcode.constants import ERROR_CORRECT_M


def qr_png_data_uri(invitation_url: str) -> str:
    """Return a PNG data URI without sending the invitation to any service."""
    if not invitation_url:
        return ""
    code = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    code.add_data(invitation_url)
    code.make(fit=True)
    image = code.make_image(fill_color="#0b5cab", back_color="#ffffff")
    output = BytesIO()
    image.save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"
