-- BGK Auction Calendar - upcoming auctions discovered from "Komunikaty" page.
--
-- BGK publishes auction announcements ("Zapowiedz przetargu sprzedazy") on
-- the same komunikaty page that hosts past-auction result PDFs. The scout
-- (refresh_bgk_calendar.py, weekly cron) scrapes the page, extracts
-- future auction_date + announced series, and upserts here.
--
-- Used by auction_day_pipeline.yml (daily 22:00 UTC cron) - if today's
-- date is in this table the pipeline runs refresh_bgk_pdfs + dashboard
-- render. Otherwise it exits early so LLM commentary tokens are only
-- spent on auction days.

CREATE TABLE IF NOT EXISTS bgk_auction_calendar (
    auction_date   DATE         PRIMARY KEY,
    series         TEXT[],                                -- announced series for that day (e.g. {FPC0631, FPC1031})
    announced_at   DATE,                                  -- when BGK published the zapowiedz (best-effort)
    source_url     TEXT,                                  -- pdf/page URL where announcement was found
    scraped_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bgk_auction_calendar_date
    ON bgk_auction_calendar(auction_date);

DROP TRIGGER IF EXISTS trg_bgk_auction_calendar_updated_at ON bgk_auction_calendar;
CREATE TRIGGER trg_bgk_auction_calendar_updated_at
    BEFORE UPDATE ON bgk_auction_calendar
    FOR EACH ROW
    EXECUTE FUNCTION bgk_set_updated_at();
