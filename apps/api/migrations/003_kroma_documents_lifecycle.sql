-- KromaRAG 003 — document lifecycle hardening (specs/07 §2.2).
-- Idempotent; runs on Neon / Render / self-hosted via DATABASE_URL. No provider-specific SQL.
-- Assumes 001 has created kroma_documents / kroma_chunks / kroma_document_roles.
-- Canonical status vocabulary (lowercase): pending|indexing|indexed|failed|archived.
-- Legacy 'ready' (001 default + 002-era writes) is aliased to 'indexed'.

-- ── normalize legacy 'ready' → 'indexed' (safe re-run) ──
UPDATE kroma_documents SET status = 'indexed' WHERE status = 'ready';

-- ── lifecycle columns ──
ALTER TABLE kroma_documents
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now(),
  ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1,
  ADD COLUMN IF NOT EXISTS chunk_config JSONB NOT NULL DEFAULT '{"chunk_tokens":600,"chunk_overlap":90}',
  ADD COLUMN IF NOT EXISTS embedding_model TEXT NOT NULL DEFAULT 'gemini-embedding-001',
  ADD COLUMN IF NOT EXISTS embedding_dim INT NOT NULL DEFAULT 768;

-- Backfill updated_at from created_at (safe re-run).
UPDATE kroma_documents SET updated_at = created_at WHERE updated_at IS NULL;

-- ── status CHECK (idempotent via DO block; ADD CONSTRAINT has no IF NOT EXISTS) ──
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'kroma_documents_status_chk'
  ) THEN
    ALTER TABLE kroma_documents
      ADD CONSTRAINT kroma_documents_status_chk
      CHECK (status IN ('pending','indexing','indexed','failed','archived'));
  END IF;
END
$$;

-- ── version / dim sanity ──
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'kroma_documents_version_chk'
  ) THEN
    ALTER TABLE kroma_documents
      ADD CONSTRAINT kroma_documents_version_chk CHECK (version >= 1);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'kroma_documents_dim_chk'
  ) THEN
    ALTER TABLE kroma_documents
      ADD CONSTRAINT kroma_documents_dim_chk CHECK (embedding_dim IN (768));
  END IF;
END
$$;

-- ── access + lifecycle indexes ──
CREATE INDEX IF NOT EXISTS kroma_documents_owner_updated_idx
  ON kroma_documents (owner_sub, updated_at DESC);
CREATE INDEX IF NOT EXISTS kroma_documents_owner_status_idx
  ON kroma_documents (owner_sub, status);
CREATE INDEX IF NOT EXISTS kroma_documents_status_updated_idx
  ON kroma_documents (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS kroma_documents_mime_idx
  ON kroma_documents (mime);
