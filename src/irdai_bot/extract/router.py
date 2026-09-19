from __future__ import annotations

from irdai_bot.extract.pdf import RawCell, extract_pdf
from irdai_bot.extract.xlsx import extract_xlsx


class UnsupportedFormat(ValueError):
    pass


def extract(
    path: str,
    mime: str,
    *,
    insurer: str,
    fy: str,
    form_hint: str | None,
    source_sha256: str,
    forms: list[str] | None = None,
) -> list[RawCell]:
    """forms: restrict to these form codes (None/empty = every form found)."""
    if mime in (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    ):
        cells = extract_xlsx(path, insurer=insurer, fy=fy, form_hint=form_hint, source_sha256=source_sha256)
        return [c for c in cells if not forms or c.form in forms]
    if mime == "application/pdf":
        return extract_pdf(path, insurer=insurer, fy=fy, source_sha256=source_sha256, forms=forms)
    raise UnsupportedFormat(mime)
