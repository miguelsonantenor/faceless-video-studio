-- Inspiration sets (INS-1): example videos the creator likes, one set per subfolder of the
-- inspiration folder. They are never clips: no clip number, so no spec can use them.
CREATE TABLE inspiration_sets (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    folder      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE inspiration_videos (
    set_id        TEXT NOT NULL REFERENCES inspiration_sets (id) ON DELETE CASCADE,
    content_hash  TEXT NOT NULL,
    path          TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    note          TEXT NOT NULL DEFAULT '',  -- "what I like here", from the creator
    added_at      TEXT NOT NULL,
    PRIMARY KEY (set_id, content_hash)
);
