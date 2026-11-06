"""
LCS FIGHT bot, boxing part. Runs in the same GitHub workflow as sync_ufc.py,
before it, and never touches the UFC data.

Writes two documents the app reads (one read per screen, not one per card):
  meta/boxingCards   upcoming and recent boxing cards
                       schedule: The Odds API (event list only; odds are never
                                 read, stored or shown)
                       names, Arabic names, flags, photos, results: Wikipedia
  meta/boxingChamps  world champions per weight class (WBA, WBC, IBF, WBO,
                     The Ring), from Wikipedia's "List of current world boxing
                     champions"
Bookkeeping (what was looked up / already sent) lives in meta/boxingState.

Alerts:
  boxing_ar / boxing_en topics   card starting, main event result
  fight_<id> + general_<lang>    the fight bell (same as UFC): next fight
                                 coming up, result
Results come from the boxers' Wikipedia "Professional boxing record" tables,
so they can arrive some minutes after the final bell. Nothing is guessed: a
fight without a result on Wikipedia simply stays "awaiting result".
"""
import hashlib
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

import sync_ufc as ufc

ODDS_KEY = os.environ.get("ODDS_API_KEY", "").strip()
ODDS_API = "https://api.the-odds-api.com/v4"
MODE = os.environ.get("SYNC_MODE", "full")
ALERTS = os.environ.get("BOXING_ALERTS", "1") != "0"  # test runs send nothing

ODDS_EVERY = timedelta(hours=6)       # schedule check (free plan: 500 credits/month)
CHAMPS_EVERY = timedelta(hours=24)    # champions list
KEEP_PAST = timedelta(days=130)       # past cards kept for the Results tab
PAST_DAYS = 120                       # champions' recent fights read into Results
PAST_EVERY = timedelta(hours=24)
RESULT_WINDOW = timedelta(hours=60)   # look for results until 60 h after the card starts
SAME_CARD_GAP = timedelta(hours=3)    # bouts closer than this belong to one card
NAME_LOOKUPS_PER_RUN = 80

ORGS = ["WBA", "WBC", "IBF", "WBO", "The Ring"]

report = []  # printed at the end (and saved by the test workflow)


def say(line):
    print(line)
    report.append(line)


def now_utc():
    return datetime.now(timezone.utc)


def bout_id(a, b):
    return "bx_" + ufc.fight_id(a, b)


def boxer_id(name):
    return "bx_" + ufc.fighter_key(name)


def last_name(name):
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    while len(parts) > 1 and parts[-1].lower().strip(".") in ("jr", "sr", "ii", "iii", "iv"):
        parts.pop()
    return parts[-1] if parts else name


