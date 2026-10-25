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
KEEP_DAYS = 8         # battles older than this are deleted (the ranked window is 7 days)
LEGEND_KEEP_DAYS = 2  # Legend I resets daily, so its battles are only needed for ~2 days
MAX_TRACKED = 100     # max registered players
MAX_PER_USER = 10     # players one Discord user can register
MAX_WATCHED = 15000   # max players polled only because they share a league group or are Legend I
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
            CREATE TABLE IF NOT EXISTS legend_trophies (
                tag TEXT PRIMARY KEY, trophies INTEGER NOT NULL, updated TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS legend_trophies_t ON legend_trophies (trophies);
            CREATE TABLE IF NOT EXISTS larp_uses (
                user_id INTEGER PRIMARY KEY, day TEXT NOT NULL, count INTEGER NOT NULL);
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


# ---- Legend I: trophies of every player in the "legend1" watch group (for the approximate rank) ----
LEGEND_GROUP = "legend1"


def list_legend_tags():
    """Everyone on the Legend I watch list."""
    with _db() as c:
        return [r[0] for r in c.execute("SELECT tag FROM watched WHERE group_key=?", (LEGEND_GROUP,))]


def set_trophies_many(pairs):
    """pairs = [(tag, trophies)]. Overwrites the stored value."""
    now = _now()
    with _db() as c:
        c.executemany("INSERT OR REPLACE INTO legend_trophies (tag, trophies, updated) VALUES (?,?,?)",
                      [(t, int(n), now) for t, n in pairs])


def drop_legend(tags):
    """Players who are no longer Legend I: remove their trophies and stop watching them."""
    tags = list(tags)
    with _db() as c:
        for i in range(0, len(tags), 500):
            chunk = tags[i:i + 500]
            q = ",".join("?" * len(chunk))
            c.execute(f"DELETE FROM legend_trophies WHERE tag IN ({q})", chunk)
            c.execute(f"DELETE FROM watched WHERE group_key=? AND tag IN ({q})", [LEGEND_GROUP, *chunk])


def legend_rank(trophies, tag=None):
    """(rank, players counted): 1 + how many stored players have more trophies. `tag` is left out."""
    tag = tag or ""
    with _db() as c:
        total = c.execute("SELECT COUNT(*) FROM legend_trophies WHERE tag != ?", (tag,)).fetchone()[0]
        ahead = c.execute("SELECT COUNT(*) FROM legend_trophies WHERE trophies > ? AND tag != ?",
                          (trophies, tag)).fetchone()[0]
    return ahead + 1, total


def legend_count():
    """How many Legend I players have a stored trophy count."""
    with _db() as c:
        return c.execute("SELECT COUNT(*) FROM legend_trophies").fetchone()[0]


def legend_top(offset, limit):
    """One page of the Legend I leaderboard, best first: [{tag, name, trophies}].
    Only `limit` rows are read, so a huge leaderboard never gets loaded at once."""
    with _db() as c:
        rows = c.execute(
            "SELECT lt.tag, lt.trophies, COALESCE(w.name, t.name) AS name "
            "FROM legend_trophies lt "
            "LEFT JOIN watched w ON w.tag = lt.tag "
            "LEFT JOIN tracked t ON t.tag = lt.tag "
            "ORDER BY lt.trophies DESC, lt.tag LIMIT ? OFFSET ?", (limit, max(0, offset))).fetchall()
    return [dict(r) for r in rows]


# ---- /larp daily limit ----
def claim_larp(user_id, day, limit):
    """Use up one /larp for `user_id` on `day` (any string that changes each day).
    False if they already used `limit` today. limit <= 0 means unlimited."""
    if limit <= 0:
        return True
    c = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    try:
        c.execute("BEGIN IMMEDIATE")   # no two simultaneous /larp can both slip through
        row = c.execute("SELECT day, count FROM larp_uses WHERE user_id=?", (user_id,)).fetchone()
        used = row[1] if row and row[0] == day else 0
        if used >= limit:
            c.execute("ROLLBACK")
            return False
        c.execute("INSERT OR REPLACE INTO larp_uses (user_id, day, count) VALUES (?,?,?)",
                  (user_id, day, used + 1))
        c.execute("COMMIT")
        return True
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()


def refund_larp(user_id, day):
    """Give back a /larp that failed before an image was sent."""
    with _db() as c:
        c.execute("UPDATE larp_uses SET count = count - 1 WHERE user_id=? AND day=? AND count > 0",
                  (user_id, day))


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
        legend_cutoff = (datetime.now(timezone.utc) - timedelta(days=LEGEND_KEEP_DAYS)).strftime(_FMT)
        c.execute("DELETE FROM battles WHERE time < ? AND tag IN "
                  "(SELECT tag FROM watched WHERE group_key=?)", (legend_cutoff, LEGEND_GROUP))
        c.execute("DELETE FROM battles WHERE tag NOT IN "
                  "(SELECT tag FROM tracked UNION SELECT tag FROM watched)")
        c.execute("DELETE FROM legend_trophies WHERE tag NOT IN (SELECT tag FROM watched)")

def vacuum():
    """Give deleted space back to the disk (SQLite keeps the file size otherwise).
    Needs free disk space about equal to the database size while it runs."""
    c = sqlite3.connect(DB_PATH, timeout=60, isolation_level=None)
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.execute("VACUUM")
    finally:
        c.close()