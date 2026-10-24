import io, math, asyncio, re, random, os, logging, unicodedata
from datetime import timedelta
from pathlib import Path
from typing import Literal, Optional

import discord
from discord import app_commands
from PIL import Image, ImageDraw, ImageFont


def _load_env(path):
    """Tiny .env reader (no extra library). Real environment variables win over the file."""
    try:
        text = path.read_text(encoding="utf-8-sig")   # utf-8-sig: Windows Notepad adds a hidden BOM
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


# Looks for ".env" (or ".env.txt", which Windows Notepad creates by accident) next to this script.
# This must run before coc_api is imported.
ENV_PATH = next((p for p in (Path(__file__).parent / ".env", Path(__file__).parent / ".env.txt")
                 if p.is_file()), None)
if ENV_PATH:
    _load_env(ENV_PATH)

import coc_api
import coc_db

ROOT = Path(__file__).parent
TOKEN = ""  # Discord bot token: put it in the .env file (DISCORD_TOKEN=...) or paste it here
TOKEN = TOKEN or os.environ.get("DISCORD_TOKEN", "")

# Load ONCE at startup
BASE = Image.open(ROOT / "base.png").convert("RGB")
BASE.load()
FONT_PATH = str(ROOT / "coc-font.ttf")

# ---- NAME: (x, y) = LEFT START of the name, grows to the right ----
NAME_POS, NAME_MAX_W, NAME_SIZE = (200, 70), 450, 40
NAME_MAX_CHARS = 15

# ---- RANK (unchanged, still centered) ----
RANK_POS, RANK_MAX_W, RANK_SIZE = (150, 70), 300, 40

# ---- TOTAL / OFF / DEF (trophy numbers, NOT affected by SHIFT) ----
TOTAL = {"pos": (858, 70),   "anchor": "lm", "size": 48, "max_w": 95,  "fill": "#ffd84a", "outline": 4}
OFF   = {"pos": (805, 165),  "anchor": "rm", "size": 32, "max_w": 100, "fill": "white",   "outline": 3}
DEF   = {"pos": (1680, 165), "anchor": "rm", "size": 32, "max_w": 100, "fill": "white",   "outline": 3}
OUTLINE_COLOR = "#1a1a1a"
SHADOW = 3

# ---- BAR OFFSET: distance between the Attacks bar and the Defenses bar ----
BAR_OFFSET = 875

# ---- SHIFT: moves stars, averages, destruction % and "|" lines (both sides) ----
SHIFT = -12.5

# ---- AVERAGE STARS (edit the DEF values, OFF follows) ----
DEF_AVG_POS = (1300 + SHIFT, 165)
OFF_AVG_POS = (DEF_AVG_POS[0] - BAR_OFFSET, DEF_AVG_POS[1])
AVG_STAR_R = 16
AVG = {"anchor": "lm", "size": 30, "max_w": 90, "fill": "white", "outline": 3}

# ---- AVERAGE DESTRUCTION (pos = left edge of text) ----
DEF_DST_POS = (1450 + SHIFT, 165)
OFF_DST_POS = (DEF_DST_POS[0] - BAR_OFFSET, DEF_DST_POS[1])
DST = {"anchor": "lm", "size": 30, "max_w": 110, "fill": "#7CFC5A", "outline": 3}

# ---- COLORS ----
OFF_AVG_FILL = "#7CFC5A"
OFF_DST_FILL = "#7CFC5A"
DEF_AVG_FILL = "#ff4a4a"
DEF_DST_FILL = "#ff4a4a"

# ---- SEPARATORS "|" ----
DEF_SEPS = (1425 + SHIFT, 1575 + SHIFT)
OFF_SEPS = tuple(x - BAR_OFFSET for x in DEF_SEPS)
SEP_Y = 165
SEP_H = 30
SEP_W = 4
SEP_FILL = "white"
SEP_GAP = 22           # constant gap between a number and a "|" (same for every digit count)
STAR_TXT_GAP = 8       # gap between the avg star icon and its number

# ---- BATTLE BOXES GRID ----
GRID_X_OFF = 46
GRID_X_DEF = 920
GRID_Y0 = 224
COL_PITCH = 144.3     # distance between box columns
ROW_PITCH = 117.7     # distance between box rows
BOX_W, BOX_H = 130, 101
COLS = 6
BASE_ROWS = 4         # rows of empty boxes that exist in base.png
ROWS = 5              # max rows the script can draw (extra rows are added automatically)
MAX_BATTLES = COLS * ROWS   # 30 per side
BOX_RADIUS = 12

# box colors (fill, border) -- muted/dark so they sit well on a dark UI
BOX_NORMAL = None                     # no change: keep the original dark gray box from base.png
BOX_GOLD   = ("#6e5a24", "#a8883a")   # 3-star attack / 0-star defense (muted dark gold)
BOX_RED    = ("#6b2c2a", "#a04a45")   # bad: <50% attack / 100% defense (muted dark red)

# inside-box layout (relative to box top-left)
BOX_TROPHY_TXT_SIZE = 26
BOX_TXT_Y = 30        # y of the "+40" text center
BOX_STAR_Y = 74       # y of the star row center
BOX_STAR_R = 15
BOX_STAR_GAP = 36
STAR_ON = "#ffffff"
STAR_OFF = "#7d7773"


# ---- LEAGUES: attacks/defenses per week + % promoted / % demoted ----
# (name, attacks per week, % promoted, % demoted). None = not filled in yet.
def _grp(prefix, nums, attacks, promos=None, demo=None):
    promos = promos or [None] * len(nums)
    return [(f"{prefix} {n}", attacks, p, demo) for n, p in zip(nums, promos)]


# Order matters: it's the order shown in the slash-command autocomplete.
LEAGUES = (
    _grp("Skeleton", (1, 2, 3), 6)
    + _grp("Barbarian", (4, 5, 6), 6)
    + _grp("Archer", (7, 8, 9), 8)
    + _grp("Wizard", (10, 11, 12), 8)
    + _grp("Valkyrie", (13, 14, 15), 10)
    + _grp("Witch", (16, 17, 18), 10)
    + _grp("Golem", (19, 20, 21), 10)
    + _grp("Pekka", (22, 23, 24), 12, [30, 50, 30], 15)
    + _grp("Titan", (25, 26, 27), 12, [30, 50, 40], 15)
    + _grp("Dragon", (28, 29, 30), 14, [30, 25, 25], 15)
    + _grp("Electro", (31, 32, 33), 18, [20, 20, 15], 15)
    + [("Legend III", 24, 5, 15),
       ("Legend II", 30, 3, 15),
       ("Legend I", 30, None, None)]   # <-- Legend I: CHANGE these numbers
)
LEAGUE_NAMES = [l[0] for l in LEAGUES]


def _norm(s):
    """Lowercase letters/digits only, so 'Pekka 24', 'pekka24' and 'P.E.K.K.A 24' all match."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


LEAGUE_ATTACKS = {_norm(n): a for n, a, _, _ in LEAGUES}
LEAGUE_MOVE = {_norm(n): (p, d) for n, _, p, d in LEAGUES}   # name -> (% promoted, % demoted)
PLAYERS_PER_LEAGUE = 100


def league_attacks(league):
    """Attacks/defenses per week for a league. Unknown names fall back to MAX_BATTLES."""
    return min(LEAGUE_ATTACKS.get(_norm(league or DEFAULT_LEAGUE), MAX_BATTLES), MAX_BATTLES)


def rank_number(rank):
    """'#5' / '5' / 'Rank 5' -> 5, or None if there's no number."""
    m = re.search(r"\d+", str(rank))
    return int(m.group()) if m else None


def movement_from_rank(league, rank):
    """Every league has 100 players: rank <= promoted% -> promotion,
    rank in the bottom demoted% -> demotion, otherwise neutral."""
    promo, demo = LEAGUE_MOVE.get(_norm(league), (None, None))
    n = rank_number(rank)
    if n is None:
        return "neutral"
    if promo is not None and n <= promo:
        return "promotion"
    if demo is not None and n > PLAYERS_PER_LEAGUE - demo:
        return "demotion"
    return "neutral"


