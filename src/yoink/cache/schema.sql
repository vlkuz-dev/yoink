CREATE TABLE IF NOT EXISTS cached_url (
    url_hash      TEXT PRIMARY KEY,
    source_url    TEXT    NOT NULL,
    provider      TEXT    NOT NULL,
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cached_file (
    url_hash  TEXT    NOT NULL,
    position  INTEGER NOT NULL,
    file_id   TEXT    NOT NULL,
    kind      TEXT    NOT NULL,
    mime      TEXT,
    PRIMARY KEY (url_hash, position),
    FOREIGN KEY (url_hash) REFERENCES cached_url(url_hash) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cached_file_url_hash ON cached_file(url_hash);