def _as_dt(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------- Wikipedia: who is a boxer

def boxer_titles(names, state):
    """Name -> English Wikipedia title of that boxer ('' = no boxer page).
    Cached in state["names"]; at most NAME_LOOKUPS_PER_RUN new lookups per run."""
    cache = state.setdefault("names", {})
    todo = [n for n in sorted(set(names)) if n and n not in cache][:NAME_LOOKUPS_PER_RUN]
    if todo:
        texts = ufc.intros(todo)  # follows redirects; missing pages come back empty
        for n in todo:
            intro = (texts.get(n) or "").lower()
            # Only a page about a boxer counts (not a footballer with the same name).
            cache[n] = n if re.search(r"\bboxer\b|\bboxing\b", intro) else ""
        say(f"boxer pages looked up: {len(todo)}, found {sum(1 for n in todo if cache[n])}")
    return {n: cache.get(n, "") for n in names}


def people_info(titles, state):
    """Wikipedia titles -> {"ar", "nat", "photo"}; cached in state["people"]."""
    cache = state.setdefault("people", {})
    todo = sorted({t for t in titles if t and t not in cache})
    if todo:
        info = ufc.fighter_info(todo)
        photos = ufc.fighter_photos(todo)
        for t in todo:
            i = info.get(t, {})
            cache[t] = {
                "ar": i.get("ar", ""),
                "nat": i.get("natEn", ""),
                "arab": bool(i.get("arab")),
                "photo": photos.get(t),
            }
        say(f"boxer details looked up: {len(todo)}")
    return {t: cache.get(t, {}) for t in titles if t}


# ---------------------------------------------------------------- champions

def _person_link(cell):
    """First link to a person in a table cell (skips flag icons and footnotes).
    A boxer without an English article (red link) gives (name, "")."""
    for a in cell.find_all("a", href=True):
        href = a["href"]
        if a.find("img") or a.find_parent(class_="flagicon") or a.find_parent("sup"):
            continue
        if "redlink" in (a.get("class") or []) or "redlink=1" in href:
            return a.get_text(" ", strip=True), ""
        if not href.startswith("/wiki/"):
            continue
        title = requests.utils.unquote(href[len("/wiki/"):]).replace("_", " ")
        if ":" in title.split("#")[0]:
            continue  # File:, Category:, Help:
        return a.get_text(" ", strip=True), title.split("#")[0]
    return None, None


def _first_line(cell):
    """The champion's own line of a cell. Interim / regular holders and notes
    come after a line break and are not the champion."""
    first = re.split(r"<br\s*/?>", str(cell), maxsplit=1)[0]
    return BeautifulSoup(first, "html.parser")


def _record(text):
    m = re.search(r"\b(\d{1,3})\s*[–\-]\s*(\d{1,3})(?:\s*[–\-]\s*(\d{1,3}))?\b", text or "")
    if not m:
        return ""
    rec = "-".join(g for g in m.groups() if g)
    ko = re.search(r"\((\d+)\s*KOs?\)", text or "", re.I)
    return f"{rec} ({ko.group(1)} KO)" if ko else rec


def _org_of(header):
    h = (header or "").lower()
    if "ring" in h:
        return "The Ring"
    for org in ("WBA", "WBC", "IBF", "WBO"):
        if org.lower() in h:
            return org
    return None


def _clean_division(text):
    text = re.sub(r"\(.*?\)|\(.*$", "", text or "")
    text = text.split("/")[0]  # "Cruiserweight/Junior heavyweight" -> "Cruiserweight"
    text = re.sub(r"\[.*?\]|\bedit\b", "", text)
    return re.sub(r"\s+", " ", text).strip(" :–-")


def parse_champions(html):
    """[{"name": division, "belts": [{"org", "name", "title", "nat", "record"}]}].

    Works whether the page has one table per weight class (division in the
    heading above it) or one big table with the division in the first column.
    """
    soup = BeautifulSoup(html, "html.parser")
    divisions, heading = [], ""
    for el in soup.find_all(["h2", "h3", "h4", "table"]):
        if el.name != "table":
            heading = _clean_division(el.get_text(" ", strip=True))
            continue
        if "wikitable" not in (el.get("class") or []):
            continue
        grid = ufc.expand_table(el)
        head_i, cols = None, {}
        for i, row in enumerate(grid[:3]):
            found = {}
            for j, cell in enumerate(row):
                org = _org_of(ufc.clean(cell))
                if org and org not in found.values():
                    found[j] = org
            if len(found) >= 3:
                head_i, cols = i, found
                break
        if head_i is None:
            continue
        header = grid[head_i]
        div_col = next((j for j, c in enumerate(header)
                        if j not in cols and re.search(r"weight|division|class", ufc.clean(c), re.I)), None)
        for row in grid[head_i + 1:]:
            if len(row) <= max(cols):
                continue
            if div_col is not None and div_col < len(row):
                name = _clean_division(ufc.clean(row[div_col]))
            else:
                name = heading
            if not name or not re.search(r"weight", name, re.I):
                continue
            belts = []
            for j, org in cols.items():
                cell = row[j]
                text = ufc.clean(cell)
                if not text:
                    continue  # this body has no title in this weight class
                line = _first_line(cell)
                line_text = ufc.clean(line)
                if re.search(r"\bvacant\b", line_text, re.I):
                    belts.append({"org": org, "name": "", "title": "", "nat": "", "record": ""})
                    continue
                shown, title = _person_link(line)
                if not shown:
                    # Plain text name (no link): the words before the record.
                    shown = re.split(r"\s*[\d(]", line_text, maxsplit=1)[0].strip(" ,")
                    title = ""
                if not shown or not re.search(r"[^\W\d_]", shown):
                    continue  # "–", "N/A": no title here
                flag = line.find(class_="flagicon") or cell.find(class_="flagicon")
                belts.append({
                    "org": org,
                    "name": shown,
                    "title": title,
                    "nat": ufc.cell_country(flag) if flag else "",
                    "record": _record(text),
                })
            if any(b["name"] for b in belts):
                # One table per division: only its first row is the champions.
                if div_col is None and any(d["name"] == name for d in divisions):
                    continue
                divisions.append({"name": name, "belts": sorted(belts, key=lambda b: ORGS.index(b["org"]))})
    return divisions


def sync_champions(db, state, force=False):
    last = _as_dt(state.get("champsAt"))
    if not force and last and now_utc() - last < CHAMPS_EVERY:
        return state.get("champs") or []
    html = ufc.fetch_page_html("List of current world boxing champions")
    if not html:
        say("champions: page could not load, kept the old list")
        return state.get("champs") or []
    if os.environ.get("BOXING_DUMP"):
        # Test runs: save the first tables as they are, to check the parser.
        soup = BeautifulSoup(html, "html.parser")
        with open(os.environ["BOXING_DUMP"], "w") as fh:
            for i, el in enumerate(soup.find_all(["h2", "h3", "h4", "table"])[:14]):
                fh.write(str(el)[:12000] + "\n\n")
    divisions = parse_champions(html)
    belts = sum(1 for d in divisions for b in d["belts"] if b["name"])
    say(f"champions: {len(divisions)} weight classes, {belts} belts held")
    if len(divisions) < 8 or belts < 25:
        say("champions: too few found (page layout changed?), kept the old list")
        return state.get("champs") or []
    info = people_info([b["title"] for d in divisions for b in d["belts"] if b["title"]], state)
    out = []
    for d in divisions:
        rows = []
        for b in d["belts"]:
            i = info.get(b["title"], {}) if b["title"] else {}
            row = {"org": b["org"], "name": b["name"], "nameAr": i.get("ar", ""),
                   "nat": b["nat"] or i.get("nat", ""), "record": b["record"], "wiki": b["title"],
                   "pid": boxer_id(b["name"]) if b["name"] else ""}
            if i.get("photo"):
                row["photo"] = i["photo"]
            rows.append(row)
        out.append({"name": d["name"], "belts": rows})
    for d in out[:3]:
        say("  " + d["name"] + ": " + ", ".join(f"{b['org']} {b['name'] or 'vacant'}" for b in d["belts"]))
    if db is not None:
        db.collection("meta").document("boxingChamps").set({
            "divisions": out,
            "updated": now_utc(),
            "source": "Wikipedia (CC BY-SA 4.0)",
        }, merge=True)  # legends + profiles live in the same document
    state["champsAt"] = now_utc().isoformat()
    # Who holds what, used to spot title fights on cards.
    state["champTitles"] = {b["title"]: b["name"] for d in divisions for b in d["belts"] if b["title"]}
    state["champs"] = [{"name": d["name"], "holders": {b["name"]: [] for b in d["belts"] if b["name"]}}
                       for d in out]
    for d, src in zip(state["champs"], out):
        for b in src["belts"]:
            if b["name"]:
                d["holders"][b["name"]].append(b["org"])
    return state["champs"]


def champion_belts(name, champs):
    """(division, [orgs]) if this boxer holds a world title now."""
    key = ufc.norm(name)
    for d in champs or []:
        for holder, orgs in (d.get("holders") or {}).items():
            if ufc.norm(holder) == key:
                return d["name"], orgs
    return "", []


# ---------------------------------------------------------------- schedule (The Odds API)

def _odds_get(path, params):
    params = dict(params, apiKey=ODDS_KEY)
    r = requests.get(f"{ODDS_API}{path}", params=params, timeout=30)
    left = r.headers.get("x-requests-remaining")
    if left is not None:
        say(f"Odds API credits left this month: {left}")
    r.raise_for_status()
    return r.json()


def odds_bouts(state, force=False):
    """Upcoming boxing bouts: [{"a", "b", "time"}]. Only the event list is
    used: who fights whom and when. Odds are never read or stored."""
    if not ODDS_KEY:
        say("schedule: no ODDS_API_KEY secret yet, skipped")
        return None
    last = _as_dt(state.get("oddsAt"))
    if not force and last and now_utc() - last < ODDS_EVERY:
        return None
    try:
        sports = _odds_get("/sports", {"all": "true"})
        keys = [s["key"] for s in sports if "boxing" in (s.get("group", "") + s.get("key", "")).lower()]
        if not keys:
            say("schedule: The Odds API lists no boxing right now")
            state["oddsAt"] = now_utc().isoformat()
            return []
        events = []
        for key in keys:
            try:
                events += _odds_get(f"/sports/{key}/events", {"dateFormat": "iso"})
            except requests.HTTPError as e:
                # Older plans: the event list only comes with odds (1 credit).
                say(f"schedule: events list unavailable ({e.response.status_code}), using the odds list")
                events += _odds_get(f"/sports/{key}/odds", {"regions": "uk", "markets": "h2h", "dateFormat": "iso"})
    except Exception as e:
        say(f"schedule: The Odds API failed ({type(e).__name__}), kept the old cards")
        return None
    state["oddsAt"] = now_utc().isoformat()
    bouts = []
    for ev in events:
        a, b = (ev.get("home_team") or "").strip(), (ev.get("away_team") or "").strip()
        when = _as_dt(ev.get("commence_time"))
        if a and b and when:
            bouts.append({"a": a, "b": b, "time": when})
    say(f"schedule: {len(bouts)} upcoming boxing bouts listed")
    return bouts


def build_cards(bouts, state, champs):
    """Groups bouts into cards (bouts within 3 h of each other), keeps only
    bouts with at least one boxer who has a Wikipedia page, and fills names."""
    titles = boxer_titles([x for b in bouts for x in (b["a"], b["b"])], state)
    bouts = [b for b in bouts if titles.get(b["a"]) or titles.get(b["b"])]
    bouts.sort(key=lambda b: b["time"])
    groups = []
    for b in bouts:
        if groups and b["time"] - groups[-1][-1]["time"] <= SAME_CARD_GAP:
            groups[-1].append(b)
        else:
            groups.append([b])
    info = people_info([t for t in titles.values() if t], state)

    cards = []
    for g in groups:
        def weight(b):
            score = 0
            for side in ("a", "b"):
                if champion_belts(b[side], champs)[1]:
                    score += 3
                if titles.get(b[side]):
                    score += 1
            return (score, b["time"])
        main = max(g, key=weight)
        # Main event first, then the rest from the latest bout down (a card
        # runs bottom to top, like the UFC cards in the app).
        ordered = [main] + sorted([b for b in g if b is not main], key=lambda b: b["time"], reverse=True)
        fights = []
        for b in ordered:
            is_main = b is main
            f = {"id": bout_id(b["a"], b["b"]), "fighterA": b["a"], "fighterB": b["b"],
                 "isMain": is_main, "winner": "", "method": "", "card": "", "division": ""}
            for side, key in (("A", "a"), ("B", "b")):
                i = info.get(titles.get(b[key], ""), {})
                f[f"fighter{side}Ar"] = i.get("ar", "")
                f[f"fighter{side}Nat"] = i.get("nat", "")
                f[f"fighter{side}Arab"] = bool(i.get("arab"))
                f[f"fighter{side}Id"] = boxer_id(b[key])
                if i.get("photo"):
                    f[f"photo{side}"] = i["photo"]
            # A reigning champion headlining = title fight (championship rounds).
            if is_main:
                for side in ("a", "b"):
                    division, orgs = champion_belts(b[side], champs)
                    if orgs:
                        f["titles"] = [o for o in ORGS if o in orgs]
                        f["division"] = division
                        f["rounds"] = 12
                        break
            fights.append(f)
        m = fights[0]
        name = f"Boxing: {last_name(m['fighterA'])} vs. {last_name(m['fighterB'])}"
        name_ar = ""
        if m.get("fighterAAr") and m.get("fighterBAr"):
            name_ar = f"ملاكمة: {last_name(m['fighterAAr'])} ضد {last_name(m['fighterBAr'])}"
        # A one- or two-bout listing with little-known boxers looks empty in
        # the app: keep a card only with 3+ bouts, a title fight, or a main
        # event between two boxers who both have a Wikipedia page.
        main_bout = ordered[0]
        known = bool(titles.get(main_bout["a"]) and titles.get(main_bout["b"]))
        if len(fights) < 3 and not m.get("titles") and not known:
            say(f"  skipped small card: {m['fighterA']} vs {m['fighterB']} ({len(fights)} bout(s))")
            continue
        start = g[0]["time"]
        cid = "bx_" + start.strftime("%Y%m%d") + "_" + hashlib.sha1(m["id"].encode()).hexdigest()[:6]
        cards.append({"id": cid, "name": name, "nameAr": name_ar, "start": start,
                      "location": "", "fights": fights})
    return cards


def merge_cards(old_cards, new_cards):
    """Upcoming cards come from the new schedule; results, alerts and past
    cards (the schedule drops bouts once they start) are kept."""
    now = now_utc()
    old_fights = {f["id"]: f for c in old_cards for f in c.get("fights") or []}
    keep_ids = set()
    merged = []
    for c in new_cards:
        for f in c["fights"]:
            old = old_fights.get(f["id"])
            if old:
                for k in ("winner", "method"):
                    if old.get(k):
                        f[k] = old[k]
        merged.append(c)
        keep_ids |= {f["id"] for f in c["fights"]}
    for c in old_cards:
        start = _as_dt(c.get("start"))
        if not start or now - start > KEEP_PAST:
            continue
        if start <= now + timedelta(minutes=30):
            # Started (or about to): the schedule no longer lists it, keep ours.
            if not any(f["id"] in keep_ids for f in c.get("fights") or []):
                merged.append(c)
    merged.sort(key=lambda c: _as_dt(c["start"]))
    return merged


# ---------------------------------------------------------------- results (Wikipedia)

_TYPES = {
    "KO": "KO", "TKO": "TKO", "RTD": "Retirement", "UD": "Decision (unanimous)",
    "SD": "Decision (split)", "MD": "Decision (majority)", "TD": "Technical decision",
    "DQ": "Disqualification", "PTS": "Decision", "NC": "No contest",
}


def _parse_day(text):
    text = re.sub(r"\[.*?\]", "", text or "").strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def record_rows(html):
    """Rows of a boxer's "Professional boxing record" table:
    [{"result", "opp", "oppTitle", "type", "round", "date", "location", "notes"}]."""
    soup = BeautifulSoup(html or "", "html.parser")
    out = []
    for table in soup.find_all("table", class_="wikitable"):
        grid = ufc.expand_table(table)
        if not grid:
            continue
        head = [ufc.clean(c).lower() for c in grid[0]]

        def col(word):
            return next((i for i, h in enumerate(head) if word in h), None)
        c = {k: col(k) for k in ("result", "opponent", "type", "round", "date", "location", "notes")}
        if None in (c["result"], c["opponent"], c["date"]):
            continue

        def cell(row, key):
            i = c[key]
            return row[i] if i is not None and i < len(row) else None
        for row in grid[1:]:
            result = ufc.clean(cell(row, "result")).lower()
            if not result.startswith(("win", "loss", "draw", "nc", "no contest")):
                continue
            opp_cell = cell(row, "opponent")
            shown, title = _person_link(opp_cell) if opp_cell is not None else (None, None)
            out.append({
                "result": result,
                "opp": shown or ufc.clean(opp_cell),
                "oppTitle": title or "",
                "type": ufc.clean(cell(row, "type")).upper(),
                "round": ufc.clean(cell(row, "round")),
                "date": _parse_day(ufc.clean(cell(row, "date"))),
                "location": ufc.clean(cell(row, "location")),
                "notes": ufc.clean(cell(row, "notes")),
            })
    return out


def record_result(html, opponent, day):
    """(result, type, round) of the bout against this opponent around this day."""
    opp_key = ufc.norm(last_name(opponent))
    for r in record_rows(html):
        if opp_key in ufc.norm(r["opp"]) and r["date"] and abs((r["date"].date() - day.date()).days) <= 2:
            return r["result"], r["type"], r["round"]
    return None


def _method_text(kind, rnd):
    kind = re.sub(r"[^A-Z]", "", kind or "")
    parts = [_TYPES.get(kind, kind)] if kind else []
    m = re.match(r"\s*(\d+)\s*(?:\(\d+\))?\s*,?\s*(\d+:\d{2})?", rnd or "")
    if m and m.group(1):
        parts.append(f"Round {m.group(1)}")
        if m.group(2):
            parts.append(m.group(2))
    return " · ".join(p for p in parts if p)


def fill_results(cards, state):
    """Fills results of bouts that started up to 60 h ago. Returns the fights
    that got a result this run: [(card, fight)]."""
    now, done = now_utc(), []
    pages = {}
    names = state.get("names", {})
    for c in cards:
        start = _as_dt(c["start"])
        if not start or not (start + timedelta(minutes=20) <= now <= start + RESULT_WINDOW):
            continue
        for f in c["fights"]:
            if f.get("winner"):
                continue
            for me, other in (("fighterA", "fighterB"), ("fighterB", "fighterA")):
                title = names.get(f[me])
                if not title:
                    continue
                if title not in pages:
                    pages[title] = ufc.fetch_page_html(title) or ""
                found = record_result(pages[title], f[other], start)
                if not found:
                    continue
                result, kind, rnd = found
                if result.startswith("win"):
                    f["winner"] = f[me]
                elif result.startswith("loss"):
                    f["winner"] = f[other]
                elif result.startswith("draw"):
                    f["winner"] = "Draw"
                else:
                    f["winner"] = "No contest"
                f["method"] = _method_text(kind, rnd)
                say(f"result: {f['fighterA']} vs {f['fighterB']} -> {f['winner']} ({f['method']})")
                done.append((c, f))
                break
    return done


# ---------------------------------------------------------------- past results (Wikipedia)

def _titles_in(notes):
    """Belts named in a record row's notes ("Retained WBA, WBC and IBF heavyweight titles")."""
    if not re.search(r"title|belt|championship", notes or "", re.I):
        return [], ""
    orgs = [o for o in ORGS if re.search(r"\b" + re.escape(o) + r"\b", notes)]
    m = re.findall(r"((?:super |light |junior |mini |minimum|bridger|cruiser|heavy|middle|welter|feather|bantam|fly|straw)?[a-z]*weight)",
                   notes.lower())
    division = m[-1].strip().capitalize() if m else ""
    return orgs, division


def _scheduled(rnd):
    m = re.search(r"\((\d+)\)", rnd or "")
    if m:
        return int(m.group(1))
    m = re.match(r"\s*(\d+)\s*$", rnd or "")
    return int(m.group(1)) if m else 0


def past_cards(state, force=False):
    """The current world champions' fights of the last PAST_DAYS days, as
    finished cards (one per date + place), for the Results tab."""
    last = _as_dt(state.get("pastAt"))
    if not force and last and now_utc() - last < PAST_EVERY:
        return None
    champs = state.get("champTitles") or {}
    if not champs:
        return None
    since = now_utc() - timedelta(days=PAST_DAYS)
    today = now_utc().date()
    bouts = {}
    for title, name in sorted(champs.items()):
        html = ufc.fetch_page_html(title)
        rows = record_rows(html)
        _champ_rows[title] = (name, rows)
        for r in rows[:6]:  # newest first on Wikipedia
            if not r["date"] or r["date"] < since or r["date"].date() >= today:
                continue
            fid = bout_id(name, r["opp"])
            if fid in bouts:
                continue
            if r["result"].startswith("win"):
                winner = name
            elif r["result"].startswith("loss"):
                winner = r["opp"]
            elif r["result"].startswith("draw"):
                winner = "Draw"
            else:
                winner = "No contest"
            orgs, division = _titles_in(r["notes"])
            bouts[fid] = {"a": name, "aTitle": title, "b": r["opp"], "bTitle": r["oppTitle"],
                          "date": r["date"], "location": r["location"], "winner": winner,
                          "method": _method_text(r["type"], r["round"]), "titles": orgs,
                          "division": division, "rounds": _scheduled(r["round"])}
        time.sleep(0.5)
    state["pastAt"] = now_utc().isoformat()
    info = people_info([t for b in bouts.values() for t in (b["aTitle"], b["bTitle"]) if t], state)
    groups = {}
    for b in bouts.values():
        groups.setdefault((b["date"].date(), ufc.norm(b["location"])), []).append(b)
    cards = []
    for (day, _), group in groups.items():
        group.sort(key=lambda b: (len(b["titles"]), b["rounds"]), reverse=True)
        fights = []
        for i, b in enumerate(group):
            f = {"id": bout_id(b["a"], b["b"]), "fighterA": b["a"], "fighterB": b["b"], "isMain": i == 0,
                 "winner": b["winner"], "method": b["method"], "card": "", "division": b["division"]}
            if b["titles"]:
                f["titles"] = b["titles"]
            if b["rounds"]:
                f["rounds"] = b["rounds"]
            for side, key in (("A", "aTitle"), ("B", "bTitle")):
                i2 = info.get(b[key], {}) if b[key] else {}
                f[f"fighter{side}Ar"] = i2.get("ar", "")
                f[f"fighter{side}Nat"] = i2.get("nat", "")
                f[f"fighter{side}Arab"] = bool(i2.get("arab"))
                f[f"fighter{side}Id"] = boxer_id(f[f"fighter{side}"])
                if i2.get("photo"):
                    f[f"photo{side}"] = i2["photo"]
            fights.append(f)
        m = fights[0]
        name_ar = ""
        if m.get("fighterAAr") and m.get("fighterBAr"):
            name_ar = f"ملاكمة: {last_name(m['fighterAAr'])} ضد {last_name(m['fighterBAr'])}"
        start = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)
        cards.append({"id": "bx_" + day.strftime("%Y%m%d") + "_" + hashlib.sha1(m["id"].encode()).hexdigest()[:6],
                      "name": f"Boxing: {last_name(m['fighterA'])} vs. {last_name(m['fighterB'])}",
                      "nameAr": name_ar, "start": start, "location": group[0]["location"], "fights": fights})
    say(f"past results: {len(bouts)} champion fights in the last {PAST_DAYS} days, {len(cards)} cards")
    return cards


