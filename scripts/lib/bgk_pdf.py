"""BGK auction-results PDF parser.

Source: https://www.bgk.pl/.../komunikaty/ - one PDF per auction day,
filename pattern `Wyniki_sprzeda{żX,zy}_dodatkowej_DD.MM.YYYY.pdf` (Polish
characters URL-encoded in modern paths, ASCII-folded in 2023-2024 paths,
under different subdirs for 2020-2022 vintage).

Each PDF is an Excel-exported document with 3 conceptual tables that
pdfplumber detects as a single 9-column grid:

  Section A - "Komunikat o przetargu ..."   (announcement, skipped)
  Section B - "Wyniki przetargu sprzedaży"  (main auction results)
  Section C - "Wyniki sprzedaży dodatkowej" (additional sale / top-up)

Section B is the gold: per series, two stacked grid rows -
  row 1 = (demand_total, sold_total, stop_price, reduction_rate, outstanding)
  row 2 = (demand_nc,    sold_nc,    yield_pct,  reduction_rate_nc)

Section C is one row per series with (cena_sprzedaży, sprzedaż_mln).

We pair them by (auction_date, series). Filter to series LIKE 'FPC%'
upstream - USD/JPY/FWA series are out of FPC PLN scope.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from io import BytesIO

import pdfplumber


# Polish number format: '.' = thousands separator, ',' = decimal point.
# Trailing '%' allowed (we strip and return the raw percent value, so
# '5,044%' -> 5.044). '-' or empty/None -> None.
def parse_pl_number(s) -> float | None:
    if s is None:
        return None
    text = str(s).strip()
    if not text or text == "-":
        return None
    text = text.rstrip("%").strip()
    # Remove thousand separators (dot), then map decimal comma to dot.
    text = text.replace(" ", "").replace("\xa0", "")
    text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _stop_price_to_pct(raw) -> float | None:
    """PDF reports stop price as PLN per 1000 nominal (e.g. '1.017,20').
    Normalise to percent-of-face to match bgk_auctions.price_pct convention
    (101.72 means 101.72%).
    """
    v = parse_pl_number(raw)
    return v / 10.0 if v is not None else None


def _parse_date(s) -> date | None:
    if not s:
        return None
    text = str(s).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


BGK_BASE = "https://www.bgk.pl"
KOMUNIKATY_PAGE = (
    f"{BGK_BASE}/dla-klienta/relacje-inwestorskie/emisje-obligacji-bgk/komunikaty/"
)

_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def date_from_pdf_url(url: str) -> date | None:
    """Extract DD.MM.YYYY from a komunikaty PDF URL. Returns None if absent."""
    m = _DATE_RE.search(url)
    if not m:
        return None
    return datetime.strptime(f"{m.group(1)}.{m.group(2)}.{m.group(3)}", "%d.%m.%Y").date()


def parse_komunikaty_listing(html: str) -> list[dict]:
    """Find auction-results PDFs on the komunikaty page.

    Strategy: BeautifulSoup over rendered HTML, every <a href="*.pdf">
    that is a results doc (i.e. NOT an "Informacja_o_przetargu" pre-auction
    announcement). For each link, derive auction_date from the filename
    if it has DD.MM.YYYY, else walk up to the nearest enclosing <tr>/<li>
    and grep the visible text for a date - covers the one 17.06.2020
    legacy URL ("Komunikat_FPC0630_-_wyniki...") that lacks a date in
    its filename.

    Returns dedupe'd list of {auction_date: date, pdf_url: str}.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=re.compile(r"\.pdf$", re.IGNORECASE)):
        href = a.get("href", "")
        if not href:
            continue
        # Skip pre-auction announcement PDFs - no result data inside.
        if "Informacja_o_przetargu" in href:
            continue
        url = BGK_BASE + href if href.startswith("/") else href
        if url in seen:
            continue
        seen.add(url)

        auction_date = date_from_pdf_url(url)
        if auction_date is None:
            # Fallback: look at the enclosing element's visible text.
            parent = a.find_parent("tr") or a.find_parent("li") or a.parent
            if parent:
                m = _DATE_RE.search(parent.get_text(" ", strip=True))
                if m:
                    auction_date = datetime.strptime(
                        f"{m.group(1)}.{m.group(2)}.{m.group(3)}", "%d.%m.%Y"
                    ).date()
        if auction_date is None:
            # No way to date this PDF - skip rather than guess.
            continue

        out.append({"auction_date": auction_date, "pdf_url": url})
    return out


