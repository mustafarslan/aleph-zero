-- One row per (content, treatment). The family is what a path selects: a language,
-- Markdown, plain text. The same bytes under two families are chunked once for each.
CREATE TABLE IF NOT EXISTS blobs (
    blob_oid        TEXT NOT NULL,
    family          TEXT NOT NULL,
    kind            TEXT NOT NULL,      -- 'doc', 'text', or 'skip:<reason>'
    size            INTEGER NOT NULL,
    chunker_version INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,   -- unix seconds; garbage collection is by age
    PRIMARY KEY (blob_oid, family)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id   INTEGER PRIMARY KEY,
    chunk_hash TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    signature  TEXT NOT NULL,
    breadcrumb TEXT NOT NULL,
    text       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blob_chunks (
    blob_oid   TEXT NOT NULL,
    family     TEXT NOT NULL,
    ord        INTEGER NOT NULL,
    chunk_id   INTEGER NOT NULL REFERENCES chunks(chunk_id),
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    PRIMARY KEY (blob_oid, family, ord),
    FOREIGN KEY (blob_oid, family) REFERENCES blobs(blob_oid, family) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS blob_chunks_by_chunk ON blob_chunks(chunk_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    symbol, signature, doc, body,
    content='', contentless_delete=1,
    -- unicode61 keeps snake_case identifiers whole; porter stems on top of it, so
    -- "invoice" finds "Invoices". The same tokenizer reads queries, so exact
    -- identifiers still match themselves.
    tokenize="porter unicode61 tokenchars '_'"
);