# ---- TROPHY ICON ----
# Drop these next to the script (.webp or .png, transparent background):
#   trophy.webp   star_full.webp   star_empty.webp
# Any that are missing fall back to the hand-drawn version.
BOX_STAR_H = 34        # star image height inside each battle box
AVG_STAR_H = 36        # star image height next to the average stars
BOX_ICON_H = 30        # trophy height inside each battle box

# ---- LEAGUE ICON (league_icon.webp / .png), centered at LEAGUE_POS ----
LEAGUE_POS = (1500, 70)   # CENTER of the icon -- use the grid in test mode to line it up
LEAGUE_H = 80             # icon height, aspect ratio kept
# League name text, drawn to the RIGHT of the icon (left-aligned, shrinks to fit max_w)
DEFAULT_LEAGUE = "Legend III"
LEAGUE_TXT = {"anchor": "lm", "size": 34, "max_w": 190, "fill": "white", "outline": 3}
LEAGUE_TXT_GAP = 12       # space between icon and text
LEAGUE_MAX_CHARS = 16

# ---- PROMOTION / DEMOTION / NEUTRAL BANNER, centered at BANNER_POS ----
BANNER_FILES = {"promotion": "promo_banner",
                "demotion": "demo_banner",
                "neutral": "neutral_banner"}   # .webp or .png next to the script
DEFAULT_MOVEMENT = "neutral"
BANNER_POS = (75, 70)   # CENTER of the banner -- line it up with the grid in test mode
BANNER_H = 125             # banner height, aspect ratio kept
# If the old red banner is baked into base.png, paint over it first: (x0, y0, x1, y1, "#color"),
# or just remove it from base.png and leave this None.
BANNER_COVER = None
# If the hamburger menu is baked into base.png, paint over it first: (x0, y0, x1, y1, "#color").
# Pick the color to match the header bar. Leave None if you removed it from base.png.
LEAGUE_COVER = None
HDR_ICONS = False      # base.png already has the trophy next to OFF / DEF totals
HDR_ICON_H = 38
HDR_ICON_GAP = 8       # space between the OFF/DEF number and its icon


def _load_asset(stem):
    for ext in (".webp", ".png"):
        p = ROOT / (stem + ext)
        if p.exists():
            return Image.open(p).convert("RGBA")
    return None


ASSETS = {s: _load_asset(s) for s in ("trophy", "star_full", "star_empty", "league_icon", *BANNER_FILES.values())}
_asset_cache = {}


def get_asset(stem, h):
    """Asset scaled to height h (aspect kept), or None if the file doesn't exist."""
    src = ASSETS.get(stem)
    if src is None:
        return None
    key = (stem, h)
    if key not in _asset_cache:
        w = max(1, round(src.width * h / src.height))
        _asset_cache[key] = src.resize((w, h), Image.LANCZOS)
    return _asset_cache[key]


def get_icon(h):
    return get_asset("trophy", h)


# ---- per-league icons: put the images in the "league_icons" folder next to the script ----
# One file per league FAMILY (all levels share it):
#   league_icons/legend.webp   (Legend I, II, III)
#   league_icons/electro.webp  (Electro 31, 32, 33)
#   league_icons/dragon.webp   league_icons/titan.webp   league_icons/pekka.webp
#   league_icons/skeleton.webp barbarian, archer, wizard, valkyrie, witch, golem ...
# A file for one exact league wins over the family file, e.g. league_icons/legend_III.webp.
# Capitals, spaces, "_" and "." don't matter. .png or .webp both work.
# If nothing matches, the script falls back to league_icon.webp/.png (the old single icon).
LEAGUE_ICON_DIR = ROOT / "league_icons"
_league_icon_cache = {}


