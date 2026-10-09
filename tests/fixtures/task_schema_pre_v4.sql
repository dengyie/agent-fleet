-- Frozen schema from 182e0340bcbe8560274aa6808d5e2e51e984fcfd, before paused/gates/result additions.

CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  client_token TEXT UNIQUE,
  machine TEXT NOT NULL,
  agent_type TEXT NOT NULL,
  project TEXT NOT NULL,
  instruction TEXT NOT NULL CHECK(length(instruction) <= 2000),
  requested_by TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('queued','leased','running','succeeded','failed','cancelled','expired')),
  attempt_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leases (
  attempt_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  runner_id TEXT NOT NULL,
  nonce TEXT NOT NULL,
  leased_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lease_task ON leases(task_id);
CREATE TABLE IF NOT EXISTS results (
  attempt_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  exit_code INTEGER,
  log_summary TEXT CHECK(length(log_summary) <= 10240),
  diff_stat TEXT CHECK(length(diff_stat) <= 5120),
  duration_s REAL,
  finished_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS result_files (
  attempt_id TEXT NOT NULL,
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  path TEXT NOT NULL,
  content TEXT NOT NULL CHECK(length(content) <= 16384),
  truncated INTEGER NOT NULL,
  redacted INTEGER NOT NULL,
  bytes INTEGER NOT NULL,
  PRIMARY KEY (attempt_id, path)
);
CREATE INDEX IF NOT EXISTS idx_result_files_task ON result_files(task_id);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  task_id TEXT,
  detail TEXT
);
