"""XLSX extraction for IRDAI disclosure files.

Neither insurer's annual filing in the MVP scope actually ships as XLSX (both
publish combined PDFs — confirmed against the live sites, see adapters/), but
the MIME router (extract/router.py) still needs a path for it since the
project scope doesn't rule it out for future years/insurers. Unlike the PDF
case, XLSX cells are already gridded, so this is a straightforward
row-label/column-header walk with no position-clustering needed.
"""

from __future__ import annotations

from dataclasses import dataclass

import openpyxl

from irdai_bot.extract.pdf import RawCell  # reuse the same raw-cell shape


def _is_header_row(row: tuple) -> bool:
    non_empty = [c for c in row if c.value is not None]
    if len(non_empty) < 2:
        return False
    numeric = sum(1 for c in non_empty if isinstance(c.value, (int, float)))
    return numeric / len(non_empty) < 0.3  # mostly text -> treat as a header row


def extract_sheet_cells(
    sheet, *, insurer: str, fy: str, form: str, source_sha256: str
) -> list[RawCell]:
    rows = list(sheet.iter_rows())
    if not rows:
        return []

    header_row_idx = 0
    for i, row in enumerate(rows[:5]):
        if _is_header_row(row):
            header_row_idx = i
    col_headers = {c.column: str(c.value).strip() for c in rows[header_row_idx] if c.value is not None}

    cells: list[RawCell] = []
    for row in rows[header_row_idx + 1 :]:
        label_cell = next((c for c in row if c.value is not None), None)
        if label_cell is None:
            continue
        row_label = str(label_cell.value).strip()
        if not row_label:
            continue
        for c in row:
            if c.column == label_cell.column or c.value is None:
                continue
            if not isinstance(c.value, (int, float, str)):
                continue
            cells.append(
                RawCell(
                    insurer=insurer,
                    fy=fy,
                    form=form,
                    row_label=row_label,
                    col_label=col_headers.get(c.column, f"col_{c.column}"),
                    raw_value=str(c.value),
                    source_sha256=source_sha256,
                    page_number=1,
                    row_top=float(c.row),
                )
            )
    return cells


def extract_xlsx(path: str, *, insurer: str, fy: str, form_hint: str | None, source_sha256: str) -> list[RawCell]:
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    cells: list[RawCell] = []
    for sheet in wb.worksheets:
        form = form_hint or sheet.title.strip()
        cells.extend(
            extract_sheet_cells(sheet, insurer=insurer, fy=fy, form=form, source_sha256=source_sha256)
        )
    return cells