def league_slug(text):
    """'P.E.K.K.A 22' -> 'pekka22', 'legend_II' -> 'legendii' (letters/digits only, lowercase)."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def league_family(league):
    """'Dragon 28' -> 'Dragon', 'Legend III' -> 'Legend'."""
    return re.sub(r"\s+(\d+|[IVXivx]+)$", "", (league or "").strip())


def _index_league_files():
    idx = {}
    if LEAGUE_ICON_DIR.is_dir():
        for f in sorted(LEAGUE_ICON_DIR.iterdir()):
            if f.suffix.lower() in (".webp", ".png"):
                idx.setdefault(league_slug(f.stem), f)
    return idx


LEAGUE_FILES = _index_league_files()


def get_league_icon(league, h):
    key = (league_slug(league), h)
    if key not in _league_icon_cache:
        f = LEAGUE_FILES.get(key[0]) or LEAGUE_FILES.get(league_slug(league_family(league)))
        if f is None:
            _league_icon_cache[key] = get_asset("league_icon", h)   # fallback (may be None)
        else:
            src = Image.open(f).convert("RGBA")
            w = max(1, round(src.width * h / src.height))
            _league_icon_cache[key] = src.resize((w, h), Image.LANCZOS)
    return _league_icon_cache[key]


def put_star(img, d, cx, cy, h, on, fb_fill, fb_r):
    """Star image centered at (cx, cy); falls back to a drawn star if the file is missing."""
    icon = get_asset("star_full" if on else "star_empty", h)
    if icon:
        img.paste(icon, (int(cx - icon.width / 2), int(cy - icon.height / 2)), icon)
    else:
        draw_star(d, cx, cy, fb_r, fill=fb_fill)


def put_icon(img, d, x, cy, h):
    """Trophy icon with its LEFT edge at x, vertically centered on cy. Returns its width."""
    icon = get_icon(h)
    if icon:
        img.paste(icon, (int(x), int(cy - icon.height / 2)), icon)
        return icon.width
    d_w = 28
    draw_trophy(d, x + d_w / 2, cy, h / 30)
    return d_w


def parse_battles(text, limit=MAX_BATTLES):
    """'3:100 2:75, 1:60' -> ([(3,100),(2,75),(1,60)], None). Format is stars:destruction%."""
    if not text:
        return [], None
    out = []
    for tok in re.split(r"[,\s;]+", text.strip()):
        if not tok:
            continue
        m = re.fullmatch(r"([0-3])\s*[:/]\s*(\d{1,3}(?:\.\d+)?)%?", tok)
        if not m:
            return None, f"Bad battle `{tok}`. Use stars:destruction% like `2:75` (stars 0-3)."
        stars, pct = int(m.group(1)), float(m.group(2))
        if pct > 100 or (stars == 3) != (pct == 100):
            return None, f"Bad battle `{tok}`: 3 stars needs exactly 100%, and 100% is always 3 stars."
        out.append((stars, pct))
    if len(out) > limit:
        return None, f"Max {limit} battles per side in this league."
    return out, None


def attack_trophies(stars, pct):
    """Legend League attacker trophies from stars + destruction %."""
    if stars == 3:
        return 40
    if stars == 2:   # 16 at 50%, +1 per full 3% above 50%
        return min(32, 16 + int((max(pct, 50) - 50) // 3))
    if stars == 1:   # 5 at 1%, +1 per full 9% above 1%
        return min(15, 5 + int(max(pct - 1, 0) // 9))
    return min(4, int(pct // 10))   # 0 stars: +1 per full 10%


def defense_trophies(stars, pct):
    """Defender gets 40 minus what the attacker earned (0 stars = full 40)."""
    if stars == 0:
        return 40
    return 40 - attack_trophies(stars, pct)


def random_battles(kind, count=None, limit=MAX_BATTLES):
    """Random list of (stars, destruction%). kind = 'off' or 'def'.
    count defaults to a full grid for the league (limit)."""
    n = limit if count is None else max(0, min(count, limit))
    out = []
    for _ in range(n):
        weights = [60, 20, 15, 5] if kind == "off" else [25, 45, 20, 10]
        stars = random.choices([3, 2, 1, 0], weights=weights)[0]
        pct = {3: 100, 2: random.randint(55, 99),
               1: random.randint(50, 90), 0: random.randint(10, 49)}[stars]
        out.append((stars, float(pct)))
    return out


def derive_stats(attacks, defenses):
    """Everything is calculated from the battle lists: trophy totals, average stars,
    average destruction %. Empty list -> zeros."""
    vals = {"off_total": 0, "off_avg": 0.0, "off_dest": 0.0,
            "def_total": 0, "def_avg": 0.0, "def_dest": 0.0}
    if attacks:
        vals["off_total"] = sum(attack_trophies(s, p) for s, p in attacks)
        vals["off_avg"] = sum(s for s, _ in attacks) / len(attacks)
        vals["off_dest"] = sum(p for _, p in attacks) / len(attacks)
    if defenses:
        vals["def_total"] = sum(defense_trophies(s, p) for s, p in defenses)
        vals["def_avg"] = sum(s for s, _ in defenses) / len(defenses)
        vals["def_dest"] = sum(p for _, p in defenses) / len(defenses)
    return vals


def resolve(name, rank, attacks_txt, defenses_txt, randomize=False, league=None):
    """Returns (render kwargs, error)."""
    league = (league or DEFAULT_LEAGUE)[:LEAGUE_MAX_CHARS]
    limit = league_attacks(league)
    n = rank_number(rank)
    if n is not None and not 1 <= n <= PLAYERS_PER_LEAGUE:
        return None, f"Rank must be between 1 and {PLAYERS_PER_LEAGUE}."
    movement = movement_from_rank(league, rank)

    attacks, e1 = parse_battles(attacks_txt or "", limit)
    if e1:
        return None, f"attacks: {e1}"
    defenses, e2 = parse_battles(defenses_txt or "", limit)
    if e2:
        return None, f"defenses: {e2}"
    if randomize:
        attacks = random_battles("off", None, limit)
        defenses = random_battles("def", None, limit)
    vals = derive_stats(attacks, defenses)
    vals.update(name=str(name)[:NAME_MAX_CHARS], rank=str(rank)[:10],
                attacks=attacks, defenses=defenses,
                league=league, movement=movement, limit=limit)
    return vals, None


sem = asyncio.Semaphore(1)   # one render at a time keeps RAM low


def draw_text(d, text, pos, max_w, size, anchor="mm"):
    while size > 20:
        font = ImageFont.truetype(FONT_PATH, size)
        if d.textlength(text, font=font) <= max_w:
            break
        size -= 2
    d.text((pos[0], pos[1] + 4), text, font=font, anchor=anchor,
           fill="black", stroke_width=4, stroke_fill="black")
    d.text(pos, text, font=font, anchor=anchor,
           fill="white", stroke_width=4, stroke_fill="#1a1a1a")


def draw_cfg(d, text, cfg):
    pos, size, anchor = cfg["pos"], cfg["size"], cfg["anchor"]
    while size > 10:
        font = ImageFont.truetype(FONT_PATH, size)
        if d.textlength(text, font=font) <= cfg["max_w"]:
            break
        size -= 2
    o = cfg["outline"]
    if SHADOW:
        d.text((pos[0], pos[1] + SHADOW), text, font=font, anchor=anchor,
               fill="black", stroke_width=o + 1, stroke_fill="black")
    d.text(pos, text, font=font, anchor=anchor,
           fill=cfg["fill"], stroke_width=o, stroke_fill=OUTLINE_COLOR)


def draw_star(d, cx, cy, r, fill="#ffd84a", outline=OUTLINE_COLOR):
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.45
        pts.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    d.polygon(pts, fill=fill, outline=outline)


def draw_trophy(d, cx, cy, s=1.0):
    """Small gold trophy cup centered at (cx, cy). s = scale."""
    gold, dark = "#f2c230", "#8a5d10"
    # handles
    d.arc([cx - 13 * s, cy - 9 * s, cx - 3 * s, cy + 3 * s], 90, 270, fill=dark, width=max(2, int(2 * s)))
    d.arc([cx + 3 * s, cy - 9 * s, cx + 13 * s, cy + 3 * s], 270, 90, fill=dark, width=max(2, int(2 * s)))
    # cup
    d.polygon([(cx - 9 * s, cy - 11 * s), (cx + 9 * s, cy - 11 * s),
               (cx + 6 * s, cy + 3 * s), (cx + 2 * s, cy + 6 * s),
               (cx - 2 * s, cy + 6 * s), (cx - 6 * s, cy + 3 * s)],
              fill=gold, outline=dark)
    # stem + base
    d.rectangle([cx - 2 * s, cy + 6 * s, cx + 2 * s, cy + 10 * s], fill=gold, outline=dark)
    d.rectangle([cx - 7 * s, cy + 10 * s, cx + 7 * s, cy + 13 * s], fill=gold, outline=dark)


def draw_avg(img, d, value, center, fill=None):
    x, y = center
    put_star(img, d, x, y, AVG_STAR_H, True, "#ffd84a", AVG_STAR_R)
    cfg = {**AVG, "pos": (x + AVG_STAR_R + 12, y)}
    if fill:
        cfg["fill"] = fill
    draw_cfg(d, f"{value:.2f}", cfg)


def draw_dst(d, value, pos, fill=None):
    cfg = {**DST, "pos": pos}
    if fill:
        cfg["fill"] = fill
    draw_cfg(d, f"{value:.1f}%", cfg)


def draw_sep(d, x):
    half = SEP_H // 2
    d.line([(x, SEP_Y - half), (x, SEP_Y + half)], fill=OUTLINE_COLOR, width=SEP_W + 4)
    d.line([(x, SEP_Y - half), (x, SEP_Y + half)], fill=SEP_FILL, width=SEP_W)


def fit_font(d, text, cfg):
    """Same auto-shrink as draw_cfg, but returns the font so we can measure the text."""
    size = cfg["size"]
    while size > 10:
        font = ImageFont.truetype(FONT_PATH, size)
        if d.textlength(text, font=font) <= cfg["max_w"]:
            break
        size -= 2
    return font


def place_right(d, text, cfg, vright, y):
    """Draw text so its VISIBLE right edge (incl. outline) is at vright.
    Returns the visible left edge, so the next item can be placed a fixed gap further left."""
    font = fit_font(d, text, cfg)
    o = cfg["outline"]
    w = d.textlength(text, font=font)
    draw_cfg(d, text, {**cfg, "pos": (vright - o, y), "anchor": "rm"})
    return vright - w - 2 * o


def draw_stat_row(img, d, total_cfg, total_text, avg, dest, avg_fill, dst_fill):
    """[star avg] | [dest%] | [total]  -- laid out right-to-left from the total, which stays
    anchored next to the trophy in base.png. Every gap is exactly SEP_GAP, whatever the digits."""
    y = total_cfg["pos"][1]
    o = total_cfg["outline"]
    font = fit_font(d, total_text, total_cfg)
    left = total_cfg["pos"][0] - d.textlength(total_text, font=font) - o   # visible left of total
    half = (SEP_W + 4) / 2                                                 # half the "|" thickness

    sep2 = left - SEP_GAP - half
    draw_sep(d, sep2)
    left = place_right(d, f"{dest:.1f}%", {**DST, "fill": dst_fill}, sep2 - half - SEP_GAP, y)

    sep1 = left - SEP_GAP - half
    draw_sep(d, sep1)
    left = place_right(d, f"{avg:.2f}", {**AVG, "fill": avg_fill}, sep1 - half - SEP_GAP, y)

    put_star(img, d, left - STAR_TXT_GAP - AVG_STAR_R, y, AVG_STAR_H, True, "#ffd84a", AVG_STAR_R)


def box_colors(kind, pct):
    """kind = 'off' or 'def'.
    Attack : 100% -> gold, 50-99% -> normal, <50% -> red.
    Defense: 100% -> red,  50-99% -> normal, <50% -> gold."""
    if kind == "off":
        return BOX_GOLD if pct >= 100 else (BOX_RED if pct < 50 else BOX_NORMAL)
    return BOX_RED if pct >= 100 else (BOX_GOLD if pct < 50 else BOX_NORMAL)


def draw_battle_box(img, d, x, y, stars, trophies, pct, kind):
    colors = box_colors(kind, pct)
    if colors is not None:  # neutral boxes are left untouched (original dark gray)
        fill, border = colors
        d.rounded_rectangle([x, y, x + BOX_W, y + BOX_H], radius=BOX_RADIUS,
                            fill=fill, outline=border, width=3)
    cx = x + BOX_W / 2
    # trophy number + icon
    txt = "0" if trophies == 0 else f"{trophies:+d}"
    font = ImageFont.truetype(FONT_PATH, BOX_TROPHY_TXT_SIZE)
    tw = d.textlength(txt, font=font)
    icon = get_icon(BOX_ICON_H)
    icon_w = icon.width if icon else 28
    total_w = tw + 8 + icon_w
    tx = cx - total_w / 2
    ty = y + BOX_TXT_Y
    d.text((tx, ty + 2), txt, font=font, anchor="lm", fill="black", stroke_width=3, stroke_fill="black")
    d.text((tx, ty), txt, font=font, anchor="lm", fill="white", stroke_width=3, stroke_fill=OUTLINE_COLOR)
    put_icon(img, d, tx + tw + 8, ty, BOX_ICON_H)
    # 3 stars
    sy = y + BOX_STAR_Y
    for i in range(3):
        sx = cx + (i - 1) * BOX_STAR_GAP
        put_star(img, d, sx, sy, BOX_STAR_H, i < stars,
                 STAR_ON if i < stars else STAR_OFF, BOX_STAR_R)


def draw_battles(img, d, battles, x0, kind):
    for i, (stars, pct) in enumerate(battles[:MAX_BATTLES]):
        r, c = divmod(i, COLS)
        x = x0 + c * COL_PITCH
        y = GRID_Y0 + r * ROW_PITCH
        trophies = attack_trophies(stars, pct) if kind == "off" else defense_trophies(stars, pct)
        draw_battle_box(img, d, x, y, stars, trophies, pct, kind)


BOTTOM_MARGIN = 24   # empty space under the last box row
# FIXED output size: every league renders at exactly this size (height fits the max 5 box rows)
OUT_W = BASE.width
OUT_H = int(GRID_Y0 + (ROWS - 1) * ROW_PITCH + BOX_H + BOTTOM_MARGIN)


def make_canvas(limit):
    """Canvas sized for the league: crops base.png when the league has fewer box rows than
    base.png, extends it when it needs more, and hides the boxes the league doesn't have."""
    rows = max(1, min(ROWS, math.ceil(limit / COLS)))
    w, h = BASE.size

    if rows <= BASE_ROWS:
        bottom = int(GRID_Y0 + (rows - 1) * ROW_PITCH + BOX_H + 8)   # inside the gap, above the next row
        img = BASE.crop((0, 0, w, min(h, bottom)))
    else:
        extra_px = int(round((rows - BASE_ROWS) * ROW_PITCH))
        cut = min(int(GRID_Y0 + BASE_ROWS * ROW_PITCH), h - 1)   # background gap below last box row
        img = Image.new("RGB", (w, h + extra_px))
        img.paste(BASE.crop((0, 0, w, cut)), (0, 0))
        img.paste(BASE.crop((0, cut - 1, w, cut)).resize((w, extra_px), Image.NEAREST), (0, cut))
        img.paste(BASE.crop((0, cut, w, h)), (0, cut + extra_px))

        # extra empty boxes: copy a real box from base.png (keeps bevel/gradient), rounded mask
        pad = 3
        bw, bh = BOX_W + 2 * pad, BOX_H + 2 * pad
        mask = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, bw - 1, bh - 1], radius=BOX_RADIUS + pad, fill=255)
        for x0 in (GRID_X_OFF, GRID_X_DEF):
            ty = GRID_Y0 + (BASE_ROWS - 1) * ROW_PITCH
            tpl = BASE.crop((int(x0 - pad), int(ty - pad), int(x0 - pad) + bw, int(ty - pad) + bh))
            for r in range(BASE_ROWS, rows):
                for c in range(COLS):
                    img.paste(tpl, (int(x0 + c * COL_PITCH) - pad, int(GRID_Y0 + r * ROW_PITCH) - pad), mask)

    # hide boxes beyond the league's attack count (e.g. 8 attacks -> row 2 only keeps 2 boxes)
    pad = 3
    for x0 in (GRID_X_OFF, GRID_X_DEF):
        for i in range(limit, min(rows, BASE_ROWS) * COLS):
            r, c = divmod(i, COLS)
            x = int(x0 + c * COL_PITCH)
            y = int(GRID_Y0 + r * ROW_PITCH)
            # plain background: strip from the gap row under this box (same x range), stretched over the box
            gap = BASE.crop((x - pad, y + BOX_H + 5, x + BOX_W + pad, y + BOX_H + 11))
            img.paste(gap.resize((BOX_W + 2 * pad, BOX_H + 2 * pad), Image.NEAREST), (x - pad, y - pad))

    # force the fixed size: crop if too tall, pad with stretched background if too short
    if img.height > OUT_H:
        img = img.crop((0, 0, OUT_W, OUT_H))
    elif img.height < OUT_H:
        out = Image.new("RGB", (OUT_W, OUT_H))
        out.paste(img, (0, 0))
        out.paste(img.crop((0, img.height - 1, OUT_W, img.height)).resize((OUT_W, OUT_H - img.height), Image.NEAREST),
                  (0, img.height))
        img = out
    return img