def _cell(grid, row, col):
    """Safe grid access - returns None for out-of-range or empty cells."""
    if row < 0 or row >= len(grid) or col < 0 or col >= len(grid[row]):
        return None
    return grid[row][col]


def _find_cell(grid: list[list], keyword: str, min_col: int = 0) -> tuple[int, int] | None:
    """Return (row, col) of the first cell containing `keyword` at col >= min_col.

    The 2020-08-26 PDF squishes all column headers into a stray col-0 cell
    in addition to the real per-column header cells, so a naive search
    finds the wrong 'Popyt'. Caller passes min_col >= 3 to skip the
    stray, since 'Popyt' as a column anchor must be preceded by Seria +
    Maturity + ISIN (cols 0/1/2 at minimum).
    """
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            if c < min_col:
                continue
            if cell and keyword in str(cell):
                return (r, c)
    return None


def _parse_main_section(grid: list[list], header_row: int, popyt_col: int) -> dict[str, dict]:
    """Parse the main auction-results section starting after `header_row`.

    Column layout is anchored on the 'Popyt' header cell - PDFs of
    different vintages shift the grid left/right by 1 column (the
    2025-2026 vintage adds an empty leading column), but the relative
    offsets stay constant:
        series_col   = popyt_col - 3
        maturity_col = popyt_col - 2
        isin_col     = popyt_col - 1
        demand_col   = popyt_col
        sale_col     = popyt_col + 1
        price_col    = popyt_col + 2   (also yield in the NC row)
        red_col      = popyt_col + 3
        outstanding  = popyt_col + 4

    Stops when it hits a 'Razem' / 'Total' row, an empty section break,
    or a row whose first non-None cell looks like the next section header.
    """
    series_col, maturity_col, isin_col = popyt_col - 3, popyt_col - 2, popyt_col - 1
    sale_col, price_col, red_col, outstanding_col = (
        popyt_col + 1, popyt_col + 2, popyt_col + 3, popyt_col + 4,
    )

    out: dict[str, dict] = {}
    i = header_row + 1
    while i < len(grid):
        series_cell = _cell(grid, i, series_col)
        # Stop conditions: Razem/Total marker, or any cell announcing
        # the next section ("Komunikat o sprzedaży dodatkowej",
        # "Wyniki sprzedaży dodatkowej", etc.).
        if series_cell and "Razem" in str(series_cell):
            break
        row_text = " ".join(str(c) for c in grid[i] if c)
        if any(marker in row_text for marker in (
            "sprzedaży dodatkowej", "sprzedazy dodatkowej",
            "o przetargu sprzeda", "Wyniki sprzeda",
        )):
            break
        # Skip blank / continuation rows.
        if not series_cell or not isinstance(series_cell, str) or not series_cell.strip():
            i += 1
            continue
        series = series_cell.strip()
        # Defensive: only accept rows whose series looks like an FPC* code
        # (avoids accidentally picking up a header or label row).
        if not _looks_like_series(series):
            i += 1
            continue
        out[series] = {
            "series":               series,
            "maturity_date":        _parse_date(_cell(grid, i, maturity_col)),
            "isin":                 (str(_cell(grid, i, isin_col)).strip()
                                     if _cell(grid, i, isin_col) else None),
            "demand_total_mln":     parse_pl_number(_cell(grid, i, popyt_col)),
            "demand_nc_mln":        parse_pl_number(_cell(grid, i + 1, popyt_col)),
            "sold_total_mln":       parse_pl_number(_cell(grid, i, sale_col)),
            "sold_nc_mln":          parse_pl_number(_cell(grid, i + 1, sale_col)),
            "stop_price":           _stop_price_to_pct(_cell(grid, i, price_col)),
            "yield_pct":            parse_pl_number(_cell(grid, i + 1, price_col)),
            "reduction_rate_pct":   parse_pl_number(_cell(grid, i, red_col)),
            "reduction_rate_nc_pct": parse_pl_number(_cell(grid, i + 1, red_col)),
            "outstanding_after_mln": parse_pl_number(_cell(grid, i, outstanding_col)),
        }
        i += 2  # consume both rows of this series
    return out


