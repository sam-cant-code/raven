"""Clash of Clans API helpers.

player tag -> /players (name, league, group tag, season id)
           -> /leaguegroup (rank = position in the members list, plus the full table)
           -> /battlelog (ranked battles inside the current window)
"""
import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

# Clash of Clans API token (a JWT from https://developer.clashofclans.com).
# It is sent as "Authorization: Bearer <token>" on EVERY API request.
COC_API_TOKEN = ""   # put it in the .env file (COC_API_TOKEN=...) or paste it here
COC_API_TOKEN = (COC_API_TOKEN or os.environ.get("COC_API_TOKEN", "")).strip()


def _collect_tokens():
    """Every API key found in the environment, no duplicates. Accepted names:
        COC_API_TOKEN            (the original single key)
        COC_API_TOKEN_1, COC_API_TOKEN_2, COC_API_TOKEN_3 ...   (one key per variable)
        COC_API_TOKENS           (old style: several keys in one variable, comma/space separated)"""
    numbered = sorted(
        ((int(m.group(1)), v) for k, v in os.environ.items()
         if (m := re.fullmatch(r"COC_API_TOKEN_?(\d+)", k.strip().upper()))),
        key=lambda x: x[0])
    out = []
    for t in [COC_API_TOKEN, *(v for _, v in numbered), *re.split(r"[,\s;]+", os.environ.get("COC_API_TOKENS", ""))]:
        t = t.strip()
        if t and t not in out:
            out.append(t)
    return out


COC_API_TOKENS = _collect_tokens()
BASE_URL = "https://api.clashofclans.com/v1"

# ---- tournament windows (all GMT/UTC) ----
START_DELAY = timedelta(hours=3)   # new season starts 3h after the old one ends
WEEK = timedelta(days=7)

TAG_CHARS = "0289PYLQGRJCUV"       # the only characters a CoC tag can contain
_TAG_RE = re.compile(rf"[{TAG_CHARS}]{{3,12}}")


log = logging.getLogger("cocbot")


class CocError(Exception):
    """Error with a message that is safe to show to the Discord user."""


def normalize_tag(raw):
    """'#8cucyg9' / '8CUCYG9' / '8CUCYG9 ' -> '8CUCYG9' (no #). None if it can't be a tag.
    Also guards the URL: only the characters above ever reach the request path."""
    t = (raw or "").strip().upper().lstrip("#").replace("O", "0")
    return t if _TAG_RE.fullmatch(t) else None


def season_window(season_id):
    """currentLeagueSeasonId is the Monday 05:00 GMT boundary: the season runs from
    08:00 that day until 05:00 GMT the next Monday."""
    base = datetime.fromtimestamp(int(season_id), timezone.utc)
    return base + START_DELAY, base + WEEK


def legend1_window(now=None):
    """Legend I resets every day at 05:00 GMT."""
    now = now or datetime.now(timezone.utc)
    reset = now.replace(hour=5, minute=0, second=0, microsecond=0)
    if now < reset:
        reset -= timedelta(days=1)
    return reset, reset + timedelta(days=1)


def is_legend1(league):
    return re.sub(r"[^a-z0-9]", "", (league or "").lower()) == "legendi"


def parse_ts(s):
    """'20261020T083100.000Z' -> aware datetime (UTC)."""
    return datetime.strptime(s, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)


def extract_ranked(items, now=None):
    """Ranked battles from a battlelog 'items' list, as dicts:
    {key, attack, stars, pct, time}. `key` identifies a battle so it is never stored twice.
    If the API ever leaves out battleTimestamp, the time we first saw the battle is used."""
    now = now or datetime.now(timezone.utc)
    out = []
    for it in items or []:
        if it.get("battleType") != "ranked":
            continue
        try:
            stars, pct = int(it["stars"]), int(it["destructionPercentage"])
        except (KeyError, ValueError, TypeError):
            continue
        attack = bool(it.get("attack"))
        ts = it.get("battleTimestamp")
        try:
            t = parse_ts(ts)
        except (ValueError, TypeError):
            t = None
        opp = it.get("opponentPlayerTag")
        if t:
            key = f"{ts}|{opp}|{int(attack)}"
        else:
            key = f"?|{opp}|{int(attack)}|{stars}|{pct}|{it.get('armyShareCode')}"
        out.append({"key": key, "attack": attack, "stars": stars, "pct": pct, "time": t or now})
    return out