def render(name: str, rank: str, off_total: int, def_total: int,
           off_avg: float, def_avg: float,
           off_dest: float, def_dest: float,
           attacks=None, defenses=None, league: str = DEFAULT_LEAGUE,
           movement: str = DEFAULT_MOVEMENT, limit: int = 24, grid: bool = False) -> bytes:
    total = off_total + def_total
    img = make_canvas(limit)
    d = ImageDraw.Draw(img)

    if grid:
        small = ImageFont.truetype(FONT_PATH, 14)
        for x in range(0, img.width, 100):
            d.line([(x, 0), (x, img.height)], fill="#00ff00", width=1)
            d.text((x + 3, 3), str(x), font=small, fill="#00ff00")
        for y in range(0, img.height, 100):
            d.line([(0, y), (img.width, y)], fill="#00ffff", width=1)
            d.text((3, y + 3), str(y), font=small, fill="#00ffff")

    draw_text(d, name, NAME_POS, NAME_MAX_W, NAME_SIZE, anchor="lm")
    draw_text(d, rank, RANK_POS, RANK_MAX_W, RANK_SIZE)
    draw_cfg(d, str(total), TOTAL)
    draw_cfg(d, f"{off_total:+d}", OFF)
    draw_cfg(d, f"{def_total:+d}", DEF)
    if HDR_ICONS:  # OFF / DEF numbers are right-anchored, so the icon goes just after them
        put_icon(img, d, OFF["pos"][0] + HDR_ICON_GAP, OFF["pos"][1], HDR_ICON_H)
        put_icon(img, d, DEF["pos"][0] + HDR_ICON_GAP, DEF["pos"][1], HDR_ICON_H)
    draw_stat_row(img, d, OFF, f"{off_total:+d}", off_avg, off_dest, OFF_AVG_FILL, OFF_DST_FILL)
    draw_stat_row(img, d, DEF, f"{def_total:+d}", def_avg, def_dest, DEF_AVG_FILL, DEF_DST_FILL)

    if LEAGUE_COVER:
        d.rectangle(LEAGUE_COVER[:4], fill=LEAGUE_COVER[4])
    league_img = get_league_icon(league, LEAGUE_H)
    icon_half_w = 0
    if league_img:
        img.paste(league_img, (int(LEAGUE_POS[0] - league_img.width / 2),
                               int(LEAGUE_POS[1] - league_img.height / 2)), league_img)
        icon_half_w = league_img.width / 2
    if league:
        draw_cfg(d, league, {**LEAGUE_TXT,
                             "pos": (LEAGUE_POS[0] + icon_half_w + LEAGUE_TXT_GAP, LEAGUE_POS[1])})

    if BANNER_COVER:
        d.rectangle(BANNER_COVER[:4], fill=BANNER_COVER[4])
    banner = get_asset(BANNER_FILES.get(movement, ""), BANNER_H)
    if banner:
        img.paste(banner, (int(BANNER_POS[0] - banner.width / 2),
                           int(BANNER_POS[1] - banner.height / 2)), banner)

    draw_battles(img, d, attacks or [], GRID_X_OFF, "off")
    draw_battles(img, d, defenses or [], GRID_X_DEF, "def")

    buf = io.BytesIO()
    img.save(buf, "PNG")
    img.close()
    return buf.getvalue()