_champ_rows = {}  # Wikipedia title -> (name, record rows), filled by past_cards


# ---------------------------------------------------------------- legends + profiles

# All-time greats (English Wikipedia titles), in the order they are shown.
LEGENDS = [
    "Muhammad Ali", "Mike Tyson", "Floyd Mayweather Jr.", "Manny Pacquiao", "Naseem Hamed",
    "Sugar Ray Robinson", "Joe Louis", "Rocky Marciano", "Sugar Ray Leonard", "Marvin Hagler",
    "Thomas Hearns", "Roberto Durán", "Julio César Chávez", "Oscar De La Hoya", "Lennox Lewis",
    "Evander Holyfield", "Larry Holmes", "George Foreman", "Joe Frazier", "Bernard Hopkins",
    "Roy Jones Jr.", "Pernell Whitaker", "Wladimir Klitschko", "Vitali Klitschko", "Joe Calzaghe",
    "Andre Ward", "Juan Manuel Márquez", "Marco Antonio Barrera", "Érik Morales", "Carlos Monzón",
    "Alexis Argüello", "Salvador Sánchez", "Henry Armstrong", "Jack Dempsey", "Jack Johnson (boxer)",
    "Archie Moore", "Ricardo López (boxer)", "Gennady Golovkin", "Vasiliy Lomachenko", "Tyson Fury",
]
LEGENDS_EVERY = timedelta(days=7)
MAX_FIGHTS = 40  # per legend profile; champions keep 25 (some old-time greats had 200 fights: one document holds 1 MB)


