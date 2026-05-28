-- BGK vs POLGB spread - Milestone B.
--
-- Joins bgk_auction_results (per-auction yields/prices for BGK FPC PLN
-- tranches) against the POLGB nominal yield curve built from CETO's
-- bondspot_fixing + bond_specs tables, computes per-auction spread in
-- basis points.
--
-- DEPENDENCY: this schema requires bondspot_fixing and bond_specs to
-- exist in the same Supabase project. They live in CETO_DOWNLOADER and
-- we share the project (handoff decision 2). If they are not present,
-- the functions/views below will fail at first call with
-- "relation does not exist".
--
-- Algorithm (mirrors CETO v_auction_with_market_context for consistency):
--
--   For each BGK auction at date D, tenor T = (maturity - D) / 365.25:
--   1. Build POLGB curve at D using fixed-coupon + zero-coupon bonds
--      (bond_specs.coupon_kind IN ('S','O')) - their nominal YTM is
--      directly comparable to BGK's yield_pct.
--   2. Per-POLGB-bond fixing pick (LATERAL): prefer same-day session 1
--      (~11:00 UTC, before BGK auction result publication at ~11:30),
--      else any previous-day session (sesja 2 EOD of D-1 has the best
--      data freshness). NEVER use same-day sesja 2 - that's published
--      AFTER BGK results, so it leaks.
--   3. Linear interpolation on tenor.
--   4. For fixed-coupon BGK bonds: spread_bp = (bgk_yield - polgb_interp_yield) * 100
--   5. For floater BGK bonds (bgk_auctions.coupon_kind = 'zmienne'):
--      compute implied DM = (POWER(100/price, 365.25/days) - 1) * 100
--      both sides, then spread_dm_bp. Crucially, the POLGB side uses
--      the **WZ curve** (coupon_kind='Z' POLGB floaters), NOT the
--      fixed-coupon curve - apples-to-apples since BGK FPC floaters
--      are WIBOR/POLSTR-referenced just like POLGB WZ. CETO uses the
--      same trick for MF WZ/NZ floaters - the implied-DM formula
--      ignores reset cashflows but the bias cancels when both sides
--      are computed identically.

-- Drop dependent views FIRST so the subsequent function drops don't error
-- with "cannot drop function because other objects depend on it". Both
-- spread views reference polgb_*_interp.
--
-- Older deploys may have these as regular views; newer ones as materialised
-- views. `DROP {VIEW|MATERIALIZED VIEW} IF EXISTS` raises 42809 if the
-- object exists as the wrong kind, so we check pg_class first and dispatch.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class
               WHERE relname = 'v_bgk_issuance_spread' AND relkind = 'm') THEN
        DROP MATERIALIZED VIEW v_bgk_issuance_spread CASCADE;
    ELSIF EXISTS (SELECT 1 FROM pg_class
                  WHERE relname = 'v_bgk_issuance_spread' AND relkind = 'v') THEN
        DROP VIEW v_bgk_issuance_spread CASCADE;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class
               WHERE relname = 'v_bgk_auction_spread' AND relkind = 'm') THEN
        DROP MATERIALIZED VIEW v_bgk_auction_spread CASCADE;
    ELSIF EXISTS (SELECT 1 FROM pg_class
                  WHERE relname = 'v_bgk_auction_spread' AND relkind = 'v') THEN
        DROP VIEW v_bgk_auction_spread CASCADE;
    END IF;
END $$;

DROP FUNCTION IF EXISTS bgk_refresh_spreads();
DROP FUNCTION IF EXISTS polgb_floater_dm_interp(DATE, NUMERIC);
DROP FUNCTION IF EXISTS polgb_dm_interp(DATE, NUMERIC);
DROP FUNCTION IF EXISTS polgb_yield_interp(DATE, NUMERIC);
DROP FUNCTION IF EXISTS polgb_curve_at(DATE);