# ---- discord.py part ----
log = logging.getLogger("cocbot")
POLL_INTERVAL = 900   # seconds between battlelog polls (15 minutes)


class CocClient(discord.Client):
    async def setup_hook(self):
        await asyncio.to_thread(coc_db.init)
        await tree.sync()
        self._poller = asyncio.create_task(poll_loop())

    async def close(self):
        poller = getattr(self, "_poller", None)
        if poller:
            poller.cancel()
        await coc_api.close()
        await super().close()


client = CocClient(
    intents=discord.Intents.none(),
    member_cache_flags=discord.MemberCacheFlags.none(),
    max_messages=None,
)
tree = app_commands.CommandTree(client)

NameT = app_commands.Range[str, 1, NAME_MAX_CHARS]
BattlesT = app_commands.Range[str, 0, 500]
TagT = app_commands.Range[str, 3, 14]


def safe_name(name):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(name or "Unknown"))


# ---- background poller: saves every new ranked battle of tracked players AND of everyone
# in their league group, so the offense/defense leaderboards are complete ----
NAME_REFRESH_EVERY = 4    # every 4th poll (about hourly at 15 min): names, league groups and pruning are refreshed
POLL_CONCURRENCY = 8      # simultaneous battlelog requests (coc_api.COC_RATE sets the real speed)


def group_pairs(members):
    pairs = [(coc_api.normalize_tag(m.get("playerTag")), m.get("playerName") or "Unknown") for m in members]
    return [(t, n) for t, n in pairs if t]


async def watch_player_group(player, tag, seen=None):
    """Make sure every member of the player's league group is polled until the season ends."""
    ref = coc_api.group_ref(player)
    if not ref or (seen is not None and ref in seen):
        return
    if seen is not None:
        seen.add(ref)
    try:
        members, end = await coc_api.fetch_group_members(ref[0], ref[1], tag)
    except coc_api.CocError as e:
        log.warning("group of #%s: %s", tag, e)
        return
    ok = await asyncio.to_thread(coc_db.watch_group, f"{ref[0]}/{ref[1]}", group_pairs(members), end)
    if not ok:
        log.warning("watch list full (%d): group %s is not tracked", coc_db.MAX_WATCHED, ref[0])


async def sync_groups(rows):
    """Refresh names and (re)register the league group of every tracked player.
    Players in the same group are only fetched once."""
    seen = set()
    for row in rows:
        try:
            player = await coc_api.get_player(row["tag"])
        except coc_api.CocError as e:
            log.warning("sync #%s: %s", row["tag"], e)
            continue
        name = player.get("name")
        if name and name != row["name"]:
            await asyncio.to_thread(coc_db.update_name, row["tag"], name)
        await watch_player_group(player, row["tag"], seen)


async def poll_once(refresh=False):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    rows = await asyncio.to_thread(coc_db.list_tracked)
    if refresh:
        await sync_groups(rows)
    tags = await asyncio.to_thread(coc_db.list_poll_tags)
    gate = asyncio.Semaphore(POLL_CONCURRENCY)
    new_total = 0

    async def one(tag):
        nonlocal new_total
        async with gate:
            try:
                items = await coc_api.get_battlelog(tag)
            except coc_api.CocError as e:
                log.warning("poll #%s: %s", tag, e)
                return
        try:
            n = await asyncio.to_thread(coc_db.save_battles, tag, coc_api.extract_ranked(items))
        except Exception:
            log.exception("saving #%s failed", tag)
            return
        new_total += n

    # worker pool: only POLL_CONCURRENCY tasks exist at a time, instead of one task per player
    queue = asyncio.Queue()
    for t in tags:
        queue.put_nowait(t)

    async def worker():
        while True:
            try:
                tag = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await one(tag)

    await asyncio.gather(*(worker() for _ in range(POLL_CONCURRENCY)))
    if refresh:
        await asyncio.to_thread(coc_db.prune)
    log.info("polled %d players, %d new ranked battles in %.0fs",
             len(tags), new_total, loop.time() - t0)


async def poll_loop():
    await client.wait_until_ready()
    loop = asyncio.get_running_loop()
    cycle = 0
    while True:
        started = loop.time()
        try:
            await poll_once(refresh=(cycle % NAME_REFRESH_EVERY == 0))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("poll failed")
        cycle += 1
        await asyncio.sleep(max(30, POLL_INTERVAL - (loop.time() - started)))


async def send_image(inter, vals, view=None):
    """Renders the image and sends it. Returns the sent message (needed for the view timeout)."""
    async with sem:
        data = await asyncio.get_running_loop().run_in_executor(None, lambda: render(**vals))
    kwargs = {"view": view} if view else {}
    return await inter.followup.send(file=discord.File(io.BytesIO(data), "out.png"), **kwargs)


# ---- league table: button on /battle_log -> paginated leaderboards for the 100 players ----
TABLE_PAGE_SIZE = 20   # one line per player -> 20 per page, 5 pages for 100 players
MODE_LABELS = {"trophies": "🏆 Trophies", "off": "⚔️ Offense", "def": "🛡️ Defense"}


def player_link(name, tag):
    """Player name that opens their in-game profile when clicked."""
    label = safe_name(name).replace("[", "(").replace("]", ")")
    if not tag:
        return label
    return f"[{label}](https://link.clashofclans.com/en?action=OpenPlayerProfile&tag=%23{tag})"


def avg_stars(r, avg_key, count_key):
    return f"{r[avg_key]:.2f}" if r.get(count_key) else "-"


def per_battle(r, total_key, count_key):
    """Trophies per battle, or a dash when there are no battles / no data yet."""
    n = r.get(count_key)
    return f"{r[total_key] / n:+.1f}" if n else "-"


# ---- monospace table (inside a code block, so the columns line up) ----
NAME_W = 14   # width of the Player column


def disp_width(text):
    """Columns a string takes in a monospace font: CJK / fullwidth / emoji count 2, accents 0."""
    return sum(0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in text)