def _short_fight(r):
    res = r["result"]
    code = "W" if res.startswith("win") else "L" if res.startswith("loss") else "D" if res.startswith("draw") else "NC"
    orgs, _ = _titles_in(r["notes"])
    row = {"d": r["date"].date().isoformat() if r["date"] else "", "o": r["opp"][:40], "r": code,
           "m": _method_text(r["type"], r["round"]), "loc": r["location"][:48]}
    if orgs or re.search(r"title|championship", r["notes"] or "", re.I):
        row["n"] = r["notes"][:90]
    return row


def _pick_fights(rows, limit):
    """Newest first: every title fight, loss and draw, plus the latest five,
    filled up with the other fights in order, at most `limit` (the full list
    is on Wikipedia)."""
    important = [r for i, r in enumerate(rows)
                 if i < 5 or not r["result"].startswith("win")
                 or re.search(r"title|championship", r["notes"] or "", re.I)]
    if len(important) >= limit:
        return important[:limit]
    chosen, extra, out = {id(r) for r in important}, limit - len(important), []
    for r in rows:
        if id(r) in chosen:
            out.append(r)
        elif extra > 0:
            out.append(r)
            extra -= 1
    return out


def profile(title, name, rows, info, limit=MAX_FIGHTS):
    wins = sum(1 for r in rows if r["result"].startswith("win"))
    losses = sum(1 for r in rows if r["result"].startswith("loss"))
    draws = sum(1 for r in rows if r["result"].startswith("draw"))
    kos = sum(1 for r in rows if r["result"].startswith("win") and re.sub(r"[^A-Z]", "", r["type"]) in ("KO", "TKO", "RTD"))
    years = sorted(r["date"].year for r in rows if r["date"])
    i = info.get(title, {})
    p = {"id": boxer_id(name), "wiki": title, "name": name, "nameAr": i.get("ar", ""), "nat": i.get("nat", ""),
         "record": f"{wins}-{losses}" + (f"-{draws}" if draws else ""), "ko": kos,
         "years": f"{years[0]}–{years[-1]}" if years else "", "total": len(rows),
         "fights": [_short_fight(r) for r in _pick_fights(rows, limit)]}
    if i.get("photo"):
        p["photo"] = i["photo"]
    return p


