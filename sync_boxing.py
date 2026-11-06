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
KEEP_PAST = timedelta(days=45)        # past cards kept for the Results tab
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
    return "-".join(g for g in m.groups() if g)


def _org_of(header):
    h = (header or "").lower()
    if "ring" in h:
        return "The Ring"
    for org in ("WBA", "WBC", "IBF", "WBO"):
        if org.lower() in h:
            return org
    return None


def _clean_division(text):
    text = (text or "").split("/")[0]  # "Cruiserweight/Junior heavyweight" -> "Cruiserweight"
    text = re.sub(r"\(.*?\)", "", text)
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
                   "nat": b["nat"] or i.get("nat", ""), "record": b["record"]}
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
        })
    state["champsAt"] = now_utc().isoformat()
    # Who holds what, used to spot title fights on cards.
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


def record_result(html, opponent, day):
    """From a boxer's "Professional boxing record" table: (result, type,
    round, time) of the bout against this opponent around this day."""
    soup = BeautifulSoup(html, "html.parser")
    opp_key = ufc.norm(last_name(opponent))
    for table in soup.find_all("table", class_="wikitable"):
        grid = ufc.expand_table(table)
        if not grid:
            continue
        head = [ufc.clean(c).lower() for c in grid[0]]
        def col(word):
            return next((i for i, h in enumerate(head) if word in h), None)
        c_res, c_opp, c_type, c_round, c_date = col("result"), col("opponent"), col("type"), col("round"), col("date")
        if None in (c_res, c_opp, c_date):
            continue
        for row in grid[1:]:
            if len(row) <= max(c_res, c_opp, c_date):
                continue
            opp = ufc.clean(row[c_opp])
            if opp_key not in ufc.norm(opp):
                continue
            when = _parse_day(ufc.clean(row[c_date]))
            if not when or abs((when.date() - day.date()).days) > 2:
                continue
            result = ufc.clean(row[c_res]).lower()
            kind = ufc.clean(row[c_type]).upper() if c_type is not None and c_type < len(row) else ""
            rnd = ufc.clean(row[c_round]) if c_round is not None and c_round < len(row) else ""
            if result.startswith(("win", "loss", "draw", "nc", "no contest")):
                return result, kind, rnd
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
        if key not in sent and start - timedelta(minutes=10) <= now <= start + timedelta(hours=3):
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
            if f.get("isMain"):
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
    new_results = fill_results(cards, state)
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