-- =====================================================================
--  FUNCTION: POLGB curve points at an arbitrary date - ALL bond types.
--  One row per active POLGB bond on p_date, with its latest leakage-safe
--  fixing. Returns both coupon_kind and bond_type so callers can filter:
--    coupon_kind 'S' / 'O', bond_type any  -> nominal yield curve
--    bond_type 'WZ' (WIBOR floater)        -> WIBOR-based DM curve
--    bond_type 'NZ' (POLSTR floater)       -> POLSTR-based DM curve, separate
--    coupon_kind 'I'                       -> inflation-linked, real-yield space
--
--  WZ and NZ live in DIFFERENT discount-margin spaces (WIBOR vs POLSTR
--  benchmarks) - mixing them in one curve biases the result. BGK FPC
--  floaters reference WIBOR, so spread should be computed vs WZ curve only.
-- =====================================================================
CREATE OR REPLACE FUNCTION polgb_curve_at(p_date DATE)
RETURNS TABLE (
    polgb_isin     VARCHAR(12),
    coupon_kind    CHAR(1),
    bond_type      TEXT,
    fixing_date    DATE,
    fixing_session SMALLINT,
    tenor_years    NUMERIC,
    fixing_yield   NUMERIC,
    fixing_price   NUMERIC,
    implied_dm_pct NUMERIC
)
LANGUAGE sql STABLE AS $$
    SELECT
        b.isin AS polgb_isin,
        b.coupon_kind,
        b.bond_type,
        fx.fixing_date,
        fx.fixing_session,
        (b.maturity_date - p_date)::NUMERIC / 365.25 AS tenor_years,
        fx.fixing_yield,
        fx.fixing_price,
        -- Implied DM = zero-coupon-equivalent yield back-solved from clean price.
        -- Meaningful for WZ floaters (relative measure of cheapness vs par),
        -- noisy for fixed-coupon bonds (since they have a real coupon).
        CASE
            WHEN fx.fixing_price IS NOT NULL AND fx.fixing_price > 0
                 AND b.maturity_date > p_date
            THEN (POWER(100.0 / fx.fixing_price,
                        365.25 / (b.maturity_date - p_date)) - 1.0) * 100.0
        END AS implied_dm_pct
    FROM bond_specs b
    LEFT JOIN LATERAL (
        -- Newest fixing per POLGB ISIN that is leakage-safe vs an auction
        -- announced ~11:30 on p_date: same-day sesja 1 OR previous-day any.
        SELECT f.fixing_date, f.fixing_session, f.fixing_yield, f.fixing_price
        FROM bondspot_fixing f
        WHERE f.isin = b.isin
          AND (
              (f.fixing_date = p_date AND f.fixing_session = 1)
              OR f.fixing_date < p_date
          )
        ORDER BY f.fixing_date DESC, f.fixing_session DESC
        LIMIT 1
    ) fx ON TRUE
    WHERE b.coupon_kind IN ('S', 'O', 'Z')   -- fixed + zero + floaters
      AND b.maturity_date > p_date
      -- Need either a yield quote (S/O for nominal curve) OR a price
      -- (Z floaters for DM curve - BondSpot doesn't quote YTM for WZ).
      AND (fx.fixing_yield IS NOT NULL OR fx.fixing_price IS NOT NULL);
$$;

-- =====================================================================
--  FUNCTION: linear-interpolate POLGB yield curve at a target tenor.
--  Returns NULL if curve is empty or the target falls outside the
--  curve's bracket (we deliberately don't extrapolate to keep spreads
--  honest at the long end).
-- =====================================================================
CREATE OR REPLACE FUNCTION polgb_yield_interp(p_date DATE, p_tenor_years NUMERIC)
RETURNS NUMERIC
LANGUAGE sql STABLE AS $$
    WITH curve AS (
        SELECT tenor_years, fixing_yield
        FROM polgb_curve_at(p_date)
        WHERE coupon_kind IN ('S', 'O')     -- nominal yield curve only
          AND fixing_yield IS NOT NULL
    ),
    bracket AS (
        SELECT
            (SELECT tenor_years FROM curve WHERE tenor_years <= p_tenor_years
             ORDER BY tenor_years DESC LIMIT 1) AS t_lo,
            (SELECT fixing_yield FROM curve WHERE tenor_years <= p_tenor_years
             ORDER BY tenor_years DESC LIMIT 1) AS y_lo,
            (SELECT tenor_years FROM curve WHERE tenor_years >= p_tenor_years
             ORDER BY tenor_years ASC LIMIT 1) AS t_hi,
            (SELECT fixing_yield FROM curve WHERE tenor_years >= p_tenor_years
             ORDER BY tenor_years ASC LIMIT 1) AS y_hi
    )
    SELECT CASE
        WHEN t_lo IS NULL OR t_hi IS NULL THEN NULL  -- no bracket -> NULL (no extrapolation)
        WHEN t_lo = t_hi THEN y_lo                   -- exact match
        ELSE y_lo + (y_hi - y_lo) * (p_tenor_years - t_lo) / (t_hi - t_lo)
    END
    FROM bracket;
