"""Axis Max Life public-disclosure adapter.

Confirmed by inspecting the live page (2026-09-13): this is a Next.js page, and
its "Select Year" dropdown only toggles which slice of an already-fully-loaded
dataset is visible in the DOM — it does not lazily fetch data. That full
dataset ships inline in the server-rendered HTML as
`<script id="__NEXT_DATA__" type="application/json">`. So instead of scraping
the (partially client-filtered) rendered `<a>` tags, we use BeautifulSoup to
pull out that one <script> tag and parse the JSON payload directly — this is
plain requests + BeautifulSoup, no headless browser, and it is *more* complete
than scraping the rendered DOM since the DOM only shows one year at a time.

Within that JSON, `props.pageProps.page.sections[*].reports.data` is a list of
per-fiscal-year buckets (`attributes.title` = "2019-20" etc.), each holding a
`reports` list of individual filings. Two link fields appear depending on
vintage: newer filings (FY2024-25+) populate `uploadFile.url` (absolute);
older filings populate `actionLink` (site-relative) instead, with
`uploadFile` left null — both were confirmed present in the live payload
across FY2019-20..FY2025-26, so both are read here, absolute URL preferred.

Annual filings are tagged internally by *quarter* ("Q4-FY-XX-YY" = year-end),
there being no separate "annual" bucket — so the year-end filing is selected
by quarter tag, not by a distinct field.

Before FY2021-22, insurers split the L-forms across two files per period
("Financial Disclosures" / Forms L1-L22, and "Other Disclosures" / Forms
L24-L42); from FY2021-22 onward it's one consolidated "Public Disclosures" /
"L1--L45" file. Both shapes are handled the same way here: any report whose
title contains "disclosure" is kept (this also naturally excludes unrelated
Q4 entries like "Annual Report" or "Resignation of Director").
"""

from __future__ import annotations

import json
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from irdai_bot.adapters.base import DisclosureFile, DiscoveryError, guess_mime
from irdai_bot.scope import FISCAL_YEARS

BASE_URL = "https://www.axismaxlife.com"
DISCLOSURE_PAGE = f"{BASE_URL}/newsroom/public-disclosures"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

insurer = "axis_max_life"


def _fetch_next_data() -> dict[str, Any]:
    resp = requests.get(DISCLOSURE_PAGE, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag is None or not tag.string:
        raise DiscoveryError(
            f"No __NEXT_DATA__ payload found on {DISCLOSURE_PAGE}. The site's "
            "framework or build setup may have changed (this page was Next.js "
            "with an inline JSON payload as of 2026-09-13) — re-inspect the "
            "page source for where the file manifest now lives."
        )
    try:
        return json.loads(tag.string)
    except json.JSONDecodeError as e:
        raise DiscoveryError(f"__NEXT_DATA__ on {DISCLOSURE_PAGE} is not valid JSON: {e}") from e


def _find_fy_buckets(data: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        sections = data["props"]["pageProps"]["page"]["sections"]
    except (KeyError, TypeError) as e:
        raise DiscoveryError(
            "__NEXT_DATA__ payload shape changed: expected "
            "props.pageProps.page.sections — re-inspect the page's JSON."
        ) from e
    for section in sections:
        reports = section.get("reports")
        if isinstance(reports, dict) and isinstance(reports.get("data"), list):
            return reports["data"]
    raise DiscoveryError(
        "No section with a reports.data[] list found in __NEXT_DATA__ — the "
        "page structure likely changed since 2026-09-13."
    )


def _report_url(report: dict[str, Any]) -> str | None:
    upload = report.get("uploadFile") or {}
    url = upload.get("url") or report.get("actionLink")
    if not url:
        return None
    return urljoin(BASE_URL, url)


def list_files(fy_filter: tuple[str, ...] = FISCAL_YEARS) -> Iterable[DisclosureFile]:
    data = _fetch_next_data()
    buckets = _find_fy_buckets(data)

    found_any_bucket = False
    for bucket in buckets:
        attrs = bucket.get("attributes", {})
        fy = attrs.get("title")
        if fy not in fy_filter:
            continue
        found_any_bucket = True

        for report in attrs.get("reports") or []:
            quarters = (report.get("quaterlyYears") or {}).get("data") or []
            quarter_tag = quarters[0]["attributes"]["title"] if quarters else None
            if not quarter_tag or not quarter_tag.upper().startswith("Q4"):
                continue  # annual (year-end) filings only, per MVP scope

            title = (report.get("title") or "").strip().lower()
            if "disclosure" not in title:
                continue  # excludes Annual Report/Return, director notices, etc.

            url = _report_url(report)
            if url is None:
                continue  # metadata row with no attached file in the CMS

            yield DisclosureFile(
                insurer=insurer,
                fy=fy,
                form_hint=None,
                url=url,
                mime=guess_mime(url),
                label=(report.get("title") or "").strip(),
            )

    if not found_any_bucket:
        raise DiscoveryError(
            f"None of the requested fiscal years {fy_filter} were found among "
            f"the fiscal-year buckets on {DISCLOSURE_PAGE} — the year labels "
            "or JSON structure likely changed."
        )
