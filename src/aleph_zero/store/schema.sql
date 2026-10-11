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
    scope_start INTEGER NOT NULL DEFAULT 0,  -- the whole definition, when this is a piece of one
    scope_end   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (blob_oid, family, ord),
    FOREIGN KEY (blob_oid, family) REFERENCES blobs(blob_oid, family) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS blob_chunks_by_chunk ON blob_chunks(chunk_id);

-- Every definition of a blob: what a by-name lookup reads.
CREATE TABLE IF NOT EXISTS symbols (
    blob_oid  TEXT NOT NULL,
    family    TEXT NOT NULL,
    ord       INTEGER NOT NULL,
    name      TEXT NOT NULL,
    name_key  TEXT NOT NULL,      -- lower-cased name
    qualified TEXT NOT NULL,      -- dotted, within the file: TokenStore.get
    kind      TEXT NOT NULL,
    signature TEXT NOT NULL,
    chunk_ord INTEGER NOT NULL,   -- the chunk of the same blob that holds its first line
    line      INTEGER NOT NULL,
    PRIMARY KEY (blob_oid, family, ord),
    FOREIGN KEY (blob_oid, family) REFERENCES blobs(blob_oid, family) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS symbols_by_name ON symbols(name_key);

-- What a blob imports, unresolved: the code graph (M7) resolves these per view.
CREATE TABLE IF NOT EXISTS imports (
    blob_oid  TEXT NOT NULL,
    family    TEXT NOT NULL,
    ord       INTEGER NOT NULL,
    specifier TEXT NOT NULL,
    names     TEXT NOT NULL,      -- JSON array of the names taken from the module
    level     INTEGER NOT NULL,   -- leading dots of a relative Python import
    kind      TEXT NOT NULL,      -- value, type_only, wildcard, dynamic, side_effect, system
    flags     INTEGER NOT NULL,   -- 1 = re-export, 2 = deferred (inside a definition)
    line      INTEGER NOT NULL,
    PRIMARY KEY (blob_oid, family, ord),
    FOREIGN KEY (blob_oid, family) REFERENCES blobs(blob_oid, family) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    symbol, signature, doc, body,
    content='', contentless_delete=1,
    -- unicode61 keeps snake_case identifiers whole; porter stems on top of it, so
    -- "invoice" finds "Invoices". The same tokenizer reads queries, so exact
    -- identifiers still match themselves.
    tokenize="porter unicode61 tokenchars '_'"
);