$$;

-- =====================================================================
--  FUNCTION: linear-interpolate POLGB WZ (WIBOR-floater) implied-DM at a tenor.
--  Filters to bond_type='WZ' only - excludes NZ (POLSTR-floaters) because
--  WIBOR and POLSTR are different benchmarks and their DMs live in
--  separate spaces; mixing them gives a biased curve.
--
--  Used to compute spread for BGK floater auctions - BGK FPC floaters
--  are WIBOR-referenced, so comparison must be vs WZ only.
--
--  Allows linear extrapolation up to 1 year past the curve's longest
--  WZ tenor (longer than the nominal-curve helper, where we don't
--  extrapolate). Reason: BondSpot WZ universe is shallow (longest
--  active WZ ~5.5Y as of 2026-05); FPC PLN floaters routinely sit
--  0.1-1Y past that, and NULL'ing them all out loses too much signal.
--  The 1Y bound keeps wild extrapolations off the dashboard - anything
--  further (e.g. FPC1140 at 14Y) still returns NULL.
--
--  Earlier revisions of this schema (a) interpolated over fixed-coupon
--  bonds (apple/orange), then (b) over all floaters including NZ
--  (WIBOR/POLSTR mix); both produced wrong spreads, now fixed.
-- =====================================================================
CREATE OR REPLACE FUNCTION polgb_floater_dm_interp(p_date DATE, p_tenor_years NUMERIC)
RETURNS NUMERIC
LANGUAGE sql STABLE AS $$
    WITH curve AS (
        SELECT tenor_years, implied_dm_pct
        FROM polgb_curve_at(p_date)
        WHERE bond_type = 'WZ'              -- WIBOR floaters only (no NZ/POLSTR)
          AND implied_dm_pct IS NOT NULL
    ),
    bracket AS (
        SELECT
            -- Largest tenor <= target (lower bracket / extrapolation anchor).
            (SELECT tenor_years    FROM curve WHERE tenor_years <= p_tenor_years
             ORDER BY tenor_years DESC LIMIT 1) AS t_lo,
            (SELECT implied_dm_pct FROM curve WHERE tenor_years <= p_tenor_years
             ORDER BY tenor_years DESC LIMIT 1) AS y_lo,
            -- Smallest tenor >= target (upper bracket).
            (SELECT tenor_years    FROM curve WHERE tenor_years >= p_tenor_years
             ORDER BY tenor_years ASC LIMIT 1) AS t_hi,
            (SELECT implied_dm_pct FROM curve WHERE tenor_years >= p_tenor_years
             ORDER BY tenor_years ASC LIMIT 1) AS y_hi,
            -- Second-largest tenor in curve - needed to compute the slope
            -- of the last segment when extrapolating past the end.
            (SELECT tenor_years    FROM curve
             ORDER BY tenor_years DESC OFFSET 1 LIMIT 1) AS t_max_prev,
            (SELECT implied_dm_pct FROM curve
             ORDER BY tenor_years DESC OFFSET 1 LIMIT 1) AS y_max_prev
    )
    SELECT CASE
        -- Empty curve / no anchor.
        WHEN t_lo IS NULL THEN NULL
        -- Normal bracket: standard linear interp.
        WHEN t_hi IS NOT NULL THEN
            CASE WHEN t_lo = t_hi THEN y_lo
                 ELSE y_lo + (y_hi - y_lo) * (p_tenor_years - t_lo) / (t_hi - t_lo)
            END
        -- Past the longest point: extrapolate using last-segment slope,
        -- but only within 1Y of the curve end and only if we have
        -- 2+ curve points to compute a slope.
        WHEN t_hi IS NULL
             AND p_tenor_years - t_lo <= 1.0
             AND t_max_prev IS NOT NULL
             AND t_lo <> t_max_prev
        THEN y_lo + (y_lo - y_max_prev) / (t_lo - t_max_prev) * (p_tenor_years - t_lo)
        ELSE NULL
    END
    FROM bracket;
$$;

