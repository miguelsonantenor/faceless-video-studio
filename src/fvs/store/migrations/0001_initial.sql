-- FVS schema v1: the 19 tables of DS 6.1.
-- Times are ISO 8601 UTC text. JSON columns hold JSON text.
-- Version tables (scripts, sentences, voiceovers, specs, reviews, ledger) are insert-only (PRJ-5).

CREATE TABLE clips (
    id            TEXT PRIMARY KEY,
    number        INTEGER NOT NULL UNIQUE,
    path          TEXT NOT NULL UNIQUE,
    file_name     TEXT NOT NULL,
    content_hash  TEXT NOT NULL UNIQUE,
    size_bytes    INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    duration_s    REAL,
    width         INTEGER,
    height        INTEGER,
    fps           REAL,
    has_audio     INTEGER,
    state         TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'changed', 'missing')),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE projects (
    id            TEXT PRIMARY KEY,
    number        INTEGER NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    slug          TEXT NOT NULL,
    series        TEXT,
    part          INTEGER,
    state         TEXT NOT NULL DEFAULT 'new' CHECK (state IN (
                      'new', 'scripted', 'voiced', 'planned', 'rendering', 'claude_check',
                      'in_review', 'changes_requested', 'exported', 'skipped')),
    template      TEXT,
    script_v      INTEGER,
    voiceover_id  TEXT,
    spec_v        INTEGER,
    render_id     TEXT,
    failed        INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    stale_flags   TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX projects_state ON projects (state);

CREATE TABLE project_clips (
    project_id    TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    clip_id       TEXT NOT NULL REFERENCES clips (id),
    position      INTEGER NOT NULL,
    PRIMARY KEY (project_id, clip_id)
);

CREATE TABLE script_versions (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    title           TEXT NOT NULL,
    voiceover_text  TEXT NOT NULL,
    hook            TEXT,
    post_caption    TEXT,
    hashtags        TEXT NOT NULL DEFAULT '[]',
    language        TEXT NOT NULL DEFAULT 'en',
    notes           TEXT,
    est_seconds     REAL,
    author          TEXT NOT NULL CHECK (author IN ('claude', 'creator')),
    created_at      TEXT NOT NULL,
    UNIQUE (project_id, version)
);

CREATE TABLE sentences (
    script_version_id  TEXT NOT NULL REFERENCES script_versions (id) ON DELETE CASCADE,
    i                  INTEGER NOT NULL,
    spoken_text        TEXT NOT NULL,
    caption_text       TEXT NOT NULL,
    pause_after_s      REAL NOT NULL DEFAULT 0,
    emphasis           TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (script_version_id, i)
);

CREATE TABLE voice_sentences (
    cache_key       TEXT PRIMARY KEY,
    voice_alias     TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    audio_path      TEXT NOT NULL,
    alignment_path  TEXT NOT NULL,
    duration_s      REAL NOT NULL,
    chars_billed    INTEGER NOT NULL,
    request_id      TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE voiceovers (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    script_version  INTEGER NOT NULL,
    voice_alias     TEXT NOT NULL,
    audio_path      TEXT NOT NULL,
    timings_path    TEXT NOT NULL,
    duration_s      REAL NOT NULL,
    chars_billed    INTEGER NOT NULL DEFAULT 0,
    chars_reused    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE TABLE edit_specs (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    version           INTEGER NOT NULL,
    spec_json         TEXT NOT NULL,
    spec_hash         TEXT NOT NULL,
    schema_version    INTEGER NOT NULL,
    template_version  TEXT,
    author            TEXT NOT NULL DEFAULT 'claude' CHECK (author IN ('claude', 'creator')),
    created_at        TEXT NOT NULL,
    UNIQUE (project_id, version)
);
CREATE INDEX edit_specs_hash ON edit_specs (spec_hash);

CREATE TABLE renders (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    spec_hash           TEXT NOT NULL UNIQUE,
    path                TEXT NOT NULL,
    duration_s          REAL,
    bytes               INTEGER,
    qa_report           TEXT,
    contact_sheet_path  TEXT,
    command_log         TEXT,
    created_at          TEXT NOT NULL
);

CREATE TABLE reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    at           TEXT NOT NULL,
    decision     TEXT NOT NULL CHECK (decision IN ('ready', 'approve', 'reject', 'skip', 'reopen')),
    note         TEXT,
    by           TEXT NOT NULL CHECK (by IN ('creator', 'claude')),
    qa_override  INTEGER NOT NULL DEFAULT 0,
    render_id    TEXT
);

CREATE TABLE jobs (
    id               TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    project_id       TEXT REFERENCES projects (id) ON DELETE SET NULL,
    parent_id        TEXT REFERENCES jobs (id) ON DELETE CASCADE,
    state            TEXT NOT NULL DEFAULT 'queued'
                         CHECK (state IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    progress         REAL NOT NULL DEFAULT 0,
    progress_note    TEXT,
    attempt          INTEGER NOT NULL DEFAULT 1,
    payload          TEXT NOT NULL DEFAULT '{}',
    result           TEXT,
    error_code       TEXT,
    error_message    TEXT,
    fix_hint         TEXT,
    idempotency_key  TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    updated_at       TEXT NOT NULL
);
CREATE INDEX jobs_state ON jobs (state, created_at);
CREATE INDEX jobs_idem ON jobs (idempotency_key);
CREATE INDEX jobs_parent ON jobs (parent_id);

CREATE TABLE activity (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    project_id  TEXT,
    source      TEXT NOT NULL CHECK (source IN ('claude', 'creator', 'engine')),
    tool        TEXT,
    summary     TEXT NOT NULL
);
CREATE INDEX activity_at ON activity (at);

CREATE TABLE assets (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('music', 'font', 'logo', 'image', 'sfx')),
    name        TEXT NOT NULL,
    path        TEXT NOT NULL,
    duration_s  REAL,
    tags        TEXT NOT NULL DEFAULT '[]',
    license     TEXT NOT NULL CHECK (length(trim(license)) > 0),  -- no license, no use (NFR-L1)
    source_url  TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (kind, name)
);

CREATE TABLE templates (
    name           TEXT NOT NULL,
    version        INTEGER NOT NULL,
    description    TEXT,
    defaults_json  TEXT NOT NULL,
    read_only      INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (name, version)
);

CREATE TABLE caption_styles (
    name        TEXT PRIMARY KEY,
    style_json  TEXT NOT NULL
);

CREATE TABLE voice_profiles (
    alias          TEXT PRIMARY KEY,
    voice_id       TEXT NOT NULL,
    model_id       TEXT NOT NULL,
    settings_json  TEXT NOT NULL DEFAULT '{}',
    language       TEXT NOT NULL DEFAULT 'en',
    is_default     INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX voice_profiles_one_default ON voice_profiles (is_default) WHERE is_default = 1;

CREATE TABLE style_guide (
    version     INTEGER PRIMARY KEY,
    guide_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE cost_ledger (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    at                    TEXT NOT NULL,
    chars                 INTEGER NOT NULL,
    cache_key             TEXT,
    project_id            TEXT,
    over_budget_approved  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX cost_ledger_at ON cost_ledger (at);

CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Versions are never changed, only added (PRJ-5); history and spending are never rewritten.
CREATE TRIGGER script_versions_immutable BEFORE UPDATE ON script_versions
BEGIN SELECT RAISE(ABORT, 'script versions are never changed; save a new version'); END;
CREATE TRIGGER sentences_immutable BEFORE UPDATE ON sentences
BEGIN SELECT RAISE(ABORT, 'sentences are never changed; save a new script version'); END;
CREATE TRIGGER voiceovers_immutable BEFORE UPDATE ON voiceovers
BEGIN SELECT RAISE(ABORT, 'voiceovers are never changed; make a new one'); END;
CREATE TRIGGER edit_specs_immutable BEFORE UPDATE ON edit_specs
BEGIN SELECT RAISE(ABORT, 'Edit Spec versions are never changed; save a new version'); END;
CREATE TRIGGER reviews_immutable BEFORE UPDATE ON reviews
BEGIN SELECT RAISE(ABORT, 'review history is never changed'); END;
CREATE TRIGGER cost_ledger_immutable BEFORE UPDATE ON cost_ledger
BEGIN SELECT RAISE(ABORT, 'the cost ledger is never changed'); END;
