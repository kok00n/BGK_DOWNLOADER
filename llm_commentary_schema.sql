-- LLM Commentary - shared with CETO_DOWNLOADER (same Supabase project per
-- handoff decision 2). The setup cell in notebooks/bgk_dashboard.ipynb
-- writes one row per chart + final report:
--   - snapshot_date   = TODAY at render time
--   - section         = stable id, "bgk_<chart>" or "bgk_final_auction_report"
--                       so we don't collide with CETO's "polgb_*" / "Chart N" ids
--   - prompt/response = full I/O so we can audit + replay
--   - input/output_tokens = cost tracking
--
-- On the next render, the model receives the last N historical responses
-- for the same section so it can reference its own past analyses
-- ("trend continues from last time", "as I noted before...").
--
-- Schema is IDEMPOTENT and identical to CETO_DOWNLOADER/llm_commentary_schema.sql
-- (intentional 1:1 copy). Apply it from either repo; running both is a no-op.

DROP FUNCTION IF EXISTS llm_commentary_history(TEXT, INT);

CREATE TABLE IF NOT EXISTS llm_commentary (
    id             BIGSERIAL PRIMARY KEY,
    rendered_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    snapshot_date  DATE NOT NULL,
    section        TEXT NOT NULL,
    chart_name     TEXT,
    model          TEXT NOT NULL,
    prompt         TEXT NOT NULL,
    response       TEXT NOT NULL,
    input_tokens   INT,
    output_tokens  INT,
    inserted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_llm_commentary_section_date
    ON llm_commentary (section, snapshot_date DESC, rendered_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_commentary_rendered
    ON llm_commentary (rendered_at DESC);

-- =====================================================================
--  RPC: ostatnie N analiz dla danej sekcji - injected as history block
--  into the next prompt so model can reference its own previous output.
-- =====================================================================
CREATE OR REPLACE FUNCTION llm_commentary_history(
    p_section TEXT,
    p_limit   INT DEFAULT 3
)
RETURNS TABLE (
    snapshot_date DATE,
    rendered_at   TIMESTAMPTZ,
    response      TEXT
)
LANGUAGE sql STABLE AS $$
    SELECT snapshot_date, rendered_at, response
    FROM llm_commentary
    WHERE section = p_section
    ORDER BY rendered_at DESC
    LIMIT p_limit;
$$;
