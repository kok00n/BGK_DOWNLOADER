# BGK Dashboard - kontekst projektu (handoff z CETO_DOWNLOADER)

Ten dokument przenosi decyzje + recon z sesji w `CETO_DOWNLOADER`
do **nowego repo** dla dashboardu obligacji BGK (Bank Gospodarstwa
Krajowego). W CETO_DOWNLOADER zostaje POLGB dashboard.

## Jak używać tego pliku

1. Utworzyć nowe repo (np. `BGK_DOWNLOADER`), w tym samym katalogu
   nadrzędnym co `CETO_DOWNLOADER`.
2. Skopiować ten plik do roota nowego repo jako `BGK_HANDOFF.md`.
3. W pierwszej sesji Claude Code w nowym repo — powiedzieć "przeczytaj
   BGK_HANDOFF.md i zacznij od Milestone A".
4. Claude w nowym repo będzie miał pełen kontekst: scope, decyzje, wzorce
   do reuse'u, gotchas, otwarte pytania.

---

## Cel projektu

Równoległy dashboard dla aukcji obligacji BGK, struktura podobna do
istniejącego POLGB (`CETO_DOWNLOADER/notebooks/bondspot_dashboard.ipynb`)
ale z adaptacjami pod sub-sovereign emitenta:
- **Spread vs POLGB curve** zamiast concession vs prior auction
- BGK nie ma B/C/NK/demand w głównym dataset (XLSX) - to wymaga
  dodatkowego scrapera PDF-ów z komunikatów

## Źródła danych

### 1. XLSX "Baza obligacji" (główne źródło, kompletne historycznie)
URL: `https://www.bgk.pl/files/public/Pliki/informacje/Emisje_obligacji_BGK/Statystyka/Baza_obligacji_strona_internetowa_DD.MM.YYYY.xlsx`

**Uwaga**: data w nazwie pliku - aktualizowana co tydzień/dwa. Trzeba
najpierw scrapować stronę indeksu żeby znaleźć aktualny link:
`https://www.bgk.pl/dla-klienta/relacje-inwestorskie/emisje-obligacji-bgk/statystyka/`

**Struktura** (potwierdzona, 365 wierszy × 12 useful kolumn, dane od 2009):
| Col | Polish | English | Notes |
|---|---|---|---|
| 0 | Seria | Series | np. IPS1014, IDS1018 |
| 1 | Data emisji | Issue date | datetime |
| 2 | Data wykupu | Maturity date | datetime |
| 3 | ISIN | ISIN | PL000... |
| 4 | Typ transakcji | Transaction type | KFD (Krajowy Fundusz Drogowy), FPC (Fundusz Przeciwdziałania COVID), inne |
| 5 | Lata do wykupu | Years to maturity | INT (przy emisji) |
| 6 | Oprocentowanie | Coupon type | stałe/fixed, zmienne/floating |
| 7 | Kupon | Coupon | DECIMAL (0.0575 = 5.75%) |
| 8 | Waluta | Currency | PLN (głównie), EUR |
| 9 | Wartość emisji | Issue amount | nominal w walucie emisji |
| 10 | Cena | Price | DECIMAL (0.98901 = 98.901%) |
| 11 | Rentowność | Yield | DECIMAL (0.06008 = 6.008%) |

**Kluczowe braki vs POLGB:**
- Brak bid-to-cover
- Brak NK (non-competitive) split
- Brak demand_total / demand_nc
- Brak offer_amount (tylko sold)
- Brak concession (zastąpione spreadem vs POLGB - liczonym z naszych danych)

### 2. Komunikaty page (lista PDF-ów z wynikami szczegółowymi)
URL: `https://www.bgk.pl/dla-klienta/relacje-inwestorskie/emisje-obligacji-bgk/komunikaty/`

- Statyczny HTML, tabela z kolumnami: data przetargu, seria, link do PDF
- Każdy PDF = wyniki konkretnej aukcji (zawiera B/C, popyt, sold)
- ~30 aukcji/rok
- **Ryzyko**: PDF layouts mogą się różnić między latami - parser fragile

### 3. Harmonogram nadchodzących aukcji
Ta sama strona komunikaty zawiera tylko "data + (puste seria/wolumen)"
dla planowanych aukcji. Mało użyteczne dla LLM context, niski priorytet.

## Spread vs POLGB - jak liczyć

**Decyzja użytkownika**: linear interpolation z POLGB curve zbudowanej
z **fixingów BondSpot** (które są w tabeli `bondspot_fixing` w starym repo).

