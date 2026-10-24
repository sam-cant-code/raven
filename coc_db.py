"""SQLite storage: tracked players, the players watched because they share a league group
with a tracked player, and the ranked battles the poller has seen.

The battlelog only keeps the latest ~46 battles, so the poller saves every new ranked
battle here and /battle_log merges them with the live log. Everyone in the league group of a
tracked player is polled until the season ends, which makes the offense/defense leaderboards
complete instead of limited to what is still in each player's log."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "coc.db"
KEEP_DAYS = 21        # battles older than this are deleted
MAX_TRACKED = 100     # max registered players
MAX_PER_USER = 5      # players one Discord user can register
MAX_WATCHED = 10000   # max players polled only because they share a league group (100 per group)
_FMT = "%Y-%m-%dT%H:%M:%S"


def _utc(s):
    return datetime.strptime(s, _FMT).replace(tzinfo=timezone.utc)


def _now():
    return datetime.now(timezone.utc).strftime(_FMT)


@contextmanager
def _db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA synchronous=NORMAL")   # safe with WAL; far fewer disk syncs per commit
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
            CREATE INDEX IF NOT EXISTS battles_time ON battles (time);
            CREATE TABLE IF NOT EXISTS watched (
                tag TEXT PRIMARY KEY, name TEXT, group_key TEXT NOT NULL,
                since TEXT NOT NULL, primed INTEGER NOT NULL DEFAULT 0, expires TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS watched_group ON watched (group_key);
        """)


# ---- tracked (registered) players ----
def add_tracked(tag, name, user_id):
    """True if added, False if it was already tracked."""
    with _db() as c:
        cur = c.execute("INSERT OR IGNORE INTO tracked (tag, name, added_by, added_at) VALUES (?,?,?,?)",
                        (tag, name, user_id, _now()))
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


# ---- watched players (everyone in a tracked player's league group) ----
def watch_group(group_key, members, expires):
    """members = [(tag, name)], group_key = 'grouptag/seasonid', expires = datetime (season end).
    Adds the whole group to the poll list. Returns False if the watch cap would be exceeded."""
    exp = expires.astimezone(timezone.utc).strftime(_FMT)
    now = _now()
    with _db() as c:
        existing = {r["tag"]: r["group_key"] for r in c.execute("SELECT tag, group_key FROM watched")}
        if len(existing) + sum(1 for t, _ in members if t not in existing) > MAX_WATCHED:
            return False
        for tag, name in members:
            if existing.get(tag) == group_key:        # same group: just extend it
                c.execute("UPDATE watched SET name=?, expires=? WHERE tag=?", (name, exp, tag))
            else:                                      # new player or new season: start fresh
                c.execute("INSERT OR REPLACE INTO watched (tag, name, group_key, since, primed, expires) "
                          "VALUES (?,?,?,?,0,?)", (tag, name, group_key, now, exp))
    return True


def drop_group_if_unused(tag):
    """Call before remove_tracked: if no OTHER tracked player shares this player's group,
    stop watching the group."""
    with _db() as c:
        row = c.execute("SELECT group_key FROM watched WHERE tag=?", (tag,)).fetchone()
        if not row:
            return
        left = c.execute("SELECT COUNT(*) FROM watched w JOIN tracked t ON t.tag = w.tag "
                         "WHERE w.group_key=? AND w.tag != ?", (row["group_key"], tag)).fetchone()[0]
        if left == 0:
            c.execute("DELETE FROM watched WHERE group_key=?", (row["group_key"],))


def list_watched():
    """Players polled only because they share a league group with a tracked player
    (registered players are left out, they're listed separately). Ordered by group, then name."""
    with _db() as c:
        return [dict(r) for r in c.execute(
            "SELECT tag, name, group_key FROM watched "
            "WHERE tag NOT IN (SELECT tag FROM tracked) "
            "ORDER BY group_key, name COLLATE NOCASE")]


def watch_status(tags):
    """tag -> {group_key, since (datetime), primed (bool)} for the tags that are watched."""
    tags = list(tags)
    out = {}
    with _db() as c:
        for i in range(0, len(tags), 500):
            chunk = tags[i:i + 500]
            q = ",".join("?" * len(chunk))
            for r in c.execute(f"SELECT tag, group_key, since, primed FROM watched WHERE tag IN ({q})", chunk):
                out[r["tag"]] = {"group_key": r["group_key"], "primed": bool(r["primed"]),
                                 "since": _utc(r["since"])}
    return out


def list_poll_tags():
    """Everyone the poller fetches: registered players + watched group members."""
    with _db() as c:
        return [r[0] for r in c.execute("SELECT tag FROM tracked UNION SELECT tag FROM watched")]


# ---- battles ----
def save_battles_many(by_tag):
    """by_tag = {tag: [battle dicts from coc_api.extract_ranked]}. One transaction.
    Returns how many battles were NEW. A successful save also marks a watched player as primed
    (their log has been captured at least once)."""
    new = 0
    with _db() as c:
        for tag, battles in by_tag.items():
            for b in battles:
                cur = c.execute(
                    "INSERT OR IGNORE INTO battles (tag, key, attack, stars, pct, time) VALUES (?,?,?,?,?,?)",
                    (tag, b["key"], int(b["attack"]), b["stars"], b["pct"],
                     b["time"].astimezone(timezone.utc).strftime(_FMT)))
                new += cur.rowcount
            c.execute("UPDATE watched SET primed=1 WHERE tag=?", (tag,))
    return new


def save_battles(tag, battles):
    return save_battles_many({tag: battles})


def _row_to_battle(r):
    return {"key": r["key"], "attack": bool(r["attack"]), "stars": r["stars"], "pct": r["pct"],
            "time": _utc(r["time"])}


def load_battles(tag):
    with _db() as c:
        rows = c.execute("SELECT key, attack, stars, pct, time FROM battles WHERE tag=?", (tag,)).fetchall()
    return [_row_to_battle(r) for r in rows]


def load_battles_many(tags):
    """{tag: [battle dicts]} for every tag (empty list when nothing is stored)."""
    tags = list(tags)
    out = {t: [] for t in tags}
    with _db() as c:
        for i in range(0, len(tags), 500):
            chunk = tags[i:i + 500]
            q = ",".join("?" * len(chunk))
            for r in c.execute(f"SELECT tag, key, attack, stars, pct, time FROM battles "
                               f"WHERE tag IN ({q})", chunk):
                out[r["tag"]].append(_row_to_battle(r))
    return out


def prune():
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime(_FMT)
    with _db() as c:
        c.execute("DELETE FROM watched WHERE expires < ?", (_now(),))
        c.execute("DELETE FROM battles WHERE time < ?", (cutoff,))
        c.execute("DELETE FROM battles WHERE tag NOT IN "
                  "(SELECT tag FROM tracked UNION SELECT tag FROM watched)")