def sync_profiles(db, state, force=False):
    """Legends (weekly) and current champions' profiles (with the past
    results run), saved in meta/boxingChamps next to the champions."""
    if db is None:
        return
    ref = db.collection("meta").document("boxingChamps")
    update = {}
    last = _as_dt(state.get("legendsAt"))
    if force or not last or now_utc() - last > LEGENDS_EVERY:
        info = people_info(LEGENDS, state)
        legends = []
        for title in LEGENDS:
            rows = record_rows(ufc.fetch_page_html(title))
            if len(rows) < 5:
                say(f"legend skipped (no record table found): {title}")
                continue
            name = re.sub(r"\s*\(.*?\)", "", title)
            legends.append(profile(title, name, rows, info))
            time.sleep(0.5)
        if len(legends) >= 20:
            update["legends"] = legends
            state["legendsAt"] = now_utc().isoformat()
        say(f"legends: {len(legends)} profiles")
        for lg in legends[:3]:
            say(f"  {lg['name']} ({lg['nameAr']}) {lg['record']}, {lg['ko']} KO, {lg['years']}, {len(lg['fights'])} fights kept")
    if _champ_rows:
        info = people_info(list(_champ_rows), state)
        update["profiles"] = {boxer_id(name): profile(title, name, rows, info, 25)
                              for title, (name, rows) in _champ_rows.items() if rows}
        say(f"champion profiles: {len(update['profiles'])}")
    if update:
        ref.set(update, merge=True)