-- =====================================================================
--  VIEW: per-BGK-auction spread vs POLGB curve.
--  One row per (auction_date, series) from bgk_auction_results, enriched
--  with the bond's coupon kind (from bgk_auctions), the POLGB curve
--  point at the same tenor, and the spread in basis points.
-- =====================================================================
CREATE MATERIALIZED VIEW v_bgk_auction_spread AS
WITH bgk_with_kind AS (
    SELECT
        r.*,
        -- coupon_kind + coupon_margin_bp from XLSX (bgk_auctions). Bond-level
        -- properties, same across all issuance events for a given ISIN.
        bk.coupon_kind        AS bgk_coupon_kind,
        bk.coupon_margin_bp   AS bgk_margin_bp,
        (r.maturity_date - r.auction_date)::NUMERIC / 365.25 AS tenor_years
    FROM bgk_auction_results r
    LEFT JOIN LATERAL (
        SELECT a.coupon_kind, a.coupon_margin_bp
        FROM bgk_auctions a
        WHERE a.isin = r.isin
        LIMIT 1
    ) bk ON TRUE
),
-- enriched CTE runs once per row via the parent materialised view;
-- polgb_*_interp results land in physical storage on REFRESH.
enriched AS (
    SELECT
        b.*,
        CASE
            WHEN b.bgk_coupon_kind = 'zmienne'
                 AND b.stop_price IS NOT NULL AND b.stop_price > 0
                 AND b.maturity_date > b.auction_date
            THEN (POWER(100.0 / b.stop_price,
                        365.25 / (b.maturity_date - b.auction_date)) - 1.0) * 100.0
        END AS bgk_implied_dm_pct_calc,
        polgb_yield_interp(b.auction_date, b.tenor_years)        AS polgb_yield_at_tenor,
        polgb_floater_dm_interp(b.auction_date, b.tenor_years)   AS polgb_floater_dm_at_tenor
    FROM bgk_with_kind b
)
SELECT
    e.auction_date,
    e.series,
    e.isin,
    e.maturity_date,
    e.tenor_years,
    e.bgk_coupon_kind,
    e.bgk_margin_bp,
    e.yield_pct                AS bgk_yield_pct,
    e.stop_price               AS bgk_stop_price,
    e.bgk_implied_dm_pct_calc  AS bgk_implied_dm_pct,
    -- BGK TRUE DM = price-implied + margin from XLSX 'Kupon' column.
    CASE
        WHEN e.bgk_coupon_kind = 'zmienne'
             AND e.bgk_implied_dm_pct_calc IS NOT NULL
             AND e.bgk_margin_bp IS NOT NULL
        THEN e.bgk_implied_dm_pct_calc + (e.bgk_margin_bp / 100.0)
    END AS bgk_true_dm_pct,
    e.polgb_yield_at_tenor,
    e.polgb_floater_dm_at_tenor,
    CASE
        WHEN e.bgk_coupon_kind = 'zmienne'
             AND e.bgk_implied_dm_pct_calc IS NOT NULL
             AND e.bgk_margin_bp IS NOT NULL
             AND e.polgb_floater_dm_at_tenor IS NOT NULL
        THEN ((e.bgk_implied_dm_pct_calc + e.bgk_margin_bp / 100.0)
              - e.polgb_floater_dm_at_tenor) * 100
        WHEN e.yield_pct IS NOT NULL
             AND e.polgb_yield_at_tenor IS NOT NULL
        THEN (e.yield_pct - e.polgb_yield_at_tenor) * 100
    END AS spread_bp
FROM enriched e;

COMMENT ON MATERIALIZED VIEW v_bgk_auction_spread IS
    'BGK FPC PLN per-auction spread vs POLGB curve in basis points. '
    'Source: bgk_auction_results (PDF auctions). Materialised - call '
    'bgk_refresh_spreads() to update after refresh_bgk_pdfs workflow.';