def _parse_additional_section(grid: list[list], header_row: int, cena_col: int) -> dict[str, dict]:
    """Parse the additional-sale section.

    Anchored on 'Cena sprzedaży' column position:
        series_col   = cena_col - 4
        maturity_col = cena_col - 3
        isin_col     = cena_col - 2
        accrued_col  = cena_col - 1
        price_col    = cena_col
        sold_col     = cena_col + 2   (col +1 is an empty separator)
    """
    # maturity / isin live at cena_col-3 / cena_col-2 in the source PDF but
    # we don't read them here - they're already filled in by the main section.
    series_col = cena_col - 4
    accrued_col = cena_col - 1
    sold_col = cena_col + 2

    out: dict[str, dict] = {}
    i = header_row + 1
    while i < len(grid):
        series_cell = _cell(grid, i, series_col)
        if series_cell and "Razem" in str(series_cell):
            break
        if not series_cell or not isinstance(series_cell, str) or not series_cell.strip():
            i += 1
            continue
        series = series_cell.strip()
        if not _looks_like_series(series):
            i += 1
            continue
        out[series] = {
            "accrued_interest":      parse_pl_number(_cell(grid, i, accrued_col)),
            "additional_sale_price": _stop_price_to_pct(_cell(grid, i, cena_col)),
            "additional_sale_mln":   parse_pl_number(_cell(grid, i, sold_col)),
        }
        i += 1
    return out


_SERIES_RE = re.compile(r"^[A-Z]{2,4}\d{3,4}$")


def _looks_like_series(s: str) -> bool:
    """True for codes like FPC0229, KFD0529, FWA0928 (uppercase prefix + digits)."""
    return bool(_SERIES_RE.match(s.strip()))


def parse_pdf(pdf_bytes: BytesIO, source_url: str,
              auction_date: date | None = None) -> list[dict]:
    """Parse a BGK auction-results PDF into per-series records.

    auction_date is taken from the filename if not provided.
    Returns one dict per series, with main + additional fields merged.
    Series not appearing in the main-auction section are dropped (we
    don't store "announcement-only" rows without results).
    """
    if auction_date is None:
        auction_date = date_from_pdf_url(source_url)
    if auction_date is None:
        raise RuntimeError(
            f"Could not infer auction_date from URL {source_url!r}; "
            f"pass auction_date explicitly."
        )

    with pdfplumber.open(pdf_bytes) as pdf:
        if not pdf.pages:
            raise RuntimeError(f"PDF has no pages: {source_url}")
        # All sections of interest live on page 1 of these one-pagers.
        # If BGK ever ships a multi-page PDF we'll need to concatenate.
        tables = pdf.pages[0].extract_tables()

    if not tables:
        raise RuntimeError(f"pdfplumber found no tables in {source_url}")
    # The Excel-export PDFs ship one big 9-column grid containing all 3
    # sections; if that ever changes we'll pick the largest table.
    grid = max(tables, key=len)

    # Section B: anchor on the 'Popyt' header cell wherever it lives
    # (col 3 in 2020-2024 vintages, col 4 in 2025-2026). min_col=3 skips
    # the stray "squished all headers" cell at col 0 seen in 2020-08-26.
    popyt_loc = _find_cell(grid, "Popyt", min_col=3)
    if popyt_loc is None:
        raise RuntimeError(
            f"PDF {source_url}: could not locate 'Popyt' header cell "
            f"(grid has {len(grid)} rows; expected results table)"
        )
    main_hdr_row, popyt_col = popyt_loc
    main_results = _parse_main_section(grid, main_hdr_row, popyt_col)

    # Section C: anchor on 'Cena sprzedaży' header cell. Search only AFTER
    # the main-auction section to skip the same-named announcement field
    # in section A, and require col >= 4 to skip stray-squished cells.
    additional_results: dict[str, dict] = {}
    for r in range(main_hdr_row + 1, len(grid)):
        for c, cell in enumerate(grid[r]):
            if c < 4:
                continue
            if cell and "Cena sprzeda" in str(cell):
                additional_results = _parse_additional_section(grid, r, c)
                break
        if additional_results:
            break

    out: list[dict] = []
    for series, main in main_results.items():
        record = {
            "auction_date":  auction_date.isoformat(),
            **{k: (v.isoformat() if isinstance(v, date) else v) for k, v in main.items()},
            "additional_sale_price": None,
            "additional_sale_mln":   None,
            "accrued_interest":      None,
            "source_pdf_url":        source_url,
        }
        if series in additional_results:
            record.update(additional_results[series])
        out.append(record)
    return out