def fit_cell(text, width, right=False):
    """Clean, shorten (with an ellipsis) and pad a name to exactly `width` columns."""
    text = "".join(ch for ch in (text or "") if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co"))
    text = text.replace("`", "'").strip() or "Unknown"
    if disp_width(text) > width:
        while text and disp_width(text) > width - 1:
            text = text[:-1]
        text += "…"
    pad = " " * (width - disp_width(text))
    return pad + text if right else text + pad


ESC = "\u001b"
ANSI_RESET = f"{ESC}[0m"
ANSI_HEAD = f"{ESC}[1;37m"                                  # bold white
ANSI_ME = f"{ESC}[1;33m"                                    # bold yellow = gold
ANSI_ZONE = {"promotion": f"{ESC}[0;32m", "demotion": f"{ESC}[0;31m"}   # green / red


def cnt(n):
    """Battle count shown after a total, e.g. 30 -> '(30)'."""
    return f"({n})"


def render_table(mode, rows, start, size, my_tag, zone_of):
    """One page as an aligned text table for a ```ansi code block.
    Your own row is gold and marked with '>'; in the trophy view promotion rows are green
    and demotion rows red. In the offense/defense views the number in brackets after a total
    is how many battles it covers (attacks done, or defenses taken)."""
    if mode == "trophies":
        cols = [("Trophies", 8)]
    else:
        cols = [("Total", 9), ("Avg", 5)]
    out = [ANSI_HEAD + f"{'#':>3} {fit_cell('Player', NAME_W)}"
           + "".join(f" {h:>{w}}" for h, w in cols) + ANSI_RESET]
    for pos, r in enumerate(rows[start:start + size], start + 1):
        if mode == "trophies":
            vals = [str(r["trophies"]) + (cnt(r["n_att"]) if "n_att" in r else "")]
        elif mode == "off":
            vals = [f"{r.get('off_total', 0):+d}" + (cnt(r["n_att"]) if "n_att" in r else ""),
                    avg_stars(r, "off_avg", "n_att")]
        else:
            vals = [f"{r.get('def_total', 0):+d}" + (cnt(r["n_def"]) if "n_def" in r else ""),
                    avg_stars(r, "def_avg", "n_def")]
        mine = r["tag"] == my_tag
        line = (f"{pos:>3}{'>' if mine else ' '}{fit_cell(r['name'], NAME_W)}"
                + "".join(f" {v:>{w}}" for v, (_, w) in zip(vals, cols)))
        color = ANSI_ME if mine else (ANSI_ZONE.get(zone_of(pos)) if mode == "trophies" else None)
        out.append(f"{color}{line}{ANSI_RESET}" if color else line)
    return "\n".join(out)


def build_group_stats(league, group_data):
    """tag -> trophy totals / averages, computed with the same formulas as the image."""
    limit = league_attacks(league)
    out = {}
    for g in group_data:
        att, dfn = g["attacks"][:limit], g["defenses"][:limit]
        v = derive_stats(att, dfn)
        v.update(n_att=len(att), n_def=len(dfn), ok=g["ok"])
        out[g["tag"]] = v
    return out


def find_profile_rows(rows, query, limit=5):
    """'12' / '#12' -> the player at that position in the current list.
    Anything else -> players whose name contains the text. Returns [(position, row)]."""
    q = (query or "").strip().lstrip("#").strip()
    if not q:
        return []
    if q.isdigit():
        n = int(q)
        return [(n, rows[n - 1])] if 1 <= n <= len(rows) else []
    qc = q.casefold()
    return [(i, r) for i, r in enumerate(rows, 1) if qc in (r["name"] or "").casefold()][:limit]


class ProfileModal(discord.ui.Modal, title="Open a player's profile"):
    query = discord.ui.TextInput(label="Rank number or player name", placeholder="e.g. 57 or Dude",
                                 max_length=32)

    def __init__(self, table):
        super().__init__()
        self.table = table

    async def on_submit(self, inter: discord.Interaction):
        hits = find_profile_rows(self.table.rows, self.query.value)
        if not hits:
            await inter.response.send_message("No player found. Type a rank (1-100) or part of a name.",
                                              ephemeral=True)
            return
        lines = [f"`#{pos:<3}` {player_link(r['name'], r['tag'])}" for pos, r in hits]
        await inter.response.send_message("\n".join(lines), ephemeral=True)


class TableView(discord.ui.View):
    """Paginated league leaderboards (trophies / offense / defense).
    Sent as an ephemeral message, so only the clicker sees it."""

    def __init__(self, league, members, my_tag, window, group_key, owner_id=None):
        super().__init__(timeout=600)
        self.owner_id = owner_id   # only this Discord user can press the buttons
        self.league, self.members, self.my_tag = league, members, my_tag
        self.window, self.group_key = window, group_key
        self.mode = "trophies"
        self.stats = None      # tag -> stats, loaded the first time Offense/Defense is opened
        self.failed = 0        # players whose battle log couldn't be loaded
        self.note = ""         # how complete the offense/defense data is
        self.inter = None
        self.page = 0
        self._rebuild(jump_to_me=True)

    # ---- data ----
    def _rows(self):
        rows = []
        for i, m in enumerate(self.members, 1):
            tag = coc_api.normalize_tag(m.get("playerTag"))
            rows.append({"tag": tag, "name": m.get("playerName"), "clan": m.get("clanName"),
                         "trophies": m.get("leagueTrophies", 0),
                         **(self.stats.get(tag, {}) if self.stats else {})})
        if self.mode == "off":      # more offense trophies is better
            rows.sort(key=lambda r: (-r.get("off_total", 0), -r.get("off_dest", 0)))
        elif self.mode == "def":    # more defense trophies kept is better, then less destruction
            rows.sort(key=lambda r: (-r.get("def_total", 0), r.get("def_dest", 0)))
        return rows

    def _rebuild(self, jump_to_me=False):
        self.rows = self._rows()
        self.pages = max(1, math.ceil(len(self.rows) / TABLE_PAGE_SIZE))
        mine = next((i for i, r in enumerate(self.rows) if r["tag"] == self.my_tag), None)
        self.my_index = mine
        if jump_to_me and mine is not None:
            self.page = mine // TABLE_PAGE_SIZE
        self.page = max(0, min(self.page, self.pages - 1))
        self.me_btn.disabled = mine is None
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.pages - 1
        self.page_btn.label = f"{self.page + 1}/{self.pages}"
        for mode, btn in (("trophies", self.trophies_btn), ("off", self.off_btn), ("def", self.def_btn)):
            btn.style = discord.ButtonStyle.success if mode == self.mode else discord.ButtonStyle.secondary

    def embed(self):
        zone_of = lambda pos: movement_from_rank(self.league, pos)
        table = render_table(self.mode, self.rows, self.page * TABLE_PAGE_SIZE, TABLE_PAGE_SIZE,
                             self.my_tag, zone_of)
        desc = f"```ansi\n{table}\n```"
        if self.note and self.stats is not None:
            desc += "\n" + self.note
        e = discord.Embed(title=f"{self.league} · {MODE_LABELS[self.mode]}",
                          description=desc, colour=discord.Colour.gold())
        legend = {"trophies": "gold = you · green/red = promotion/demotion · (n) = attacks done",
                  "off": "gold = you · (n) = attacks · Avg = average stars per attack",
                  "def": "gold = you · (n) = defenses taken · Avg = average stars per defense"}[self.mode]
        if self.failed:
            legend += f" · {self.failed} players couldn't be loaded"
        e.set_footer(text=f"Page {self.page + 1}/{self.pages} · {legend}")
        return e

    # ---- navigation ----
    async def _go(self, inter, page):
        self.page = page
        self._rebuild()
        await inter.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._go(inter, self.page - 1)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True, row=0)
    async def page_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._go(inter, self.page + 1)

    @discord.ui.button(label="Me", style=discord.ButtonStyle.primary, row=0)
    async def me_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._go(inter, self.my_index // TABLE_PAGE_SIZE)

    # ---- mode switching ----
    async def _load_stats(self):
        """Offense/defense data for all 100 players.
        Saved battles (from the poller) + live battle logs. When the poller already has every
        member of the group, no API request is made at all."""
        tags = [t for t in (coc_api.normalize_tag(m.get("playerTag")) for m in self.members) if t]
        gk = f"{self.group_key[0]}/{self.group_key[1]}" if self.group_key else None
        status = await asyncio.to_thread(coc_db.watch_status, tags)
        here = {t: status[t] for t in tags if t in status and status[t]["group_key"] == gk}
        covered = len(here) == len(tags) and all(v["primed"] for v in here.values())
        stored = await asyncio.to_thread(coc_db.load_battles_many, tags)
        start, end = self.window
        group = await coc_api.fetch_group_battles(self.members, start, end, key=self.group_key,
                                                  stored=stored, live=not covered)
        if not covered and here:   # first look at this group: keep what we just fetched
            fresh = {g["tag"]: g["fresh"] for g in group if g["ok"] and g["tag"] in here}
            await asyncio.to_thread(coc_db.save_battles_many, fresh)
        self.stats = build_group_stats(self.league, group)
        self.failed = sum(1 for s in self.stats.values() if not s["ok"])

        if len(here) < len(tags):
            self.note = "ℹ️ Recent battle logs only (group not tracked continuously)."
        else:
            since = max(v["since"] for v in here.values())
            if since <= start + timedelta(minutes=30):
                self.note = "✅ Complete data for this week."
            else:
                self.note = (f"ℹ️ Tracked since {discord.utils.format_dt(since, 'f')}, earlier battles may be missing.")

    async def _set_mode(self, inter, mode):
        if mode == self.mode:
            await inter.response.defer()
            return
        responded = False
        if mode != "trophies" and self.stats is None:
            loading = discord.Embed(title=f"{self.league} · {MODE_LABELS[mode]}",
                                    description="Loading battle logs for all players…",
                                    colour=discord.Colour.gold())
            await inter.response.edit_message(embed=loading, view=None)   # no buttons while loading
            responded = True
            try:
                await self._load_stats()
            except Exception:
                log.exception("league stats failed")
                await inter.edit_original_response(embed=self.embed(), view=self)
                await inter.followup.send("Couldn't load the battle logs. Try again in a moment.",
                                          ephemeral=True)
                return
        self.mode = mode
        self._rebuild(jump_to_me=True)
        if responded:
            await inter.edit_original_response(embed=self.embed(), view=self)
        else:
            await inter.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Trophies", emoji="🏆", style=discord.ButtonStyle.success, row=1)
    async def trophies_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._set_mode(inter, "trophies")

    @discord.ui.button(label="Offense", emoji="⚔️", style=discord.ButtonStyle.secondary, row=1)
    async def off_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._set_mode(inter, "off")

    @discord.ui.button(label="Defense", emoji="🛡️", style=discord.ButtonStyle.secondary, row=1)
    async def def_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._set_mode(inter, "def")

    @discord.ui.button(label="Profile", emoji="🔗", style=discord.ButtonStyle.secondary, row=0)
    async def profile_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_modal(ProfileModal(self))

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if self.owner_id is not None and inter.user.id != self.owner_id:
            await inter.response.send_message(
                "This table belongs to someone else. Press 📊 League Table under the image to open your own.",
                ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.inter:
            try:
                await self.inter.edit_original_response(view=self)
            except discord.HTTPException:
                pass


class BattleLogView(discord.ui.View):
    """Attached to the /battle_log image. Opens the league leaderboards."""

    def __init__(self, league, members, my_tag, window, group_key):
        super().__init__(timeout=600)
        self.league, self.members, self.my_tag = league, members, my_tag
        self.window, self.group_key = window, group_key
        self.message = None

    @discord.ui.button(label="View League Stats", emoji="📊", style=discord.ButtonStyle.primary)
    async def open_table(self, inter: discord.Interaction, button: discord.ui.Button):
        view = TableView(self.league, self.members, self.my_tag, self.window, self.group_key,
                         owner_id=inter.user.id)
        view.inter = inter
        loading = discord.Embed(title=f"{self.league} · League Table",
                                description="Loading battle logs…", colour=discord.Colour.gold())
        await inter.response.send_message(embed=loading)   # public, but only the opener can click
        try:
            await view._load_stats()   # average stars need every player's battles
        except Exception:
            log.exception("league stats failed")   # the table still opens, stars show as –
        view._rebuild(jump_to_me=True)
        await inter.edit_original_response(embed=view.embed(), view=view)

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)   # drop the button once the data is stale
            except discord.HTTPException:
                pass


async def tracked_autocomplete(inter: discord.Interaction, current: str):
    cur = current.lower().lstrip("#")
    out = []
    for r in await asyncio.to_thread(coc_db.list_tracked):
        if cur in r["tag"].lower() or cur in (r["name"] or "").lower():
            out.append(app_commands.Choice(name=f"{r['name'] or 'Unknown'} (#{r['tag']})"[:100], value=r["tag"]))
            if len(out) >= 25:
                break
    return out


# ---- /coc : manual input ----
@tree.command(name="coc", description="Generate the trophy summary image from typed battles")
@app_commands.describe(
    name="Player name (max 15 characters)",
    rank="Player rank 1-100 in the league (decides promotion/demotion banner)",
    attacks="Attacks as stars:destruction%, e.g. 3:100 2:75 1:60 0:30",
    defenses="Defenses as stars:destruction%, e.g. 3:100 2:80 0:30",
    randomize="Randomly generate attacks and defenses (overrides typed ones)",
    league="League name, e.g. Legend III. Automatically sets the number of attacks/defenses",
)
async def coc(
    inter: discord.Interaction,
    name: NameT,
    rank: str,
    attacks: Optional[BattlesT] = None,
    defenses: Optional[BattlesT] = None,
    randomize: bool = False,
    league: Optional[app_commands.Range[str, 1, LEAGUE_MAX_CHARS]] = None,
):
    vals, err = resolve(name, rank, attacks, defenses, randomize, league)
    if err:
        await inter.response.send_message(err, ephemeral=True)
        return
    await inter.response.defer()
    await send_image(inter, vals)


@coc.autocomplete("league")
async def league_autocomplete(inter: discord.Interaction, current: str):
    cur = _norm(current)
    return [app_commands.Choice(name=f"{n} ({a} attacks)", value=n)
            for n, a, _, _ in LEAGUES if cur in _norm(n)][:25]


PlayerT = app_commands.Range[str, 1, 32]


def find_player(query, rows):
    """Match what the user typed to a registered player: exact cached name first,
    then a tag, then a unique partial name. Returns (tag, error_message)."""
    q = (query or "").strip()
    qc = q.casefold()
    exact = [r for r in rows if (r["name"] or "").casefold() == qc]
    if len(exact) == 1:
        return exact[0]["tag"], None
    if len(exact) > 1:
        return None, f"More than one registered player is called {safe_name(q)}. Use the tag instead."
    tag = coc_api.normalize_tag(q)
    if tag:
        return tag, None
    part = [r for r in rows if qc and qc in (r["name"] or "").casefold()]
    if len(part) == 1:
        return part[0]["tag"], None
    if len(part) > 1:
        names = ", ".join(safe_name(r["name"]) for r in part[:5])
        return None, f"Several players match that: {names}. Be more specific, or use the tag."
    return None, "I don't know a player with that name. Register one with /track_player, or enter a tag."


# ---- /track_player : register your tag ----
@tree.command(name="track_player", description="Register a player tag so the bot tracks their ranked battles")
@app_commands.describe(tag="Player tag WITHOUT the #, e.g. 8CUCYG9")
@app_commands.checks.cooldown(1, 10.0)
async def track_player(inter: discord.Interaction, tag: TagT):
    clean = coc_api.normalize_tag(tag)
    if not clean:
        await inter.response.send_message(
            "That doesn't look like a valid player tag. Enter it without the #, e.g. `8CUCYG9`.",
            ephemeral=True)
        return
    await inter.response.defer()
    row = await asyncio.to_thread(coc_db.get_tracked, clean)
    if row:
        await inter.followup.send(f"#{clean} is already registered as **{safe_name(row['name'])}**.")
        return
    if await asyncio.to_thread(coc_db.count_by_user, inter.user.id) >= coc_db.MAX_PER_USER:
        await inter.followup.send(f"You can register up to {coc_db.MAX_PER_USER} players. "
                                  "Remove one with /untrack_player first.")
        return
    if await asyncio.to_thread(coc_db.count_tracked) >= coc_db.MAX_TRACKED:
        await inter.followup.send(f"The tracking list is full ({coc_db.MAX_TRACKED} players).")
        return
    try:
        player = await coc_api.get_player(clean)
        items = await coc_api.get_battlelog(clean)
    except coc_api.CocError as e:
        await inter.followup.send(str(e))
        return
    name = player.get("name") or "Unknown"
    await asyncio.to_thread(coc_db.add_tracked, clean, name, inter.user.id)
    saved = await asyncio.to_thread(coc_db.save_battles, clean, coc_api.extract_ranked(items))
    await watch_player_group(player, clean)   # also tracks the rest of their league group
    await inter.followup.send(
        f"Registered **{safe_name(name)}** (#{clean}). Saved {saved} ranked battles from the "
        f"current log; new ones are picked up every {POLL_INTERVAL // 60} minutes. "
        f"Use `/battle_log` to see the image.")


# ---- /battle_log : the image ----
@tree.command(name="battle_log", description="Generate the ranked battle log image for a player")
@app_commands.describe(player="Player name (from /track_player) or tag. Leave empty for your own player")
@app_commands.checks.cooldown(1, 10.0)
async def battle_log(inter: discord.Interaction, player: Optional[PlayerT] = None):
    rows = await asyncio.to_thread(coc_db.list_tracked)
    if player:
        clean, err = find_player(player, rows)
        if err:
            await inter.response.send_message(err, ephemeral=True)
            return
    else:
        mine = [r for r in rows if r["added_by"] == inter.user.id]
        if not mine:
            await inter.response.send_message(
                "You haven't registered a player yet. Use /track_player, or enter a player name or tag.",
                ephemeral=True)
            return
        if len(mine) > 1:
            names = ", ".join(safe_name(r["name"]) for r in mine)
            await inter.response.send_message(
                f"You have several registered players ({names}). Pick one with the player option.",
                ephemeral=True)
            return
        clean = mine[0]["tag"]
    await inter.response.defer()
    stored = await asyncio.to_thread(coc_db.load_battles, clean)
    try:
        data = await coc_api.fetch_summary(clean, stored)
    except coc_api.CocError as e:
        await inter.followup.send(str(e))
        return
    cached = next((r for r in rows if r["tag"] == clean), None)
    if cached:   # registered player: keep saved battles and the cached name up to date
        await asyncio.to_thread(coc_db.save_battles, clean, data["live"])
        if data["name"] != cached["name"]:
            await asyncio.to_thread(coc_db.update_name, clean, data["name"])
    limit = league_attacks(data["league"])
    att = " ".join(f"{s}:{p}" for s, p in data["attacks"][:limit])
    dfn = " ".join(f"{s}:{p}" for s, p in data["defenses"][:limit])
    rank = f"#{data['rank']}" if data["rank"] else "-"
    vals, err = resolve(data["name"], rank, att, dfn, False, data["league"])
    if err:
        await inter.followup.send(err)
        return

    if cached and data["members"] and data["group_key"]:   # registered player: track the whole group
        gk = f"{data['group_key'][0]}/{data['group_key'][1]}"
        await asyncio.to_thread(coc_db.watch_group, gk, group_pairs(data["members"]), data["window"][1])

    view = None
    if data["members"]:   # Legend I (and API failures) have no group -> no button
        view = BattleLogView(data["league"], data["members"], clean,
                              data["window"], data["group_key"])
    msg = await send_image(inter, vals, view)
    if view:
        view.message = msg


battle_log.autocomplete("player")(tracked_autocomplete)


# ---- /untrack_player /tracked_players ----
@tree.command(name="untrack_player", description="Remove a registered player and delete their saved battles")
@app_commands.describe(player="Player name or tag")
async def untrack_player(inter: discord.Interaction, player: PlayerT):
    rows = await asyncio.to_thread(coc_db.list_tracked)
    clean, err = find_player(player, rows)
    row = next((r for r in rows if r["tag"] == clean), None) if clean else None
    if err or not row:
        await inter.response.send_message(err or "That player isn't registered.", ephemeral=True)
        return
    perms = getattr(inter, "permissions", None)
    if row["added_by"] != inter.user.id and not (perms and perms.manage_guild):
        await inter.response.send_message(
            "Only the person who registered this player, or someone with Manage Server, can remove them.",
            ephemeral=True)
        return
    await asyncio.to_thread(coc_db.drop_group_if_unused, clean)   # stop polling their league group
    await asyncio.to_thread(coc_db.remove_tracked, clean)
    await inter.response.send_message(f"Removed **{safe_name(row['name'])}** (#{clean}).")


untrack_player.autocomplete("player")(tracked_autocomplete)


def _tracked_text(rows, watched, fmt, bold):
    lines = [f"{bold}Registered players ({len(rows)}){bold}"]
    lines += [f"- {fmt(r['name'])} (#{r['tag']})" for r in rows]
    if watched:
        groups = {}
        for w in watched:
            groups.setdefault(w["group_key"], []).append(w)
        lines += ["", f"{bold}Also tracked: league group members ({len(watched)}){bold}"]
        for i, members in enumerate(groups.values(), 1):
            lines.append(f"League group {i} ({len(members)} players)")
            lines += [f"- {fmt(w['name'])} (#{w['tag']})" for w in members]
    return "\n".join(lines)


@tree.command(name="tracked_players", description="List registered players and everyone else being tracked")
async def tracked_players(inter: discord.Interaction):
    rows = await asyncio.to_thread(coc_db.list_tracked)
    watched = await asyncio.to_thread(coc_db.list_watched)
    if not rows and not watched:
        await inter.response.send_message("Nobody is tracked yet. Use `/track_player`.", ephemeral=True)
        return
    text = _tracked_text(rows, watched, safe_name, "**")
    if len(text) <= 1900:
        await inter.response.send_message(text)
        return
    # too long for one Discord message -> send the full list as a text file
    plain = _tracked_text(rows, watched, lambda n: n or "Unknown", "")
    await inter.response.send_message(
        f"{len(rows)} registered + {len(watched)} league group members are tracked. Full list attached.",
        file=discord.File(io.BytesIO(plain.encode("utf-8")), filename="tracked_players.txt"))


@tree.error
async def on_app_command_error(inter: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        msg = f"Slow down, try again in {error.retry_after:.0f}s."
    else:
        log.exception("command error", exc_info=error)
        msg = "Something went wrong. Try again in a moment."
    if inter.response.is_done():
        await inter.followup.send(msg, ephemeral=True)
    else:
        await inter.response.send_message(msg, ephemeral=True)


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


# ---- terminal test (no token) ----
def ask(label, cast, default=None):
    raw = input(f"{label}" + (f" [{default}]" if default is not None else "") + ": ").strip()
    if raw == "":
        return default
    return cast(raw)


if __name__ == "__main__":
    print("Settings file:", ENV_PATH or f"NOT FOUND (looked for .env in {ROOT})")
    print("DISCORD_TOKEN:", "set" if TOKEN else "MISSING", "| COC_API_TOKEN:",
          f"{len(coc_api.COC_API_TOKENS)} key(s)" if coc_api.COC_API_TOKENS else "MISSING")
    if not TOKEN:
        print("No Discord token set -> local test mode (fill in TOKEN to run the bot).")
        name = ask("Name", str, "TestPlayer")
        rank = ask("Rank", str, "#1")
        league = ask("League", str, DEFAULT_LEAGUE)
        lim = league_attacks(league)
        print(f"{league}: {lim} attacks/defenses per week")
        randomize = input("Randomize attacks/defenses? (y/n): ").strip().lower() == "y"
        att_txt = def_txt = ""
        if not randomize:
            att_txt = ask("Attacks (stars:destruction%, e.g. 3:100 2:75 1:60)", str, "")
            def_txt = ask("Defenses (stars:destruction%, e.g. 3:100 2:80 0:30)", str, "")
        grid = input("Show grid? (y/n): ").strip().lower() == "y"
        vals, err = resolve(name, rank, att_txt, def_txt, randomize, league)
        if err:
            raise SystemExit(err)
        if randomize:
            print("Attacks:", " ".join(f"{s}:{p:g}" for s, p in vals["attacks"]))
            print("Defenses:", " ".join(f"{s}:{p:g}" for s, p in vals["defenses"]))
        (ROOT / "test.png").write_bytes(render(**vals, grid=grid))
        print(f"Banner: {vals['movement']}")
        print(f"Offense {vals['off_total']:+d}, Defense {vals['def_total']:+d}, "
              f"Total = {vals['off_total'] + vals['def_total']}. Saved test.png")
    else:
        client.run(TOKEN)