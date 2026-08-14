PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS snapshots (
 id INTEGER PRIMARY KEY, fetched_at TEXT NOT NULL, url TEXT NOT NULL,
 content_hash TEXT NOT NULL UNIQUE, html TEXT NOT NULL, parser_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lessons (
 id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
 source_row INTEGER NOT NULL, day TEXT NOT NULL, time_text TEXT NOT NULL,
 subject_raw TEXT NOT NULL, subject TEXT NOT NULL, teacher TEXT,
 raw_room TEXT, room TEXT, raw_comment TEXT, location TEXT,
 delivery_mode TEXT NOT NULL, link TEXT, note TEXT,
 UNIQUE(snapshot_id, source_row)
);
CREATE TABLE IF NOT EXISTS glossary (term TEXT PRIMARY KEY, label TEXT NOT NULL, explanation TEXT);
CREATE TABLE IF NOT EXISTS annotation_rules (id TEXT PRIMARY KEY, pattern TEXT NOT NULL, meaning TEXT NOT NULL);
