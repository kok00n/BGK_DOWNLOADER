-- BGK Bonds - "Baza obligacji" XLSX mirror.
--
-- One row per issue event from BGK's Statystyka XLSX
-- (https://www.bgk.pl/.../statystyka/). The XLSX is a wide history dump
-- (365+ rows since 2009); we mirror it 1:1 as the canonical source of
-- per-ISIN coupon/price/yield/issuance-amount facts.
--
-- Naming convention: bgk_* prefix (we share the Supabase project with
-- CETO_DOWNLOADER's bondspot_* tables - see BGK_HANDOFF.md, decision 2).
--
-- Source XLSX column -> table column mapping:
--   Seria            -> series
--   Data emisji      -> issue_date         (PK part 1)
--   Data wykupu      -> maturity_date
--   ISIN             -> isin               (PK part 2)
--   Typ transakcji   -> program            (KFD / FPC / ...)
--   Lata do wykupu   -> years_to_maturity  (original tenor at issuance)
--   Oprocentowanie   -> coupon_kind        ('stałe' / 'zmienne')
--   Kupon            -> coupon_pct         (XLSX 0.0575 -> 5.75)
--   Waluta           -> currency           (PLN / EUR)
--   Wartość emisji   -> issue_amount       (nominal, in `currency`)
--   Cena             -> price_pct          (XLSX 0.98901 -> 98.901)
--   Rentowność       -> yield_pct          (XLSX 0.06008 -> 6.008)
--
-- All percent fields are stored as percent (5.75 not 0.0575) to match
-- the convention used in bondspot_fixing.fixing_yield.

-- Drop dependent objects before recreating (idempotent).
DROP VIEW  IF EXISTS v_bgk_outstanding_timeline;
DROP VIEW  IF EXISTS v_bgk_outstanding_now;
DROP FUNCTION IF EXISTS bgk_outstanding_at(DATE);

-- Generic updated_at trigger helper (project-scoped, no dependency on
-- bondspot_set_updated_at from the POLGB schema).
CREATE OR REPLACE FUNCTION bgk_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
--  BGK_AUCTIONS - issuance events from "Baza obligacji" XLSX
-- =====================================================================
CREATE TABLE IF NOT EXISTS bgk_auctions (
    issue_date        DATE         NOT NULL,
    isin              VARCHAR(12)  NOT NULL,
    series            VARCHAR(32)  NOT NULL,
    maturity_date     DATE         NOT NULL,
    program           TEXT,                              -- KFD, FPC, ...
    years_to_maturity SMALLINT,                          -- original tenor at issuance
    coupon_kind       TEXT,                              -- 'stałe' / 'zmienne'
    coupon_pct        NUMERIC(12,6),                     -- e.g. 5.75
    currency          CHAR(3)      NOT NULL DEFAULT 'PLN',
    issue_amount      NUMERIC(20,2),                     -- nominal, in `currency`
    price_pct         NUMERIC(14,6),                     -- e.g. 98.901
    yield_pct         NUMERIC(12,6),                     -- e.g. 6.008
    source            TEXT         NOT NULL DEFAULT 'bgk_xlsx',
    source_url        TEXT,
    inserted_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (issue_date, isin)
);

CREATE INDEX IF NOT EXISTS idx_bgk_auctions_isin       ON bgk_auctions(isin, issue_date DESC);
CREATE INDEX IF NOT EXISTS idx_bgk_auctions_maturity   ON bgk_auctions(maturity_date);
CREATE INDEX IF NOT EXISTS idx_bgk_auctions_program    ON bgk_auctions(program, issue_date DESC);
CREATE INDEX IF NOT EXISTS idx_bgk_auctions_issue_date ON bgk_auctions(issue_date DESC);

DROP TRIGGER IF EXISTS trg_bgk_auctions_updated_at ON bgk_auctions;
CREATE TRIGGER trg_bgk_auctions_updated_at
    BEFORE UPDATE ON bgk_auctions
    FOR EACH ROW
    EXECUTE FUNCTION bgk_set_updated_at();

-- =====================================================================
--  FUNCTION: outstanding (nominal) at an arbitrary date, per (program, currency).
--  Outstanding = sum(issue_amount) where issue_date <= D AND maturity_date > D.
-- =====================================================================
CREATE OR REPLACE FUNCTION bgk_outstanding_at(p_date DATE)
RETURNS TABLE (
    program            TEXT,
    currency           CHAR(3),
    n_active_isins     BIGINT,
    outstanding_amount NUMERIC
)
LANGUAGE sql STABLE AS $$
    SELECT
        program,
        currency,
        COUNT(DISTINCT isin)        AS n_active_isins,
        COALESCE(SUM(issue_amount), 0) AS outstanding_amount
    FROM bgk_auctions
    WHERE issue_date    <= p_date
      AND maturity_date >  p_date
    GROUP BY program, currency;
$$;

-- =====================================================================
--  VIEW: outstanding today (convenience).
-- =====================================================================
CREATE OR REPLACE VIEW v_bgk_outstanding_now AS
    SELECT * FROM bgk_outstanding_at(CURRENT_DATE);

-- =====================================================================
--  VIEW: full outstanding timeline - running total at every change event
--  (issuance = +amount, maturity = -amount). One row per change point;
--  to get outstanding at arbitrary date D, take last row with event_date <= D.
-- =====================================================================
CREATE OR REPLACE VIEW v_bgk_outstanding_timeline AS
WITH events AS (
    SELECT issue_date    AS event_date, isin, program, currency,  issue_amount AS delta
      FROM bgk_auctions
    UNION ALL
    SELECT maturity_date AS event_date, isin, program, currency, -issue_amount AS delta
      FROM bgk_auctions
)
SELECT
    event_date,
    program,
    currency,
    SUM(delta) OVER (
        PARTITION BY program, currency
        ORDER BY event_date
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS running_outstanding
FROM events
ORDER BY event_date, program, currency;