def merge_battles(stored, live):
    """Stored (poller) battles + the live log, without duplicates. Stored wins."""
    by_key = {b["key"]: b for b in (stored or [])}
    for b in live or []:
        by_key.setdefault(b["key"], b)
    return list(by_key.values())


def split_window(battles, start, end):
    """Battles inside [start, end), oldest first -> (attacks, defenses) as (stars, pct) lists."""
    inside = sorted((b for b in battles if start <= b["time"] < end), key=lambda b: (b["time"], b["key"]))
    return ([(b["stars"], b["pct"]) for b in inside if b["attack"]],
            [(b["stars"], b["pct"]) for b in inside if not b["attack"]])


def find_rank(members, tag):
    """Rank = position in the members list (first = 1). None if the player isn't in it."""
    for i, m in enumerate(members or []):
        if normalize_tag(m.get("playerTag")) == tag:
            return i + 1
    return None


# ---- HTTP ----
_session = None
COC_RATE = 20           # max requests per second PER KEY (the API tolerates roughly 30 per key)
KEY_BLOCK_SECONDS = 5   # a key that got a 429 rests this long, other keys carry on
KEY_DEAD_SECONDS = 600  # a key that got a 403 (wrong IP / revoked) is skipped this long


class _Key:
    def __init__(self, token):
        self.token = token
        self.next_slot = 0.0       # earliest time this key may send its next request
        self.blocked_until = 0.0   # 429 cooldown
        self.dead_until = 0.0      # 403 cooldown


_keys = [_Key(t) for t in COC_API_TOKENS]


async def _acquire():
    """Pick the usable key whose next slot comes first, reserve that slot and wait for it.
    With N keys the bot's total speed is N * COC_RATE."""
    loop = asyncio.get_running_loop()
    while True:
        now = loop.time()
        live = [k for k in _keys if k.dead_until <= now]
        if not live:
            raise CocError("The API rejected every key (wrong token, or this server's IP isn't allowed for it).")
        ready = [k for k in live if k.blocked_until <= now]
        if ready:
            k = min(ready, key=lambda k: k.next_slot)
            slot = max(now, k.next_slot)
            k.next_slot = slot + 1.0 / COC_RATE
            if slot > now:
                await asyncio.sleep(slot - now)
            return k
        await asyncio.sleep(max(0.05, min(k.blocked_until for k in live) - now))   # all keys resting


async def _get(path):
    """GET BASE_URL + path (path already contains %23 for every #). Returns parsed JSON.
    A 429 or 403 on one key is retried on another key when there is one."""
    global _session
    import aiohttp
    from yarl import URL
    if not _keys:
        raise CocError("The bot has no Clash of Clans API token set (COC_API_TOKEN).")
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(headers={"Accept": "application/json"},
                                         timeout=aiohttp.ClientTimeout(total=15))
    loop = asyncio.get_running_loop()
    last = CocError("Too many requests right now. Try again in a moment.")
    for _ in range(len(_keys) + 1):
        key = await _acquire()
        try:
            async with _session.get(URL(BASE_URL + path, encoded=True),
                                    headers={"Authorization": f"Bearer {key.token}"}) as r:
                if r.status == 200:
                    return await r.json()
                if r.status == 404:
                    raise CocError("Not found. Check the player tag.")
                if r.status == 429:
                    key.blocked_until = loop.time() + KEY_BLOCK_SECONDS
                    last = CocError("Too many requests right now. Try again in a moment.")
                    continue
                if r.status == 403:
                    last = CocError("The API rejected the bot's key (wrong token, or this server's IP isn't allowed for it).")
                    if len(_keys) > 1:
                        key.dead_until = loop.time() + KEY_DEAD_SECONDS
                        log.warning("API key ...%s was rejected (403); skipping it for %d s",
                                    key.token[-6:], KEY_DEAD_SECONDS)
                        continue
                    raise last
                if r.status == 503:
                    raise CocError("The Clash of Clans API is down for maintenance.")
                raise CocError(f"The API returned an error ({r.status}).")
        except asyncio.TimeoutError:
            raise CocError("The Clash of Clans API took too long to answer.")
        except aiohttp.ClientError:
            raise CocError("Couldn't reach the Clash of Clans API.")
    raise last


async def close():
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


async def get_player(tag):
    return await _get(f"/players/%23{tag}")


async def get_battlelog(tag):
    """Raw battlelog 'items' list."""
    return (await _get(f"/players/%23{tag}/battlelog")).get("items", [])


