-- KromaRAG 001 — generic Postgres + pgvector schema (specs/01 §3, specs/03 §1).
-- Idempotent; runs on Neon / Render / self-hosted via DATABASE_URL. No provider-specific SQL.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS kroma_documents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_sub TEXT NOT NULL,
  title TEXT NOT NULL,
  mime TEXT NOT NULL DEFAULT 'text/plain',
  uri TEXT,
  status TEXT NOT NULL DEFAULT 'ready',
  error TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kroma_chunks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  doc_id UUID NOT NULL REFERENCES kroma_documents(id) ON DELETE CASCADE,
  ord INT NOT NULL,
  content TEXT NOT NULL,
  content_tsv TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
  embedding VECTOR(768) NOT NULL,
  token_count INT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS kroma_chunks_embedding_idx
  ON kroma_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists=100);
CREATE INDEX IF NOT EXISTS kroma_chunks_tsv_idx ON kroma_chunks USING GIN (content_tsv);
CREATE INDEX IF NOT EXISTS kroma_chunks_doc_idx ON kroma_chunks (doc_id);

CREATE TABLE IF NOT EXISTS kroma_roles (name TEXT PRIMARY KEY);
INSERT INTO kroma_roles(name) VALUES ('admin'),('editor'),('viewer') ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS kroma_user_roles (
  user_sub TEXT NOT NULL,
  role TEXT NOT NULL REFERENCES kroma_roles(name),
  PRIMARY KEY (user_sub, role)
);

CREATE TABLE IF NOT EXISTS kroma_document_roles (
  doc_id UUID NOT NULL REFERENCES kroma_documents(id) ON DELETE CASCADE,
  role TEXT NOT NULL REFERENCES kroma_roles(name),
  PRIMARY KEY (doc_id, role)
);

CREATE TABLE IF NOT EXISTS kroma_conversations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_sub TEXT NOT NULL,
  title TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kroma_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conv_id UUID NOT NULL REFERENCES kroma_conversations(id) ON DELETE CASCADE,
  role TEXT,
  content TEXT,
  trace_id TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
