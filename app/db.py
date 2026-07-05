"""SQLite catalog. Thread-local connections, WAL mode, dict rows."""
import json
import sqlite3
import threading
from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,            -- 'gdrive' | 'localfs'
  source_id TEXT NOT NULL,         -- Drive file id, or absolute path for localfs
  name TEXT, path TEXT, mime TEXT,
  kind TEXT,                       -- 'photo' | 'video'
  size INTEGER, md5 TEXT,
  created_time TEXT, modified_time TEXT, taken_time TEXT,
  camera TEXT, gps_lat REAL, gps_lon REAL,
  width INTEGER, height INTEGER, duration REAL,
  local_path TEXT, thumb_path TEXT,
  sha256 TEXT, phash TEXT,
  quality REAL,                    -- 0..1 composite quality score
  summary TEXT,                    -- generated description (videos and photos)
  status TEXT DEFAULT 'indexed',   -- indexed|analyzed|error|trashed
  error TEXT,
  UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_kind ON files(kind);

CREATE TABLE IF NOT EXISTS embeddings (
  file_id INTEGER PRIMARY KEY REFERENCES files(id),
  model TEXT, dim INTEGER, vec BLOB
);

CREATE TABLE IF NOT EXISTS faces (
  id INTEGER PRIMARY KEY,
  file_id INTEGER REFERENCES files(id),
  frame_time REAL,                 -- NULL for photos; seconds into video otherwise
  bbox TEXT, det_score REAL,
  vec BLOB,
  cluster_id INTEGER,              -- unsupervised cluster
  person_id INTEGER                -- set once user labels the cluster
);
CREATE INDEX IF NOT EXISTS idx_faces_file ON faces(file_id);
CREATE INDEX IF NOT EXISTS idx_faces_cluster ON faces(cluster_id);

CREATE TABLE IF NOT EXISTS persons (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE,
  cover_face_id INTEGER
);

CREATE TABLE IF NOT EXISTS classifications (
  file_id INTEGER REFERENCES files(id),
  label TEXT, score REAL, method TEXT,   -- 'heuristic' | 'clip'
  PRIMARY KEY (file_id, label)
);

CREATE TABLE IF NOT EXISTS dup_groups (
  id INTEGER PRIMARY KEY,
  kind TEXT,                        -- 'exact' | 'near' | 'video'
  keep_file_id INTEGER,
  explanation TEXT
);
CREATE TABLE IF NOT EXISTS dup_members (
  group_id INTEGER REFERENCES dup_groups(id),
  file_id INTEGER REFERENCES files(id),
  similarity REAL,
  PRIMARY KEY (group_id, file_id)
);

CREATE TABLE IF NOT EXISTS video_frames (
  id INTEGER PRIMARY KEY,
  file_id INTEGER REFERENCES files(id),
  t REAL, phash TEXT, sharpness REAL,
  is_representative INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_vframes_file ON video_frames(file_id);

CREATE TABLE IF NOT EXISTS recommendations (
  id INTEGER PRIMARY KEY,
  file_id INTEGER REFERENCES files(id),
  collection TEXT,                  -- Keep|Review|Duplicate Candidates|Screenshots|Documents|Memes
  action TEXT,                      -- 'keep' | 'review' | 'trash'
  confidence REAL,
  explanation TEXT,
  status TEXT DEFAULT 'pending',    -- pending|approved|rejected|executed|undone
  created_at TEXT DEFAULT (datetime('now')),
  decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_recs_status ON recommendations(status);
CREATE INDEX IF NOT EXISTS idx_recs_coll ON recommendations(collection);

CREATE TABLE IF NOT EXISTS actions_log (
  id INTEGER PRIMARY KEY,
  rec_id INTEGER, file_id INTEGER,
  action TEXT, detail TEXT,
  undo_info TEXT,
  executed_at TEXT DEFAULT (datetime('now')),
  undone_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  type TEXT, status TEXT DEFAULT 'running',  -- running|done|error|cancelled
  progress REAL DEFAULT 0, message TEXT,
  started_at TEXT DEFAULT (datetime('now')), finished_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""


def get_db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(config.DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init_db():
    get_db().executescript(SCHEMA)
    get_db().commit()


def rows(sql, params=()):
    return [dict(r) for r in get_db().execute(sql, params).fetchall()]


def row(sql, params=()):
    r = get_db().execute(sql, params).fetchone()
    return dict(r) if r else None


def execute(sql, params=(), commit=True):
    cur = get_db().execute(sql, params)
    if commit:
        get_db().commit()
    return cur


def get_setting(key, default=None):
    r = row("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default


def set_setting(key, value):
    execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)))