def add_past(cards, past):
    """Adds past cards whose bouts aren't on our cards yet."""
    known = {f["id"] for c in cards for f in c.get("fights") or []}
    for c in past or []:
        if not any(f["id"] in known for f in c["fights"]):
            cards.append(c)
    cards.sort(key=lambda c: _as_dt(c["start"]))
    return cards


# ---------------------------------------------------------------- official clips (YouTube)

# Official boxing channels. Each is checked through the YouTube API (the
# channel's own name must contain the word, and it must be big), else skipped.
BOXING_YT = [
    ("@DAZNBoxing", "dazn"), ("@toprank", "top rank"), ("@MatchroomBoxing", "matchroom"),
    ("@QueensberryPromotions", "queensberry"), ("@GoldenBoyBoxing", "golden boy"),
    ("@PremierBoxingChampions", "premier boxing"), ("@ZuffaBoxing", "zuffa"),
    ("@RingMagazine", "ring"), ("@RiyadhSeason", "riyadh"),
]
YT_MIN_SUBS = 200_000
# Official channels allowed below YT_MIN_SUBS (owner's choice, Nov 6).
YT_SMALL_OK = {"@ZuffaBoxing", "@RiyadhSeason"}
YT_BOX_V = 2  # raise when the channel list changes (re-checks the channels now)
YT_EVERY = timedelta(hours=3)
CLIP_SKIP = ("press conference", "presser", "weigh", "face off", "faceoff", "face-off", "preview", "interview",
             "media day", "workout", "countdown", "prediction", "breakdown", "reaction", "callout", "calls out",
             "build up", "build-up", "embedded", "podcast", "live stream", "watch party", "trailer", "promo",
             "ringwalk", "ring walk", "walkout", "arrival", "fight week", "behind the scenes", "every ", "top ",
             "best of", "compilation", "full card", "full event", "watch along", "watchalong", "watch-along",
             "live", "stream", "react", "analysis")
# Arabic words (Riyadh Season titles): highlights / knockout / full fight; and press / weigh-in / face-off.
CLIP_GOOD_AR = (("ملخص", 3), ("أبرز", 3), ("ابرز", 3), ("النزال كامل", 4), ("ضربة قاضية", 2), ("الضربة القاضية", 2))
CLIP_SKIP_AR = ("مؤتمر", "الميزان", "وجها لوجه", "وجهاً لوجه", "مباشر", "بث", "مقابلة", "لقاء", "تحليل", "توقعات")
FIGHT_WORDS = (" vs", "knocks out", "knockout", " over ", "defeats", " ko", "tko", "beats", "stops", " ضد ")
CLIP_GOOD = (("full fight", 4), ("highlight", 3), ("knockout", 2), ("knocks out", 2), ("defeats", 1),
             ("stops", 1), ("stoppage", 1), ("tko", 1), ("ko!", 1), (" ko ", 1), ("buzzer beater", 1))


def _yt_channels(state):
    """Verified official channels: [{"id", "uploads", "title"}] (cached a week)."""
    cache = state.get("ytBoxing") or {}
    at = _as_dt(cache.get("at"))
    if at and now_utc() - at < timedelta(days=7) and cache.get("v") == YT_BOX_V:
        return cache.get("channels") or []
    found = []
    for handle, word in BOXING_YT:
        try:
            items = ufc._yt_get("channels", {"part": "snippet,contentDetails,statistics",
                                             "forHandle": handle}).get("items") or []
        except Exception as e:
            say(f"clips: {handle} lookup failed ({type(e).__name__})")
            continue
        if not items:
            say(f"clips: {handle} not found, skipped")
            continue
        c = items[0]
        title = (c.get("snippet") or {}).get("title", "")
        subs = ufc._yt_subs(c)
        if word not in title.lower() or (subs < YT_MIN_SUBS and handle not in YT_SMALL_OK):
            say(f"clips: {handle} = '{title}' ({subs} subscribers) not accepted")
            continue
        found.append({"id": c["id"], "uploads": ufc._yt_uploads(c), "title": title})
        say(f"clips: verified {title} ({subs // 1000}k subscribers)")
    state["ytBoxing"] = {"at": now_utc().isoformat(), "channels": found, "v": YT_BOX_V}
    return found


