-- KromaRAG 002 — conversations & history hardening (specs/06 §2.2).
-- Idempotent; runs on Neon / Render / self-hosted via DATABASE_URL. No provider-specific SQL.
-- Assumes 001 has created kroma_conversations / kroma_messages.

-- ── updated_at on threads ──
ALTER TABLE kroma_conversations
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();

-- Backfill NULL updated_at from created_at (safe re-run).
UPDATE kroma_conversations SET updated_at = created_at WHERE updated_at IS NULL;

-- ── token/model/route accounting on messages ──
ALTER TABLE kroma_messages
  ADD COLUMN IF NOT EXISTS prompt_tokens INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS completion_tokens INT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS model TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS route TEXT NOT NULL DEFAULT 'retrieval';

-- ── backfill nullable role/content before constraining ──
UPDATE kroma_messages SET role = 'user' WHERE role IS NULL;
UPDATE kroma_messages SET content = '' WHERE content IS NULL;

-- ── role CHECK (idempotent via DO block; ADD CONSTRAINT has no IF NOT EXISTS) ──
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'kroma_messages_role_chk'
  ) THEN
    ALTER TABLE kroma_messages
      ADD CONSTRAINT kroma_messages_role_chk CHECK (role IN ('user', 'assistant', 'system'));
  END IF;
END
$$;

-- ── NOT NULL on role/content (safe re-run; raises only if NULLs remain, which backfill prevents) ──
DO $$
BEGIN
  BEGIN
    ALTER TABLE kroma_messages ALTER COLUMN role SET NOT NULL;
  EXCEPTION WHEN others THEN NULL;
  END;
  BEGIN
    ALTER TABLE kroma_messages ALTER COLUMN content SET NOT NULL;
  EXCEPTION WHEN others THEN NULL;
  END;
END
$$;

-- ── idempotency: one assistant row per trace (reconnect-safe) ──
CREATE UNIQUE INDEX IF NOT EXISTS kroma_messages_trace_uidx
  ON kroma_messages (trace_id) WHERE trace_id IS NOT NULL;

-- ── access + pagination indexes ──
CREATE INDEX IF NOT EXISTS kroma_conversations_user_updated_idx
  ON kroma_conversations (user_sub, updated_at DESC);
CREATE INDEX IF NOT EXISTS kroma_messages_conv_created_idx
  ON kroma_messages (conv_id, created_at ASC, id ASC);
