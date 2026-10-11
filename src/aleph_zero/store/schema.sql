CREATE TABLE IF NOT EXISTS blobs (
    blob_oid        TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,      -- 'doc', 'text', or 'skip:<reason>'
    size            INTEGER NOT NULL,
    chunker_version INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL    -- unix seconds; garbage collection is by age
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
    blob_oid   TEXT NOT NULL REFERENCES blobs(blob_oid) ON DELETE CASCADE,
    ord        INTEGER NOT NULL,
    chunk_id   INTEGER NOT NULL REFERENCES chunks(chunk_id),
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    PRIMARY KEY (blob_oid, ord)
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