def _yt_index(db, state, force=False):
    """Recent uploads of the official channels (meta/boxingYt, bot only)."""
    ref = db.collection("meta").document("boxingYt") if db else None
    saved = (ref.get().to_dict() or {}) if ref else {}
    rows = saved.get("rows") or []
    at = _as_dt(saved.get("at"))
    if not force and at and now_utc() - at < YT_EVERY:
        return rows
    seen = {r["v"] for r in rows}
    done = set(saved.get("chs") or [])
    channels = _yt_channels(state)
    for ch in channels:
        pages = 1 if ch["id"] in done else 6  # a new channel: go back a few hundred videos
        token = None
        for _ in range(pages):
            params = {"part": "snippet", "playlistId": ch["uploads"], "maxResults": 50}
            if token:
                params["pageToken"] = token
            try:
                data = ufc._yt_get("playlistItems", params)
            except Exception as e:
                say(f"clips: {ch['title']} uploads failed ({type(e).__name__})")
                break
            for it in data.get("items") or []:
                sn = it.get("snippet") or {}
                vid = (sn.get("resourceId") or {}).get("videoId")
                if vid and vid not in seen:
                    seen.add(vid)
                    rows.append({"v": vid, "t": sn.get("title", ""), "at": sn.get("publishedAt", ""),
                                 "ch": ch["title"]})
            token = data.get("nextPageToken")
            if not token:
                break
    cutoff = now_utc() - timedelta(days=PAST_DAYS + 10)
    rows = [r for r in rows if (_as_dt(r["at"]) or now_utc()) > cutoff][-4000:]
    if ref is not None:
        ref.set({"rows": rows, "at": now_utc(), "chs": [c["id"] for c in channels]})
    say(f"clips: {len(rows)} official boxing videos indexed")
    return rows


def _plays_in_arab_countries(ids):
    ok = {}
    for i in range(0, len(ids), 50):
        try:
            items = ufc._yt_get("videos", {"part": "contentDetails", "id": ",".join(ids[i:i + 50])}).get("items") or []
        except Exception:
            items = []
        for it in items:
            rr = (it.get("contentDetails") or {}).get("regionRestriction") or {}
            allowed, blocked = set(rr.get("allowed") or []), set(rr.get("blocked") or [])
            ok[it["id"]] = (not allowed or ufc.YT_MUST_PLAY_IN <= allowed) and not (blocked & ufc.YT_MUST_PLAY_IN)
    return ok


def add_clips(db, cards, state, force=False):
    """Official YouTube clips for finished boxing fights (both last names in
    the title, posted from the fight day to 5 days after)."""
    if not ufc.YT_KEY:
        say("clips: no YOUTUBE_API_KEY, skipped")
        return
    # Manual/test runs re-check every saved clip against today's rules.
    todo = [(c, f) for c in cards for f in c.get("fights") or []
            if f.get("winner") and (force or not f.get("clips"))]
    if not todo:
        return
    try:
        rows = _yt_index(db, state, force=force)
    except Exception as e:
        say(f"clips: index failed ({type(e).__name__})")
        return
    found = {}
    for c, f in todo:
        day = _as_dt(c["start"])
        a, b = ufc.norm(last_name(f["fighterA"])), ufc.norm(last_name(f["fighterB"]))
        if len(a) < 3 or len(b) < 3:
            continue
        # Arabic titles (Riyadh Season): the Arabic last names count too.
        a_ar = ufc._ar_norm(last_name(f.get("fighterAAr") or "")).replace(" ", "")
        b_ar = ufc._ar_norm(last_name(f.get("fighterBAr") or "")).replace(" ", "")
        best = []
        for r in rows:
            title = re.sub(r"#\S+", " ", r["t"])  # hashtags like #garciabenn name the event, not the fight
            low = title.lower()
            # "X knocks out Y: Garcia vs Benn" names the fight first and the
            # event after the colon: then the names must be in the first part.
            parts = re.split(r"[:|]", title)
            head = parts[0] if len(parts) > 1 and any(w in f" {parts[0].lower()} " for w in FIGHT_WORDS) else title
            letters = ufc.norm(head)
            when = _as_dt(r["at"])
            if not when or not (day - timedelta(hours=12) <= when <= day + timedelta(days=5)):
                continue
            arabic = ufc._ar_norm(head).replace(" ", "")
            has_a = a in letters or (len(a_ar) >= 3 and a_ar in arabic)
            has_b = b in letters or (len(b_ar) >= 3 and b_ar in arabic)
            if not (has_a and has_b) or any(w in low for w in CLIP_SKIP + CLIP_SKIP_AR):
                continue
            score = sum(p for w, p in CLIP_GOOD + CLIP_GOOD_AR if w in f" {low} ")
            if score > 0:  # must say highlights / full fight / KO...
                best.append((score, when, r))
        best.sort(key=lambda x: (-x[0], x[1]))
        if best:
            found[f["id"]] = (f, [x[2] for x in best[:6]])
        elif force and f.get("clips"):
            f.pop("clip", None)
            f.pop("clips", None)
            say(f"clip removed (no longer matches): {f['fighterA']} vs {f['fighterB']}")
    if not found:
        say("clips: no new official clips")
        return
    plays = _plays_in_arab_countries(sorted({r["v"] for _, rs in found.values() for r in rs}))
    n = 0
    for f, rs in found.values():
        clips, used = [], set()
        for r in rs:
            if plays.get(r["v"], True) and r["ch"] not in used:
                used.add(r["ch"])
                clips.append({"url": f"https://www.youtube.com/watch?v={r['v']}", "source": "youtube",
                              "at": r["at"], "channel": r["ch"], "title": r["t"][:120]})
            if len(clips) == 3:
                break
        if clips:
            f["clip"], f["clips"] = clips[0], clips
            n += 1
            say(f"clip: {f['fighterA']} vs {f['fighterB']} -> {clips[0]['channel']}: {rs[0]['t'][:70]}")
    say(f"clips: {n} fight(s) got an official clip")


