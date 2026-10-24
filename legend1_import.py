"""Collect Legend I player tags and add them to the bot's watch list.

1. GET /locations                          -> every country
2. GET /locations/{id}/rankings/players    -> top 200 players of each country
3. keep players that look like Legend I    -> league name from the ranking if it has one,
                                              otherwise trophies >= MIN_TROPHIES
4. (default) confirm each one with /players -> leagueTier name must be Legend I
5. coc_db.watch_group("legend1", ...)      -> the bot's poller then saves their battles every cycle

Run it next to the bot files (same folder as coc.db and .env):

    python legend1_import.py --dry-run     # look at the numbers, change nothing
    python legend1_import.py               # add them
    python legend1_import.py --no-verify   # faster, trusts the trophy threshold only
    python legend1_import.py --max 3000    # keep only the top 3000 by trophies

The bot does not need to be stopped. Players stay on the watch list for EXPIRES_DAYS days
(a year), so you only need to run this again if you want to add newly found players.
"""
import argparse
import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent


def _load_env(path):
    """Same tiny .env reader the bot uses. Real environment variables win over the file."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


for _p in (ROOT / ".env", ROOT / ".env.txt"):   # must run before coc_api is imported
    if _p.is_file():
        _load_env(_p)
        break

import coc_api
import coc_db

MIN_TROPHIES = 3000     # ranking entries below this are ignored (used when the ranking has no league name)
GROUP_KEY = "legend1"   # one fixed key, so re-running just extends the same list
EXPIRES_DAYS = 365      # players stay on the watch list this long (re-running the script is optional)
CONCURRENCY = 8         # simultaneous requests (coc_api.COC_RATE still sets the real speed)
OUT_FILE = ROOT / "legend1_tags.txt"


async def run_pool(items, fn, label):
    """Run fn(item) for every item with CONCURRENCY workers. Returns the non-None results."""
    queue = asyncio.Queue()
    for it in items:
        queue.put_nowait(it)
    total, done, out = len(items), 0, []

    async def worker():
        nonlocal done
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            res = await fn(item)
            if res is not None:
                out.append(res)
            done += 1
            if done % 50 == 0 or done == total:
                print(f"  {label}: {done}/{total}", flush=True)

    await asyncio.gather(*(worker() for _ in range(CONCURRENCY)))
    return out


async def fetch_countries():
    data = await coc_api._get("/locations?limit=1000")
    return [loc for loc in data.get("items", []) if loc.get("isCountry")]


async def fetch_ranking(loc):
    try:
        data = await coc_api._get(f"/locations/{loc['id']}/rankings/players?limit=200")
    except coc_api.CocError as e:
        print(f"  skipped {loc.get('name') or loc['id']}: {e}")
        return None
    return data.get("items", [])


async def main(args):
    print("Loading countries...")
    countries = await fetch_countries()
    print(f"{len(countries)} countries")

    print("Loading the top 200 of each country...")
    rankings = await run_pool(countries, fetch_ranking, "countries")

    # tag -> {name, trophies, confirmed}
    found, seen = {}, 0
    for items in rankings:
        for it in items:
            seen += 1
            tag = coc_api.normalize_tag(it.get("tag"))
            if not tag:
                continue
            trophies = it.get("trophies") or 0
            tier = (it.get("leagueTier") or {}).get("name")
            if tier:                                  # the ranking tells us the league: trust it
                if not coc_api.is_legend1(tier):
                    continue
                confirmed = True
            elif trophies >= args.min_trophies:       # otherwise guess from trophies
                confirmed = False
            else:
                continue
            prev = found.get(tag)
            if prev is None or trophies > prev["trophies"]:
                found[tag] = {"name": it.get("name") or "Unknown", "trophies": trophies,
                              "confirmed": confirmed}
    print(f"{seen} ranking entries -> {len(found)} unique candidates")

    if not args.no_verify:
        todo = [t for t, v in found.items() if not v["confirmed"]]
        print(f"Confirming {len(todo)} players with /players...")

        async def verify(tag):
            try:
                p = await coc_api.get_player(tag)
            except coc_api.CocError:
                return tag, None
            tier = (p.get("leagueTier") or {}).get("name")
            return tag, (coc_api.is_legend1(tier), p.get("name"), p.get("trophies"))

        failed = 0
        for tag, res in await run_pool(todo, verify, "players"):
            if res is None:
                failed += 1
                del found[tag]
            elif not res[0]:
                del found[tag]
            else:
                found[tag].update(name=res[1] or found[tag]["name"],
                                  trophies=res[2] or found[tag]["trophies"], confirmed=True)
        if failed:
            print(f"{failed} players couldn't be checked and were left out")

    players = sorted(found.items(), key=lambda kv: -kv[1]["trophies"])
    if args.max:
        players = players[:args.max]
    print(f"Legend I players found: {len(players)}")

    # don't steal players that belong to a league group the bot is already watching
    status = await asyncio.to_thread(coc_db.watch_status, [t for t, _ in players])
    players = [(t, v) for t, v in players if t not in status or status[t]["group_key"] == GROUP_KEY]

    OUT_FILE.write_text("\n".join(f"#{t}\t{v['name']}\t{v['trophies']}" for t, v in players),
                        encoding="utf-8")
    print(f"List saved to {OUT_FILE.name} ({len(players)} players)")

    if args.dry_run:
        print("Dry run: nothing was added to the bot.")
    else:
        await asyncio.to_thread(coc_db.init)
        expires = datetime.now(timezone.utc) + timedelta(days=EXPIRES_DAYS)
        ok = await asyncio.to_thread(coc_db.watch_group, GROUP_KEY,
                                     [(t, v["name"]) for t, v in players], expires)
        if ok:
            await asyncio.to_thread(coc_db.set_trophies_many, [(t, v["trophies"]) for t, v in players])
            print(f"Added {len(players)} players to the watch list. "
                  f"The bot starts polling them on its next cycle.")
        else:
            print(f"Watch list is full (MAX_WATCHED = {coc_db.MAX_WATCHED}). "
                  f"Raise MAX_WATCHED in coc_db.py, or run again with --max N.")
    await coc_api.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Add Legend I players to the bot's watch list")
    ap.add_argument("--dry-run", action="store_true", help="only report and write the tag list")
    ap.add_argument("--no-verify", action="store_true", help="skip the /players check")
    ap.add_argument("--min-trophies", type=int, default=MIN_TROPHIES)
    ap.add_argument("--max", type=int, default=0, help="keep only the top N by trophies")
    asyncio.run(main(ap.parse_args()))