-- =====================================================================
--  VIEW: per-issuance spread vs POLGB curve, ANY PLN program.
--  Source: bgk_auctions (XLSX = canonical list of all issuance events,
--  public auctions + private placements). FPC/KFD/FP/FWSZ/własne all
--  covered, since XLSX carries price + yield + coupon margin for every row.
--
--  Use this for cross-program time-series (e.g. funding cost trends across
--  KFD vs FPC vs FWSZ). Use v_bgk_auction_spread when you need exact
--  auction-day numbers and B/C metrics (FPC only).
-- =====================================================================
CREATE MATERIALIZED VIEW v_bgk_issuance_spread AS
WITH base AS (
    SELECT
        a.issue_date,
        a.series,
        a.isin,
        a.maturity_date,
        a.program,
        a.currency,
        a.coupon_kind        AS bgk_coupon_kind,
        a.coupon_margin_bp   AS bgk_margin_bp,
        a.coupon_ref_rate    AS bgk_ref_rate,
        a.issue_amount,
        a.price_pct          AS bgk_price_pct,
        a.yield_pct          AS bgk_yield_pct,
        (a.maturity_date - a.issue_date)::NUMERIC / 365.25 AS tenor_years
    FROM bgk_auctions a
    WHERE a.currency = 'PLN'                        -- POLGB curve is PLN-only
      AND a.maturity_date > a.issue_date
),
-- enriched CTE materialises with the parent MV on REFRESH; polgb_*_interp
-- runs once per row at refresh time, results land in physical storage.
enriched AS (
    SELECT
        b.*,
        CASE
            WHEN b.bgk_coupon_kind = 'zmienne'
                 AND b.bgk_price_pct IS NOT NULL AND b.bgk_price_pct > 0
            THEN (POWER(100.0 / b.bgk_price_pct,
                        365.25 / (b.maturity_date - b.issue_date)) - 1.0) * 100.0
        END AS bgk_implied_dm_pct,
        polgb_yield_interp(b.issue_date, b.tenor_years)        AS polgb_yield_at_tenor,
        polgb_floater_dm_interp(b.issue_date, b.tenor_years)   AS polgb_floater_dm_at_tenor
    FROM base b
)
SELECT
    e.*,
    -- Floater true DM = price-implied + margin (NULL if any input missing)
    CASE
        WHEN e.bgk_coupon_kind = 'zmienne'
             AND e.bgk_implied_dm_pct IS NOT NULL
             AND e.bgk_margin_bp IS NOT NULL
        THEN e.bgk_implied_dm_pct + (e.bgk_margin_bp / 100.0)
    END AS bgk_true_dm_pct,
    -- Spread in basis points, choosing yield- or DM-space per bond kind.
    CASE
        WHEN e.bgk_coupon_kind = 'zmienne'
             AND e.bgk_implied_dm_pct IS NOT NULL
             AND e.bgk_margin_bp IS NOT NULL
             AND e.polgb_floater_dm_at_tenor IS NOT NULL
        THEN ((e.bgk_implied_dm_pct + e.bgk_margin_bp / 100.0)
              - e.polgb_floater_dm_at_tenor) * 100
        WHEN e.bgk_yield_pct IS NOT NULL
             AND e.polgb_yield_at_tenor IS NOT NULL
        THEN (e.bgk_yield_pct - e.polgb_yield_at_tenor) * 100
    END AS spread_bp
FROM enriched e;

COMMENT ON MATERIALIZED VIEW v_bgk_issuance_spread IS
    'BGK per-issuance-event spread vs POLGB curve (bp), all PLN programs. '
    'Source: bgk_auctions XLSX. Materialised - call bgk_refresh_spreads() '
    'after refresh_bgk_xlsx workflow runs to update.';

-- =====================================================================
--  UNIQUE INDEXes - required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
--  Match each view's natural key.
-- =====================================================================
CREATE UNIQUE INDEX IF NOT EXISTS uq_v_bgk_auction_spread
    ON v_bgk_auction_spread  (auction_date, series);
CREATE UNIQUE INDEX IF NOT EXISTS uq_v_bgk_issuance_spread
    ON v_bgk_issuance_spread (issue_date, isin);

-- Secondary indexes that help common dashboard filters.
CREATE INDEX IF NOT EXISTS idx_v_bgk_issuance_spread_program
    ON v_bgk_issuance_spread (program, issue_date DESC);
CREATE INDEX IF NOT EXISTS idx_v_bgk_issuance_spread_series
    ON v_bgk_issuance_spread (series, issue_date DESC);

-- =====================================================================
--  FUNCTION: bgk_refresh_spreads() - one-shot refresh of both spread
--  materialised views, callable from workflows or the SQL editor.
--  Uses CONCURRENTLY so readers (dashboard, notebook) keep working
--  during refresh; relies on the unique indexes above.
-- =====================================================================
CREATE OR REPLACE FUNCTION bgk_refresh_spreads()
RETURNS TEXT
LANGUAGE plpgsql AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY v_bgk_auction_spread;
    REFRESH MATERIALIZED VIEW CONCURRENTLY v_bgk_issuance_spread;
    RETURN 'refreshed at ' || now();
END;
$$;

-- =====================================================================
--  Initial populate. REFRESH CONCURRENTLY requires the MV to be already
--  populated, so we do a plain non-concurrent REFRESH on first install
--  (will block briefly while polgb_*_interp runs 304 + 277 rows worth).
-- =====================================================================
REFRESH MATERIALIZED VIEW v_bgk_auction_spread;
REFRESH MATERIALIZED VIEW v_bgk_issuance_spread;
