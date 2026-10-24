"""Clash of Clans API helpers.

player tag -> /players (name, league, group tag, season id)
           -> /leaguegroup (rank = position in the members list, plus the full table)
           -> /battlelog (ranked battles inside the current window)
"""
import asyncio
import os
import re
from datetime import datetime, timedelta, timezone

# Clash of Clans API token (a JWT from https://developer.clashofclans.com).
# It is sent as "Authorization: Bearer <token>" on EVERY API request.
COC_API_TOKEN = ""   # put it in the .env file (COC_API_TOKEN=...) or paste it here
COC_API_TOKEN = (COC_API_TOKEN or os.environ.get("COC_API_TOKEN", "")).strip()
BASE_URL = "https://api.clashofclans.com/v1"

# ---- tournament windows (all GMT/UTC) ----
START_DELAY = timedelta(hours=3)   # new season starts 3h after the old one ends
WEEK = timedelta(days=7)

TAG_CHARS = "0289PYLQGRJCUV"       # the only characters a CoC tag can contain
_TAG_RE = re.compile(rf"[{TAG_CHARS}]{{3,12}}")


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


async def _get(path):
    """GET BASE_URL + path (path already contains %23 for every #). Returns parsed JSON."""
    global _session
    import aiohttp
    from yarl import URL
    if not COC_API_TOKEN:
        raise CocError("The bot has no Clash of Clans API token set (COC_API_TOKEN).")
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {COC_API_TOKEN}", "Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=15))
    try:
        async with _session.get(URL(BASE_URL + path, encoded=True)) as r:
            if r.status == 200:
                return await r.json()
            if r.status == 404:
                raise CocError("Not found. Check the player tag.")
            if r.status == 403:
                raise CocError("The API rejected the bot's key (wrong token, or this server's IP isn't allowed for it).")
            if r.status == 429:
                raise CocError("Too many requests right now. Try again in a moment.")
            if r.status == 503:
                raise CocError("The Clash of Clans API is down for maintenance.")
            raise CocError(f"The API returned an error ({r.status}).")
    except asyncio.TimeoutError:
        raise CocError("The Clash of Clans API took too long to answer.")
    except aiohttp.ClientError:
        raise CocError("Couldn't reach the Clash of Clans API.")


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
        except CocError:
            return []   # optional: the image still renders without it
        return g.get("members") or []

    members, items = await asyncio.gather(group_members(), get_battlelog(tag))
    rank = find_rank(members, tag)
    live = extract_ranked(items)
    attacks, defenses = split_window(merge_battles(stored, live), start, end)
    return {"name": name, "league": league, "rank": rank, "attacks": attacks,
            "defenses": defenses, "window": (start, end), "live": live,
            "members": members, "tag": tag}