**Algorytm**:
1. Dla daty aukcji BGK D i tenoru T (years to maturity):
2. Zbuduj POLGB curve dla daty D: dla każdej aktywnej POLGB obligacji
   na D, oblicz (years_to_maturity_on_D, fixing_yield_on_D)
3. Linear interpolacja na T → otrzymujesz POLGB equivalent yield
4. Spread BGK = bgk_yield_at_issuance - polgb_curve(T, D) → w bp

**Reuse**: schema bondspot_fixing, bond_specs, view typu
v_bondspot_full_weighted są w starym repo - można pożyczyć
strukturę lub odwołać się do nich przez foreign reference.

**Alternatywa do rozważenia**: jeśli BGK aukcja jest weekendowa /
bez fresh fixings, fallback do D-1 lub interpolacja czasowa.

## Architektura - wzorzec do reuse'u z POLGB

### Schemas (w CETO_DOWNLOADER, do skopiowania/adaptacji)
- `bond_auctions_schema.sql` - per-aukcja per-leg z polami popytu/podaży
  → BGK ma tylko subset, ale wzorzec PRIMARY KEY (date, isin) jest dobry
- `bond_outstanding_schema.sql` - punkty zmian salda per ISIN + view
  v_bondspot_full_weighted (LATERAL joins, functional indexes pod
  COALESCE(auction_date, change_date))
- `reference_rates_schema.sql` - jak trzymać multi-series rate data
  (WIBOR, POLSTR, FX, yields) w jednej tabeli + view per snapshot
- `swap_curve_schema.sql` - jak trzymać daily curve snapshot
  (rate_date PK, tenor PK)
- `llm_commentary_schema.sql` - **REUSE BEZ ZMIAN**: section field
  pozwala na 'bgk_chart_1', 'bgk_final_report' obok 'polgb_*'

### Scrapers (w CETO_DOWNLOADER/scripts)
- `refresh_reference_rates.py` - wzorzec stooq scraper z apikey
- `refresh_swap_curve.py` - wzorzec scrapera Next.js SSR (bluegamma)
  → BGK XLSX scraper będzie prostszy: requests + openpyxl
- `lib/supabase.py` - upsert helper, można skopiować 1:1

### Notebook (`bondspot_dashboard.ipynb` w CETO_DOWNLOADER)
Struktura 17 sekcji + LLM commentary + final report:
1. Portfolio metryki w czasie
2. Per coupon bucket
3. Skład długu (stacked area)
4. Skład długu (% per bucket)
5. Maturity ladder
6-12. Aukcyjne metryki (B/C, concession, NK, tail, scorecard, scatters)
13-16. Trendy + funding pace
17. LLM final report

**Dla BGK**:
- Sekcje 6-13 (B/C i pochodne) - **wymagają PDF parsera** żeby
  funkcjonowały. Bez PDF: tylko chart 14 (scatter spread vs tenor itp.)
- Sekcje 1-5 + 16 (struktura/ladder/funding) - **działają z samego XLSX**
- Final report - **działa**, LLM dostaje grounded macro + portfolio

### LLM commentary - **REUSE INFRA**
- Tabela `llm_commentary` z polem `section` (np. 'bgk_chart_1',
  'bgk_final_report')
- Funkcje w setup cell notebooka:
  - `_fetch_commentary_history(section, limit)` - historia per sekcja
  - `_save_commentary(section, chart_name, prompt, response, ...)`
  - `llm_chart_commentary(...)` - skraca prompt + zapis + return markdown
  - `llm_final_report(context)` - finalny raport
  - `fetch_macro_snapshot()` + `format_macro_block(ctx)` - grounded macro
    (mamy już PL10Y/DE10Y/US10Y/WIBOR/FX/CPI + PLN swap curve)
- **System prompt** zawiera już twarde zakazy halucynacji macro - skopiować
- **Model**: claude-opus-4-7 (Anthropic API, ANTHROPIC_API_KEY env)

### Workflows (`.github/workflows`)
- `refresh_evening.yml` - MF refresh + compute analytics (cron Mon-Thu 18:00 UTC)
- `refresh_reference_rates.yml` - stooq + bluegamma daily
- `render_dashboard.yml` - workflow_run trigger po refresh - nbconvert do HTML → GH Pages

