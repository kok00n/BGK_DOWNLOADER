-- BGK Auction Results - per-auction details from komunikaty PDFs.
--
-- Source: https://www.bgk.pl/.../komunikaty/ - one PDF per auction day,
-- typically covering 3-4 series tapped on the same day. Each PDF has
-- two result sections that we merge into one row per (auction_date, series):
--   * Wyniki przetargu sprzedaży   (main auction: demand/sale/stop_price/yield)
--   * Wyniki sprzedaży dodatkowej  (top-up: sale_price + additional_sale_mln)
--
-- Naming convention: bgk_* prefix (we share the Supabase project with
-- CETO_DOWNLOADER's bondspot_* tables and the BGK XLSX table bgk_auctions).
--
-- Scope: BGK_DOWNLOADER focuses on FPC PLN series only; this table will
-- store FPC PLN rows. The parser filters on series LIKE 'FPC%' upstream
-- (USD/JPY/FWA series live in their own series codes and are skipped).
--
-- All percent fields stored as percent (5.044, 101.72) to match
-- bgk_auctions.yield_pct/price_pct convention.

DROP VIEW IF EXISTS v_bgk_auction_metrics;

CREATE OR REPLACE FUNCTION bgk_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
--  BGK_AUCTION_RESULTS - per-series per-auction result row
-- =====================================================================
CREATE TABLE IF NOT EXISTS bgk_auction_results (
    auction_date           DATE         NOT NULL,
    series                 VARCHAR(32)  NOT NULL,
    isin                   VARCHAR(12),
    maturity_date          DATE,

    -- Main auction (sprzedaż główna)
    demand_total_mln       NUMERIC(14,3),                -- Popyt łączny
    demand_nc_mln          NUMERIC(14,3),                -- w tym oferty NK
    sold_total_mln         NUMERIC(14,3),                -- Sprzedaż łączna
    sold_nc_mln            NUMERIC(14,3),                -- w tym oferty NK
    stop_price             NUMERIC(10,4),                -- Cena minimalna - % of face (e.g. 101.72)
    yield_pct              NUMERIC(10,4),                -- Rentowność - % (NULL for floaters)
    reduction_rate_pct     NUMERIC(8,4),                 -- Stopa redukcji
    reduction_rate_nc_pct  NUMERIC(8,4),                 -- Stopa redukcji ofert NK

    -- Top-up (sprzedaż dodatkowa) - same row, separate columns
    additional_sale_price  NUMERIC(10,4),                -- typically = stop_price
    additional_sale_mln    NUMERIC(14,3),                -- 0 if no top-up

    -- Stan po rozliczeniu i meta
    outstanding_after_mln  NUMERIC(14,3),                -- Zadłużenie po rozliczeniu
    accrued_interest       NUMERIC(10,4),

    source_pdf_url         TEXT,
    inserted_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    PRIMARY KEY (auction_date, series)
);

CREATE INDEX IF NOT EXISTS idx_bgk_auction_results_date
    ON bgk_auction_results(auction_date DESC);
CREATE INDEX IF NOT EXISTS idx_bgk_auction_results_series
    ON bgk_auction_results(series, auction_date DESC);
CREATE INDEX IF NOT EXISTS idx_bgk_auction_results_isin
    ON bgk_auction_results(isin, auction_date DESC);

DROP TRIGGER IF EXISTS trg_bgk_auction_results_updated_at ON bgk_auction_results;
CREATE TRIGGER trg_bgk_auction_results_updated_at
    BEFORE UPDATE ON bgk_auction_results
    FOR EACH ROW
    EXECUTE FUNCTION bgk_set_updated_at();

-- =====================================================================
--  VIEW: derived auction metrics (B/C, NK shares, total taken vs offered)
--  Concession vs POLGB curve will land in Milestone B (separate view that
--  joins to bondspot_fixing - currently in CETO_DOWNLOADER project).
-- =====================================================================
CREATE OR REPLACE VIEW v_bgk_auction_metrics AS
SELECT
    r.*,
    -- Total taken (main + additional). Sprzedaż dodatkowa goes at the
    -- same stop_price so the marginal yield is the same.
    COALESCE(r.sold_total_mln, 0) + COALESCE(r.additional_sale_mln, 0)
        AS total_taken_mln,

    -- Bid-to-cover: total demand / accepted (main only - top-up has no
    -- competitive bid mechanic). >2 = strong demand.
    CASE WHEN COALESCE(r.sold_total_mln, 0) > 0
         THEN r.demand_total_mln / r.sold_total_mln END
        AS bid_to_cover,

    -- Allocation rate: how much of demand got filled.
    CASE WHEN COALESCE(r.demand_total_mln, 0) > 0
         THEN r.sold_total_mln / r.demand_total_mln END
        AS allocation_rate,

    -- NC share of total demand and sale.
    CASE WHEN COALESCE(r.demand_total_mln, 0) > 0
         THEN r.demand_nc_mln / r.demand_total_mln END
        AS nc_share_demand,
    CASE WHEN COALESCE(r.sold_total_mln, 0) > 0
         THEN r.sold_nc_mln / r.sold_total_mln END
        AS nc_share_sold,

    -- Additional sale as % of main sale (signals leftover NC demand).
    CASE WHEN COALESCE(r.sold_total_mln, 0) > 0
         THEN r.additional_sale_mln / r.sold_total_mln END
        AS additional_share_of_main
FROM bgk_auction_results r;
