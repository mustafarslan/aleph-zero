-- One row per (content, treatment). The family is what a path selects: a language,
-- Markdown, plain text. The same bytes under two families are chunked once for each.
CREATE TABLE IF NOT EXISTS blobs (
    blob_oid        TEXT NOT NULL,
    family          TEXT NOT NULL,
    kind            TEXT NOT NULL,      -- 'doc', 'text', or 'skip:<reason>'
    size            INTEGER NOT NULL,
    chunker_version INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,   -- unix seconds; garbage collection is by age
    symbols_version INTEGER NOT NULL DEFAULT 0,  -- what wrote its symbols: older ones are read again, chunks untouched
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
    nested    INTEGER NOT NULL DEFAULT 0,  -- defined inside a function: `symbol` finds it; search's name ranking does not
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

-- What the code graph (M7) reads from a blob: its call sites as written, the names its
-- imports bind, what its types are built on and the types of their fields. Which definition
-- a call reaches depends on the other files of a view, so no edge is stored. Filled when a
-- graph question is first asked of a view, not when a blob is indexed: an index that is only
-- searched pays nothing for it. Which blobs call a name is asked of the text index.
CREATE TABLE IF NOT EXISTS graph_facts (
    blob_oid TEXT NOT NULL,
    family   TEXT NOT NULL,
    version  INTEGER NOT NULL,    -- of the readers that wrote the row; another version is read again
    shape    BLOB NOT NULL,       -- zlib(JSON): bindings, bases, field types: needed of every blob of a view
    calls    BLOB NOT NULL,       -- zlib(JSON): the call sites: needed of the blobs a question touches
    PRIMARY KEY (blob_oid, family),
    FOREIGN KEY (blob_oid, family) REFERENCES blobs(blob_oid, family) ON DELETE CASCADE
) WITHOUT ROWID;

-- What a blob states about the architecture (M8): the routes it serves, its models and tables, the
-- environment names it reads, its tasks and services. One row per blob that was read for them,
-- empty for the many that state nothing. Filled when facts are first asked of a view, like graph_facts.
CREATE TABLE IF NOT EXISTS arch_facts (
    blob_oid TEXT NOT NULL,
    family   TEXT NOT NULL,
    version  INTEGER NOT NULL,    -- of the readers that wrote the row; another version is read again
    facts    BLOB NOT NULL,       -- zlib(JSON): [kind, name, line, detail] each; empty when there is none
    PRIMARY KEY (blob_oid, family),
    FOREIGN KEY (blob_oid, family) REFERENCES blobs(blob_oid, family) ON DELETE CASCADE
) WITHOUT ROWID;

-- One vector for a whole decision record (`aleph decisions`), by the text that was embedded: its title, its
-- Decision section and its body, cut. Keyed by that text, so a record that did not change is not embedded again
-- and one that did leaves its old row behind (a few hundred bytes; an index is a cache and is rebuilt whole).
CREATE TABLE IF NOT EXISTS record_vectors (
    text_hash TEXT NOT NULL,      -- sha256 of the embedded text
    kind      TEXT NOT NULL,      -- the model, prefix version and width the vector was made with
    vector    BLOB NOT NULL,      -- float32, unit length
    PRIMARY KEY (text_hash, kind)
) WITHOUT ROWID;

-- Compiler-grade indexes (SCIP) made elsewhere and imported (M9). One row per document: the blob
-- it is taken to have been made from, the documents it depends on, and its references to
-- definitions inside the tree. Believed in a view only while those blobs are the view's
-- (ADR-004); scipstore.py checks. Not tied to `blobs`: an import stays until it is replaced or forgotten.
CREATE TABLE IF NOT EXISTS scip_imports (
    import_id   INTEGER PRIMARY KEY,
    tool        TEXT NOT NULL,       -- the indexer that wrote it, as the index says
    stamped     TEXT NOT NULL,       -- what its documents were stamped with: a revision, or 'worktree'
    imported_at INTEGER NOT NULL,
    skipped     INTEGER NOT NULL     -- documents left out: a path outside the tree, or not in what it was stamped with
);

CREATE TABLE IF NOT EXISTS scip_documents (
    path      TEXT PRIMARY KEY,
    import_id INTEGER NOT NULL REFERENCES scip_imports(import_id) ON DELETE CASCADE,
    blob_oid  TEXT NOT NULL,
    depends   BLOB NOT NULL,         -- zlib(JSON): the paths it depends on
    refs      BLOB NOT NULL          -- zlib(JSON): where it refers to what
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS scip_documents_by_import ON scip_documents(import_id);

-- Which row of a vector file holds a chunk's embedding. One file per kind of vector
-- (model, prefix version, width). A row is never moved; when its chunk is collected it
-- goes on the free list and is given to a later chunk (vectors.py).
CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id INTEGER NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    kind     TEXT NOT NULL,
    row      INTEGER NOT NULL,
    PRIMARY KEY (chunk_id, kind)
) WITHOUT ROWID;

-- No two chunks share a row, whatever a session does.
CREATE UNIQUE INDEX IF NOT EXISTS chunk_vectors_by_row ON chunk_vectors(kind, row);

-- Rows of the vector file that belong to no chunk any more.
CREATE TABLE IF NOT EXISTS vector_free (
    kind TEXT NOT NULL,
    row  INTEGER NOT NULL,
    PRIMARY KEY (kind, row)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS chunk_vectors_freed AFTER DELETE ON chunk_vectors
BEGIN
    INSERT OR IGNORE INTO vector_free(kind, row) VALUES (old.kind, old.row);
END;

CREATE TABLE IF NOT EXISTS vector_files (
    kind     TEXT PRIMARY KEY,
    next_row INTEGER NOT NULL,     -- the file's length in rows; only ever grows
    writes   INTEGER NOT NULL DEFAULT 0   -- goes up with every change to the rows of this kind
) WITHOUT ROWID;

-- Numbers every session can see. 'collections' goes up whenever content is removed from
-- the index, so a session knows its list of chunks may name rows that are gone.
-- 'collected_at' is when garbage was last collected (unix seconds).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
) WITHOUT ROWID;

CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    symbol, signature, doc, body,
    content='', contentless_delete=1,
    -- unicode61 keeps snake_case identifiers whole; porter stems on top of it, so
    -- "invoice" finds "Invoices". The same tokenizer reads queries, so exact
    -- identifiers still match themselves.
    tokenize="porter unicode61 tokenchars '_'"
);