def group_ref(player):
    """(group_tag, season_id) of the player's current league group, or None
    (no league, Legend I which has no group, or no active season)."""
    league = (player.get("leagueTier") or {}).get("name")
    group_tag = (player.get("currentLeagueGroupTag") or "").lstrip("#")
    season_id = player.get("currentLeagueSeasonId")
    if not league or is_legend1(league) or not group_tag or not season_id:
        return None
    return group_tag, season_id


async def fetch_group_members(group_tag, season_id, tag):
    """(members list, season end datetime) of a league group."""
    g = await _get(f"/leaguegroup/%23{group_tag}/{season_id}?playerTag=%23{tag}")
    return g.get("members") or [], season_window(season_id)[1]


async def fetch_summary(tag, stored=None):
    """tag = normalized tag (no #); stored = battles from coc_db.load_battles (optional).
    Returns name, league, rank, attacks, defenses, window, the live ranked battles,
    and the full league group members list (for the league table)."""
    player = await get_player(tag)
    name = player.get("name") or "Unknown"
    league = (player.get("leagueTier") or {}).get("name")
    if not league:
        raise CocError(f"{name} isn't in a ranked league right now.")

    group_tag = (player.get("currentLeagueGroupTag") or "").lstrip("#")
    season_id = player.get("currentLeagueSeasonId")

    if is_legend1(league):
        start, end = legend1_window()
    elif season_id:
        start, end = season_window(season_id)
    else:
        raise CocError(f"{name} has no active tournament season.")

    async def group_members():
        if is_legend1(league) or not group_tag or not season_id:
            return []
        try:
            g = await _get(f"/leaguegroup/%23{group_tag}/{season_id}?playerTag=%23{tag}")
        except CocError as e:
            log.warning("league group of #%s could not be loaded (no League Table button): %s", tag, e)
            return []   # optional: the image still renders without it
        return g.get("members") or []

    members, items = await asyncio.gather(group_members(), get_battlelog(tag))
    rank = find_rank(members, tag)
    live = extract_ranked(items)
    attacks, defenses = split_window(merge_battles(stored, live), start, end)
    return {"name": name, "league": league, "rank": rank, "attacks": attacks,
            "defenses": defenses, "window": (start, end), "live": live,
            "members": members, "tag": tag,
            "group_key": (group_tag, season_id) if members else None}


# ---- every member of a league group (for the offense / defense leaderboards) ----
GROUP_CACHE_TTL = 300      # seconds a group's battle logs are reused (shared by everyone who clicks)
GROUP_CONCURRENCY = 10     # simultaneous battlelog requests, stays well under the rate limit
_group_cache = {}          # group_key -> (monotonic time, result)


async def fetch_group_battles(members, start, end, key=None, stored=None, live=True):
    """Every league member -> [{tag, name, attacks, defenses, ok, fresh}], where attacks/defenses
    are (stars, pct) lists inside [start, end) and fresh = the ranked battles just fetched.
    stored = {tag: [battle dicts]} (saved by the poller) is always merged in.
    live=False skips the API completely (use it when the poller already has everyone).
    Live results are cached per league group for GROUP_CACHE_TTL seconds."""
    now = time.monotonic()
    use_cache = live and key is not None
    hit = _group_cache.get(key) if use_cache else None
    if hit and now - hit[0] < GROUP_CACHE_TTL:
        return hit[1]
    gate = asyncio.Semaphore(GROUP_CONCURRENCY)
    stored = stored or {}

    async def one(m):
        tag = normalize_tag(m.get("playerTag"))
        if not tag:
            return None
        fresh, ok = [], True
        if live:
            async with gate:
                for attempt in (1, 2):
                    try:
                        fresh = extract_ranked(await get_battlelog(tag))
                        break
                    except CocError:
                        if attempt == 2:
                            ok = False
                        else:
                            await asyncio.sleep(1)
        att, dfn = split_window(merge_battles(stored.get(tag), fresh), start, end)
        return {"tag": tag, "name": m.get("playerName") or "Unknown",
                "attacks": att, "defenses": dfn, "ok": ok, "fresh": fresh}

    result = [r for r in await asyncio.gather(*(one(m) for m in members)) if r]
    if use_cache and sum(not r["ok"] for r in result) <= 5:   # don't cache a badly failed round
        for k in [k for k, (t, _) in _group_cache.items() if now - t > GROUP_CACHE_TTL]:
            del _group_cache[k]
        _group_cache[key] = (now, result)
    return result