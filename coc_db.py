"""SQLite storage: tracked players + the ranked battles the poller has seen.

The battlelog only keeps the latest ~46 battles, so the poller saves every new
ranked battle here and /stats merges them with the live log."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "coc.db"
KEEP_DAYS = 21      # battles older than this are deleted
MAX_TRACKED = 150   # max players polled every 5 minutes (keeps API usage low)
MAX_PER_USER = 5    # players one Discord user can register
_FMT = "%Y-%m-%dT%H:%M:%S"


@contextmanager
def _db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with _db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS tracked (
                tag TEXT PRIMARY KEY, name TEXT, added_by INTEGER, added_at TEXT);
            CREATE TABLE IF NOT EXISTS battles (
                tag TEXT NOT NULL, key TEXT NOT NULL, attack INTEGER NOT NULL,
                stars INTEGER NOT NULL, pct INTEGER NOT NULL, time TEXT NOT NULL,
                PRIMARY KEY (tag, key));
            CREATE INDEX IF NOT EXISTS battles_tag_time ON battles (tag, time);
        """)


def add_tracked(tag, name, user_id):
    """True if added, False if it was already tracked."""
    now = datetime.now(timezone.utc).strftime(_FMT)
    with _db() as c:
        cur = c.execute("INSERT OR IGNORE INTO tracked (tag, name, added_by, added_at) VALUES (?,?,?,?)",
                        (tag, name, user_id, now))
        return cur.rowcount == 1


def remove_tracked(tag):
    with _db() as c:
        c.execute("DELETE FROM tracked WHERE tag=?", (tag,))
        c.execute("DELETE FROM battles WHERE tag=?", (tag,))


def get_tracked(tag):
    with _db() as c:
        row = c.execute("SELECT * FROM tracked WHERE tag=?", (tag,)).fetchone()
        return dict(row) if row else None


def list_tracked():
    with _db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM tracked ORDER BY name COLLATE NOCASE")]


def update_name(tag, name):
    with _db() as c:
        c.execute("UPDATE tracked SET name=? WHERE tag=?", (name, tag))


def count_by_user(user_id):
    with _db() as c:
        return c.execute("SELECT COUNT(*) FROM tracked WHERE added_by=?", (user_id,)).fetchone()[0]


def count_tracked():
    with _db() as c:
        return c.execute("SELECT COUNT(*) FROM tracked").fetchone()[0]


def save_battles(tag, battles):
    """battles = list of dicts from coc_api.extract_ranked. Returns how many were NEW."""
    new = 0
    with _db() as c:
        for b in battles:
            cur = c.execute(
                "INSERT OR IGNORE INTO battles (tag, key, attack, stars, pct, time) VALUES (?,?,?,?,?,?)",
                (tag, b["key"], int(b["attack"]), b["stars"], b["pct"],
                 b["time"].astimezone(timezone.utc).strftime(_FMT)))
            new += cur.rowcount
    return new


def load_battles(tag):
    with _db() as c:
        rows = c.execute("SELECT key, attack, stars, pct, time FROM battles WHERE tag=?", (tag,)).fetchall()
    return [{"key": r["key"], "attack": bool(r["attack"]), "stars": r["stars"], "pct": r["pct"],
             "time": datetime.strptime(r["time"], _FMT).replace(tzinfo=timezone.utc)} for r in rows]


def prune():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime(_FMT)
    with _db() as c:
        c.execute("DELETE FROM battles WHERE time < ?", (cutoff,))
        c.execute("DELETE FROM battles WHERE tag NOT IN (SELECT tag FROM tracked)")