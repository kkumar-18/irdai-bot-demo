from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol


@dataclass(frozen=True)
class DisclosureFile:
    insurer: str
    fy: str
    form_hint: str | None
    url: str
    mime: str
    label: str


class DisclosureAdapter(Protocol):
    insurer: str

    def list_files(self, fy: str) -> Iterable[DisclosureFile]: ...


class DiscoveryError(RuntimeError):
    """Raised when an adapter cannot find any disclosure files for a given FY.

    This is deliberately loud rather than returning an empty list: a page that
    renders its file list client-side (React/Next.js hydration, a CMS widget
    that injects the table after load) will look identical to "no filings this
    year" to a requests+BeautifulSoup scrape unless we say so explicitly.
    """


def guess_mime(url: str) -> str:
    lower = url.lower()
    if lower.endswith((".xlsx", ".xls")):
        return (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if lower.endswith(".xlsx")
            else "application/vnd.ms-excel"
        )
    if lower.endswith(".pdf"):
        return "application/pdf"
    return "application/octet-stream"
