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
--      both for BGK auction and POLGB curve, then spread_dm_bp.
--      This is the same "zero-coupon-equivalent" technique CETO uses
--      for MF floaters (WZ/NZ) - it ignores reset cashflows but the
--      bias cancels when comparing two bonds quoted the same way.

DROP VIEW IF EXISTS v_bgk_auction_spread;
DROP FUNCTION IF EXISTS polgb_dm_interp(DATE, NUMERIC);
DROP FUNCTION IF EXISTS polgb_yield_interp(DATE, NUMERIC);
DROP FUNCTION IF EXISTS polgb_curve_at(DATE);

-- =====================================================================
--  FUNCTION: POLGB nominal yield curve points at an arbitrary date.
--  One row per active POLGB bond on p_date, with its latest valid
--  fixing per the leakage-safe pick rule.
-- =====================================================================
CREATE OR REPLACE FUNCTION polgb_curve_at(p_date DATE)
RETURNS TABLE (
    polgb_isin     VARCHAR(12),
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
        fx.fixing_date,
        fx.fixing_session,
        (b.maturity_date - p_date)::NUMERIC / 365.25 AS tenor_years,
        fx.fixing_yield,
        fx.fixing_price,
        -- Implied DM = zero-coupon-equivalent yield back-solved from price.
        -- Used as the "yield" for floater-vs-floater comparisons.
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
    WHERE b.coupon_kind IN ('S', 'O')        -- fixed + zero-coupon only
      AND b.maturity_date > p_date
      AND fx.fixing_yield IS NOT NULL;       -- need a real yield quote
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
        WHERE fixing_yield IS NOT NULL
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
--  FUNCTION: linear-interpolate POLGB implied-DM at a target tenor.
--  Same shape as polgb_yield_interp but using the implied_dm_pct column
--  derived from prices. Used for floater BGK bond spreads.
-- =====================================================================
CREATE OR REPLACE FUNCTION polgb_dm_interp(p_date DATE, p_tenor_years NUMERIC)
RETURNS NUMERIC
LANGUAGE sql STABLE AS $$
    WITH curve AS (
        SELECT tenor_years, implied_dm_pct
        FROM polgb_curve_at(p_date)
        WHERE implied_dm_pct IS NOT NULL
    ),
    bracket AS (
        SELECT
            (SELECT tenor_years    FROM curve WHERE tenor_years <= p_tenor_years
             ORDER BY tenor_years DESC LIMIT 1) AS t_lo,
            (SELECT implied_dm_pct FROM curve WHERE tenor_years <= p_tenor_years
             ORDER BY tenor_years DESC LIMIT 1) AS y_lo,
            (SELECT tenor_years    FROM curve WHERE tenor_years >= p_tenor_years
             ORDER BY tenor_years ASC LIMIT 1) AS t_hi,
            (SELECT implied_dm_pct FROM curve WHERE tenor_years >= p_tenor_years
             ORDER BY tenor_years ASC LIMIT 1) AS y_hi
    )
    SELECT CASE
        WHEN t_lo IS NULL OR t_hi IS NULL THEN NULL
        WHEN t_lo = t_hi THEN y_lo
        ELSE y_lo + (y_hi - y_lo) * (p_tenor_years - t_lo) / (t_hi - t_lo)
    END
    FROM bracket;
$$;

-- =====================================================================
--  VIEW: per-BGK-auction spread vs POLGB curve.
--  One row per (auction_date, series) from bgk_auction_results, enriched
--  with the bond's coupon kind (from bgk_auctions), the POLGB curve
--  point at the same tenor, and the spread in basis points.
-- =====================================================================
CREATE OR REPLACE VIEW v_bgk_auction_spread AS
WITH bgk_with_kind AS (
    SELECT
        r.*,
        -- coupon_kind from XLSX (bgk_auctions). Same bond -> same kind
        -- regardless of which issuance row we sample, so any row works.
        (SELECT a.coupon_kind FROM bgk_auctions a WHERE a.isin = r.isin LIMIT 1)
            AS bgk_coupon_kind,
        -- Tenor at auction date.
        (r.maturity_date - r.auction_date)::NUMERIC / 365.25
            AS tenor_years
    FROM bgk_auction_results r
)
SELECT
    b.auction_date,
    b.series,
    b.isin,
    b.maturity_date,
    b.tenor_years,
    b.bgk_coupon_kind,
    b.yield_pct                AS bgk_yield_pct,
    b.stop_price               AS bgk_stop_price,
    -- BGK implied DM (only meaningful for floaters)
    CASE
        WHEN b.bgk_coupon_kind = 'zmienne'
             AND b.stop_price IS NOT NULL AND b.stop_price > 0
             AND b.maturity_date > b.auction_date
        THEN (POWER(100.0 / b.stop_price,
                    365.25 / (b.maturity_date - b.auction_date)) - 1.0) * 100.0
    END AS bgk_implied_dm_pct,
    -- POLGB curve point at the BGK tenor
    polgb_yield_interp(b.auction_date, b.tenor_years) AS polgb_yield_at_tenor,
    polgb_dm_interp(b.auction_date, b.tenor_years)    AS polgb_dm_at_tenor,
    -- Spread in basis points. Use DM space for floaters, yield space
    -- for everything else with a quoted yield.
    CASE
        WHEN b.bgk_coupon_kind = 'zmienne' THEN
            (
                CASE
                    WHEN b.stop_price IS NOT NULL AND b.stop_price > 0
                         AND b.maturity_date > b.auction_date
                    THEN (POWER(100.0 / b.stop_price,
                                365.25 / (b.maturity_date - b.auction_date)) - 1.0) * 100.0
                END
                - polgb_dm_interp(b.auction_date, b.tenor_years)
            ) * 100
        WHEN b.yield_pct IS NOT NULL THEN
            (b.yield_pct - polgb_yield_interp(b.auction_date, b.tenor_years)) * 100
    END AS spread_bp
FROM bgk_with_kind b;

COMMENT ON VIEW v_bgk_auction_spread IS
    'BGK FPC PLN per-auction spread vs POLGB curve in basis points. '
    'Fixed-coupon bonds: yield-space spread. Floaters: DM-space spread '
    '(zero-coupon equivalent from price). NULL when POLGB curve does '
    'not bracket the BGK tenor (we do not extrapolate).';
