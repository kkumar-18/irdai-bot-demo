"""HDFC Life public-disclosure adapter.

Confirmed by inspecting the live page (2026-09-13): the disclosure listing at
/about-us/public-disclosure is server-rendered AEM markup — every fiscal year's
files are present in the initial HTML as plain <a> tags under
.../financial-information/public-disclosure/FY{YYYY}/{period}/*.pdf. The
"Select Year" <select> on the page is a client-side filter over that
already-complete DOM, not a data loader, so a plain requests+BeautifulSoup GET
is sufficient — no headless browser needed.

Each insurer publishes one combined PDF per period bundling all L-forms
(L-1 through L-45), not one file per form, so form_hint is left unset here;
the extraction step locates individual forms by scanning pages inside the PDF.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin
from typing import Iterable

import requests
from bs4 import BeautifulSoup

from irdai_bot.adapters.base import DisclosureFile, DiscoveryError, guess_mime
from irdai_bot.scope import FISCAL_YEARS

BASE_URL = "https://www.hdfclife.com"
DISCLOSURE_PAGE = f"{BASE_URL}/about-us/public-disclosure"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# .../public-disclosure/FY{folder_year}/{period}/{filename}
_LINK_RE = re.compile(
    r"/public-disclosure/FY(?P<folder_year>\d{4})/(?P<period>[A-Za-z0-9]+)/[^\"'>]+\.(?:pdf|xlsx?)",
    re.IGNORECASE,
)


def _folder_year_to_fy(folder_year: str) -> str:
    """HDFC's 'FY{Y}' folder is the fiscal year ending March {Y} -> our '{Y-1}-{YY}'."""
    end_year = int(folder_year)
    return f"{end_year - 1}-{str(end_year)[-2:]}"


insurer = "hdfc_life"


def list_files(fy_filter: tuple[str, ...] = FISCAL_YEARS) -> Iterable[DisclosureFile]:
    resp = requests.get(DISCLOSURE_PAGE, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    anchors = soup.find_all("a", href=_LINK_RE)
    if not anchors:
        raise DiscoveryError(
            f"No public-disclosure file links found on {DISCLOSURE_PAGE}. "
            "The page markup may have changed, or the file list may now be "
            "injected client-side (check for a __NEXT_DATA__/JSON payload or "
            "an XHR the year selector fires, as was done for Axis Max Life)."
        )

    seen: set[str] = set()
    for a in anchors:
        href = a["href"]
        m = _LINK_RE.search(href)
        if not m:
            continue
        fy = _folder_year_to_fy(m.group("folder_year"))
        period = m.group("period")
        if fy not in fy_filter or period != "FY":
            continue  # annual (year-end) filings only, per MVP scope
        url = urljoin(BASE_URL, href)
        if url in seen:
            continue
        seen.add(url)
        label = a.get_text(strip=True) or href.rsplit("/", 1)[-1]
        yield DisclosureFile(
            insurer=insurer,
            fy=fy,
            form_hint=None,
            url=url,
            mime=guess_mime(url),
            label=label,
        )