# ---------------------------------------------------------------- alerts

def _send_topic(lang, title, body):
    from firebase_admin import messaging
    messaging.send(messaging.Message(
        topic=f"boxing_{lang}",
        notification=messaging.Notification(title=title, body=body),
        apns=ufc._ios_sound(),
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(channel_id="general_alerts"),
        ),
    ))


def _nm(f, side, ar):
    return (f.get(f"fighter{side}Ar") if ar else "") or f[f"fighter{side}"]


def big_card(main):
    """Alerts to everyone only for big nights: a title fight, or a main event
    whose two boxers both have Arabic names on Wikipedia (known to our fans).
    Bell fans get every alert of the fights they chose either way."""
    return bool(main.get("titles")) or bool(main.get("fighterAAr") and main.get("fighterBAr"))


def send_alerts(cards, new_results, state):
    if not ALERTS:
        say(f"alerts off (test run): {len(new_results)} result alert(s) not sent")
        return
    sent = set(state.get("sent") or [])
    now = now_utc()
    for c in cards:
        start = _as_dt(c["start"])
        main = next((f for f in c["fights"] if f.get("isMain")), None)
        if not start or not main:
            continue
        key = f"start:{c['id']}"
        if key not in sent and big_card(main) and start - timedelta(minutes=10) <= now <= start + timedelta(hours=3):
            try:
                _send_topic("ar", "بدأت أمسية الملاكمة",
                            f"{_nm(main, 'A', True)} ضد {_nm(main, 'B', True)} · النتائج في LCS FIGHT")
                _send_topic("en", "Boxing card starting",
                            f"{main['fighterA']} vs {main['fighterB']} · results in LCS FIGHT")
                say(f"alert: card starting {c['id']}")
            except Exception as e:
                say(f"alert failed ({type(e).__name__})")
            sent.add(key)
    for c, f in new_results:
        if f"result:{f['id']}" in sent:
            continue
        sent.add(f"result:{f['id']}")
        if f["winner"] in (f["fighterA"], f["fighterB"]):
            side = "A" if f["winner"] == f["fighterA"] else "B"
            loser = "B" if side == "A" else "A"
            ar = f"{_nm(f, side, True)} يفوز على {_nm(f, loser, True)}"
            en = f"{f['winner']} def. {f['fighter' + loser]}"
        else:
            ar = f"{_nm(f, 'A', True)} ضد {_nm(f, 'B', True)}: {'تعادل' if f['winner'] == 'Draw' else 'لا نتيجة'}"
            en = f"{f['fighterA']} vs {f['fighterB']}: {f['winner']}"
        how_en = f["method"]
        how_ar = ufc.method_ar(how_en) if how_en else ""
        try:
            ufc.send_fight_alert(f["id"], "نتيجة نزال الملاكمة", f"{ar}{' · ' + how_ar if how_ar else ''}",
                                 "Boxing result", f"{en}{' · ' + how_en if how_en else ''}")
            if f.get("isMain") and big_card(f):
                _send_topic("ar", "نتيجة النزال الرئيسي", f"{ar}{' · ' + how_ar if how_ar else ''}")
                _send_topic("en", "Main event result", f"{en}{' · ' + how_en if how_en else ''}")
            # The bout above it on the card is next (bell fans only).
            fights = c["fights"]
            i = fights.index(f)
            nxt = fights[i - 1] if i > 0 and not fights[i - 1].get("winner") else None
            if nxt and f"next:{nxt['id']}" not in sent:
                sent.add(f"next:{nxt['id']}")
                ufc.send_fight_alert(
                    nxt["id"],
                    "الملاكمة: النزال التالي",
                    f"{_nm(nxt, 'A', True)} ضد {_nm(nxt, 'B', True)} · ترقّب دخول الملاكمين",
                    "Boxing: next fight",
                    f"{nxt['fighterA']} vs {nxt['fighterB']} · walkouts coming up",
                    bell=True,
                )
        except Exception as e:
            say(f"alert failed ({type(e).__name__})")
    state["sent"] = sorted(sent)[-500:]


# ---------------------------------------------------------------- main

def _for_firestore(cards):
    out = []
    for c in cards:
        c = dict(c)
        c["start"] = _as_dt(c["start"])
        out.append(c)
    return out


def main():
    db = ufc.firestore_client() if "FIREBASE_SERVICE_ACCOUNT" in os.environ else None
    force = MODE in ("boxing", "boxing_test")
    state_ref = db.collection("meta").document("boxingState") if db else None
    cards_ref = db.collection("meta").document("boxingCards") if db else None
    state = (state_ref.get().to_dict() or {}) if state_ref else {}
    old = (cards_ref.get().to_dict() or {}).get("cards", []) if cards_ref else []

    champs = sync_champions(db, state, force=force)
    bouts = odds_bouts(state, force=force)
    cards = merge_cards(old, build_cards(bouts, state, champs)) if bouts is not None else \
        merge_cards(old, [c for c in old if (_as_dt(c.get("start")) or now_utc()) > now_utc()])
    cards = add_past(cards, past_cards(state, force=force))
    sync_profiles(db, state, force=force)
    new_results = fill_results(cards, state)
    add_clips(db, cards, state, force=force)
    send_alerts(cards, new_results, state)

    say(f"cards saved: {len(cards)}")
    for c in cards[:6]:
        m = c["fights"][0] if c["fights"] else {}
        say(f"  {_as_dt(c['start']):%Y-%m-%d %H:%M} UTC  {c['name']}  ({len(c['fights'])} bouts)"
            f"{'  titles ' + '/'.join(m.get('titles', [])) if m.get('titles') else ''}")
    if db is not None:
        cards_ref.set({"cards": _for_firestore(cards), "updated": now_utc()})
        state_ref.set(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Boxing must never stop the UFC sync that runs after it.
        say(f"boxing sync failed: {type(e).__name__}: {e}")
    finally:
        if os.environ.get("BOXING_REPORT"):
            with open(os.environ["BOXING_REPORT"], "w") as fh:
                fh.write("\n".join(report) + "\n")
    sys.exit(0)
