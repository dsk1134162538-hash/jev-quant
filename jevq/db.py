import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cash(account TEXT PRIMARY KEY, amount REAL);
CREATE TABLE IF NOT EXISTS positions(
  account TEXT, code TEXT, shares INTEGER, cost REAL, buy_price REAL, buy_date TEXT,
  PRIMARY KEY(account, code));
CREATE TABLE IF NOT EXISTS fills(
  id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, code TEXT, side TEXT,
  price REAL, shares INTEGER, fee REAL, ts TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS notices(id TEXT PRIMARY KEY, code TEXT, title TEXT, type TEXT, date TEXT);
CREATE INDEX IF NOT EXISTS idx_notices_code ON notices(code, date);
CREATE TABLE IF NOT EXISTS notice_fetch(date TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS notice_verdicts(id TEXT PRIMARY KEY, label TEXT, confidence REAL, source TEXT);
CREATE TABLE IF NOT EXISTS day_cache(date TEXT, kind TEXT, key TEXT, value TEXT,
  PRIMARY KEY(date, kind, key));
CREATE TABLE IF NOT EXISTS jev_calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, request TEXT, response TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, code TEXT, verdict TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS turnover(ts TEXT PRIMARY KEY, amount REAL);
CREATE TABLE IF NOT EXISTS equity(date TEXT, account TEXT, value REAL, PRIMARY KEY(date, account));
CREATE TABLE IF NOT EXISTS closes(date TEXT, code TEXT, close REAL, PRIMARY KEY(date, code));
CREATE TABLE IF NOT EXISTS concepts(code TEXT, concept TEXT, updated TEXT, PRIMARY KEY(code, concept));
CREATE TABLE IF NOT EXISTS board_events(
  date TEXT, code TEXT, name TEXT, first_seal TEXT, bucket TEXT, concept TEXT,
  next_open_pct REAL, next_close_pct REAL, next_board INTEGER, PRIMARY KEY(date, code));
"""


MIGRATIONS = [("decisions", "trace", "TEXT"), ("notice_verdicts", "probs", "TEXT"),
              ("decisions", "strategy", "TEXT")]


def _migrate(conn: sqlite3.Connection):
    for table, column, kind in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    conn.commit()


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")      # 读写互不阻塞：面板读的时候交易进程照样能写
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def connect_readonly(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15, check_same_thread=False)