**Dla BGK**:
- `refresh_bgk_xlsx.yml` - daily / weekly cron
- `refresh_bgk_pdfs.yml` - osobny job po wykryciu nowych komunikatów
- `render_bgk_dashboard.yml` - workflow_run po refresh_bgk_xlsx
- **GitHub Pages**: użytkownik wybrał osobny site → osobne repo
  potrzebuje GH Pages config z gh-pages branch

## Plan implementacji - milestone'y

### A. BGK data layer (FIRST)
- Scraper strony statystyka: znajdź aktualny URL XLSX
- Pobierz + parse openpyxl
- Schema `bgk_auctions(rate_date, isin, ...)` mirror struktury XLSX
- Aggregation view `bgk_outstanding_at(date)` z delta z amount
- Workflow refresh daily
- Test E2E: workflow → tabela → manual SELECT

### B. POLGB curve interpolation
- View `v_polgb_curve_at(date)` budowany z bondspot_fixing + bond_specs
  (years_to_maturity = (maturity - date) / 365.25)
- SQL function `polgb_yield_interp(p_date, p_years)` linear interp
- View `v_bgk_with_polgb_spread` LEFT JOIN do interp function

### C. PDF parser (drugi commit)
- Scrape komunikaty page → lista (data, seria, pdf_url)
- Download + pdftotext (lub pdfplumber)
- Parse: extract B/C, demand, sold per seria
- Schema `bgk_auction_results(date, seria, bid_cover, demand_total, ...)`
- Idempotentny upsert

### D. Notebook
- `notebooks/bgk_dashboard.ipynb` - kopia struktury bondspot_dashboard.ipynb
- Adaptacje:
  - Sekcje 1-5, 16, 17 → bezpośredni mapping
  - Sekcje 6-13 → uzależnione od B/C z PDF parsera (jeśli brak, ukryć)
  - Sekcja "spread vs POLGB" → nowa (chart per tenor, time series)
- LLM commentary - section='bgk_*'
- Final report - dodać do macro block: BGK outstanding total + ostatni spread

### E. Deploy
- Osobny GH Pages site (drugie repo lub subpath)
- Decision: jeśli osobne repo, secret SUPABASE_URL/KEY/ANTHROPIC_API_KEY
  muszą być dodane do nowego repo (jako GitHub Secrets)

## Otwarte decyzje / pytania do siebie w nowej sesji

1. **GitHub Pages target**: drugie repo (czyściej) vs subpath na CETO_DOWNLOADER (mniej overhead)?
2. **Supabase**: ten sam project (tabele bgk_*) czy osobny project? Argumenty:
   - Ten sam: shared `llm_commentary` table, dostęp do bondspot_fixing do
     interpolacji POLGB curve bez duplikacji danych
   - Osobny: clean separation, ale duplikacja POLGB fixing dla interp
   - **Rekomendacja**: ten sam project, schema z prefiksem bgk_*
3. **PDF parser tooling**: pdfplumber vs pdftotext (Poppler) vs PyPDF2?
   - pdfplumber: dobre dla tabel, czysty Python
   - pdftotext: szybsze, wymaga system poppler binary
4. **XLSX URL discovery**: weekly cron vs scraper strony statystyka per
   refresh - first option prostszy, second elastyczniejszy

## Komendy startowe w nowej sesji

```bash
# 1. Utwórz nowe repo
mkdir BGK_DOWNLOADER && cd BGK_DOWNLOADER
git init

# 2. Skopiuj handoff
cp ../CETO_DOWNLOADER/bgk_handoff.md ./BGK_HANDOFF.md

# 3. Otwórz w Claude Code → poproś o przeczytanie BGK_HANDOFF.md
#    i zaczęcie od Milestone A

# 4. Setup workspace
cp ../CETO_DOWNLOADER/requirements.txt ./
cp -r ../CETO_DOWNLOADER/scripts/lib ./scripts/lib  # supabase upsert helper
```

## Dane sesji historycznej

Pełny transkrypt sesji w której zapadły te decyzje:
- Lokalizacja: `C:\Users\kamil\.claude\projects\c--Users-kamil-OneDrive-Pulpit-Trading-CETO-DOWNLOADER\`
- Najbardziej relevantne pliki w CETO_DOWNLOADER do reuse'u:
  - `notebooks/bondspot_dashboard.ipynb` - setup cell (LLM helpers + macro block)
  - `llm_commentary_schema.sql`
  - `bond_auctions_schema.sql`
  - `scripts/refresh_swap_curve.py` - wzorzec parsera Next.js
  - `scripts/lib/supabase.py`
