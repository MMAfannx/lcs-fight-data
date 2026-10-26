"""
LCS FIGHT bot: copies UFC events, fight cards and results from Wikipedia
into the app's Firebase database. Runs automatically on GitHub Actions.

Fight data comes from Wikipedia (CC BY-SA 4.0). The app shows that credit.
Anything you edit by hand in the admin screen is never overwritten:
  - a start time you changed stays yours ("startLocked")
  - fights you edited stay yours; the bot only fills in missing results ("fightsLocked")
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "LCSFightBot/1.0 (UFC schedule sync for the LCS FIGHT app; lcsfightx@gmail.com)"}
RECENT_DAYS = 21  # keep checking past events this recent for results

# "full": every event (every 2 hours). "fightnight": only this weekend's events
# (every 10 minutes on Saturday/Sunday). "backfill": one-time import of history.
MODE = os.environ.get("SYNC_MODE", "full")
BACKFILL_FROM = int(os.environ.get("BACKFILL_FROM_YEAR", "2024"))

# ---------------------------------------------------------------- Wikipedia

def fetch_page_html(title):
    params = {"action": "parse", "page": title, "prop": "text", "format": "json",
              "formatversion": "2", "redirects": "1"}
    for attempt in range(3):
        try:
            r = requests.get(API, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                return None
            return data["parse"]["text"]
        except Exception as e:  # network hiccup: wait and retry
            print(f"  retry {attempt + 1} for {title}: {e}")
            time.sleep(3)
    return None


def clean(el):
    """Text of a cell without footnote markers like [1] or [a]."""
    if el is None:
        return ""
    for sup in el.find_all("sup"):
        sup.decompose()
    text = el.get_text(" ", strip=True)
    text = re.sub(r"\[[^\]]*\]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def span_value(raw):
    """Read a rowspan/colspan number safely, even from messy HTML like '2data-sort-value=""'."""
    m = re.match(r"\s*(\d+)", str(raw or ""))
    return max(1, min(int(m.group(1)), 100)) if m else 1


def expand_table(table):
    """Rows of a table as lists of cells, with rowspan/colspan filled in."""
    grid, carry = [], {}
    for tr in table.find_all("tr"):
        row, col = [], 0
        cells = tr.find_all(["td", "th"], recursive=False)
        it = iter(cells)
        while True:
            if col in carry:
                cell, left = carry[col]
                row.append(cell)
                if left > 1:
                    carry[col] = (cell, left - 1)
                else:
                    del carry[col]
                col += 1
                continue
            cell = next(it, None)
            if cell is None:
                break
            span = span_value(cell.get("colspan"))
            rspan = span_value(cell.get("rowspan"))
            for _ in range(span):
                row.append(cell)
                if rspan > 1:
                    carry[col] = (cell, rspan - 1)
                col += 1
        while col in carry:  # trailing carried cells
            cell, left = carry[col]
            row.append(cell)
            if left > 1:
                carry[col] = (cell, left - 1)
            else:
                del carry[col]
            col += 1
        grid.append(row)
    return grid


def link_title(cell):
    a = cell.find("a", href=True) if cell else None
    if not a or not a["href"].startswith("/wiki/") or "redlink" in a.get("class", []):
        return None
    return requests.utils.unquote(a["href"][len("/wiki/"):]).replace("_", " ")


# 3-letter country codes sometimes used in flag columns -> country names
# (the app turns names into flags).
ALPHA3 = {
    "USA": "United States", "BRA": "Brazil", "GBR": "United Kingdom", "ENG": "England",
    "SCO": "Scotland", "WAL": "Wales", "IRL": "Ireland", "CAN": "Canada", "MEX": "Mexico",
    "ARG": "Argentina", "CHL": "Chile", "CHI": "Chile", "PER": "Peru", "COL": "Colombia",
    "VEN": "Venezuela", "ECU": "Ecuador", "BOL": "Bolivia", "PRY": "Paraguay", "URY": "Uruguay",
    "CUB": "Cuba", "DOM": "Dominican Republic", "PRI": "Puerto Rico", "JAM": "Jamaica",
    "FRA": "France", "DEU": "Germany", "GER": "Germany", "NLD": "Netherlands", "NED": "Netherlands",
    "BEL": "Belgium", "CHE": "Switzerland", "SUI": "Switzerland", "AUT": "Austria", "ITA": "Italy",
    "ESP": "Spain", "PRT": "Portugal", "POR": "Portugal", "POL": "Poland", "CZE": "Czech Republic",
    "SVK": "Slovakia", "HUN": "Hungary", "ROU": "Romania", "BGR": "Bulgaria", "BUL": "Bulgaria",
    "GRC": "Greece", "GRE": "Greece", "HRV": "Croatia", "CRO": "Croatia", "SRB": "Serbia",
    "BIH": "Bosnia and Herzegovina", "MNE": "Montenegro", "MKD": "North Macedonia",
    "ALB": "Albania", "KOS": "Kosovo", "SVN": "Slovenia", "SLO": "Slovenia", "DNK": "Denmark",
    "DEN": "Denmark", "NOR": "Norway", "SWE": "Sweden", "FIN": "Finland", "ISL": "Iceland",
    "EST": "Estonia", "LVA": "Latvia", "LTU": "Lithuania", "BLR": "Belarus", "UKR": "Ukraine",
    "MDA": "Moldova", "RUS": "Russia", "GEO": "Georgia", "ARM": "Armenia", "AZE": "Azerbaijan",
    "TUR": "Turkey", "KAZ": "Kazakhstan", "KGZ": "Kyrgyzstan", "UZB": "Uzbekistan",
    "TJK": "Tajikistan", "TKM": "Turkmenistan", "MNG": "Mongolia", "CHN": "China",
    "TWN": "Taiwan", "HKG": "Hong Kong", "JPN": "Japan", "KOR": "South Korea",
    "PHL": "Philippines", "PHI": "Philippines", "THA": "Thailand", "VNM": "Vietnam",
    "IDN": "Indonesia", "MYS": "Malaysia", "SGP": "Singapore", "IND": "India", "PAK": "Pakistan",
    "AFG": "Afghanistan", "IRN": "Iran", "ISR": "Israel", "AUS": "Australia",
    "NZL": "New Zealand", "FJI": "Fiji", "SAM": "Samoa", "WSM": "Samoa", "TON": "Tonga",
    "NGA": "Nigeria", "CMR": "Cameroon", "ZAF": "South Africa", "RSA": "South Africa",
    "GHA": "Ghana", "SEN": "Senegal", "CIV": "Ivory Coast", "COD": "DR Congo", "COG": "Congo",
    "AGO": "Angola", "ANG": "Angola", "KEN": "Kenya", "ETH": "Ethiopia", "UGA": "Uganda",
    "ZWE": "Zimbabwe", "ZIM": "Zimbabwe", "MAR": "Morocco", "DZA": "Algeria", "ALG": "Algeria",
    "TUN": "Tunisia", "LBY": "Libya", "EGY": "Egypt", "JOR": "Jordan", "LBN": "Lebanon",
    "LIB": "Lebanon", "SYR": "Syria", "IRQ": "Iraq", "PSE": "Palestine", "PLE": "Palestine",
    "SAU": "Saudi Arabia", "KSA": "Saudi Arabia", "ARE": "United Arab Emirates",
    "UAE": "United Arab Emirates", "KWT": "Kuwait", "KUW": "Kuwait", "QAT": "Qatar",
    "BHR": "Bahrain", "OMN": "Oman", "YEM": "Yemen", "SOM": "Somalia", "SDN": "Sudan",
}


def cell_country(cell):
    """Country shown in a flag column: its text, its sort key, or the flag picture's name.

    Wikipedia's roster tables show only a flag icon in the ISO column, so the
    plain text is empty. The flag file is named like "Flag_of_the_United_States.svg".
    """
    if cell is None:
        return ""
    text = cell.get_text(" ", strip=True)
    text = re.sub(r"\[[^\]]*\]", "", text).strip()
    if text:
        return ALPHA3.get(text.upper(), text)
    for el in [cell] + cell.find_all(attrs={"data-sort-value": True}):
        value = (el.get("data-sort-value") or "").strip()
        if value:
            return ALPHA3.get(value.upper(), value)
    for el in cell.find_all(["a", "span"], title=True):
        title = el.get("title", "").strip()
        if title and not title.lower().startswith(("file:", "edit")):
            return title
    for img in cell.find_all("img"):
        for source in (img.get("alt", ""), img.get("src", ""), img.get("srcset", "")):
            m = re.search(r"Flag[_ ]of[_ ](.+?)\.svg", requests.utils.unquote(source or ""))
            if m:
                name = m.group(1).replace("_", " ")
                name = re.sub(r"\s*\(.*?\)", "", name)          # "(Pantone)" and similar
                return re.sub(r"^the\s+", "", name, flags=re.I).strip()
        alt = (img.get("alt") or "").strip()
        if alt:
            return re.sub(r"^flag of\s+(the\s+)?", "", alt, flags=re.I).strip()
    return ""


def tidy_place(text):
    """'Edmonton, Alberta , Canada' -> 'Edmonton, Alberta, Canada'."""
    text = re.sub(r"\s+,", ",", text or "")
    text = re.sub(r",\s*,", ",", text)
    return re.sub(r"\s{2,}", " ", text).strip(" ,")


def list_events():
    """Upcoming events plus recent past events from 'List of UFC events'."""
    html = fetch_page_html("List of UFC events")
    if not html:
        raise RuntimeError("Could not load List of UFC events")
    soup = BeautifulSoup(html, "html.parser")
    events = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)
    for table in soup.find_all("table"):
        grid = expand_table(table)
        if not grid:
            continue
        header = [clean(c).lower() for c in grid[0]]
        if "event" not in header or "date" not in header:
            continue
        i_event, i_date = header.index("event"), header.index("date")
        i_venue = header.index("venue") if "venue" in header else None
        i_loc = header.index("location") if "location" in header else None
        is_past = "#" in header
        for row in grid[1:]:
            if len(row) <= max(i_event, i_date):
                continue
            row_text = " ".join(clean(c) for c in row).lower()
            if "canceled" in row_text or "cancelled" in row_text:
                continue
            name = clean(row[i_event])
            try:
                date = datetime.strptime(clean(row[i_date]), "%b %d, %Y")
            except ValueError:
                continue
            day = date.replace(tzinfo=timezone.utc)
            if MODE == "backfill":
                if not is_past or date.year < BACKFILL_FROM:
                    continue
            elif MODE == "fightnight":
                if abs((day - datetime.now(timezone.utc)).days) > 1:
                    continue
            elif is_past and day < cutoff:
                continue
            venue = tidy_place(clean(row[i_venue])) if i_venue is not None and i_venue < len(row) else ""
            loc = tidy_place(clean(row[i_loc])) if i_loc is not None and i_loc < len(row) else ""
            events.append({
                "name": name,
                "title": link_title(row[i_event]),
                "date": date,
                "location": ", ".join(p for p in [venue, loc] if p),
                "city": loc,
            })
    return events


def fight_id(a, b):
    key = "|".join(sorted([norm(a), norm(b)]))
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def norm(name):
    name = re.sub(r"\((c|ic)\)", "", name, flags=re.I)
    return re.sub(r"[^a-z0-9]", "", name.lower())


def strip_title(name):
    return re.sub(r"\s*\((c|ic)\)\s*", " ", name, flags=re.I).strip()


def parse_card(html):
    """Fights from an event page's fight card table, main event first."""
    soup = BeautifulSoup(html, "html.parser")
    fights = []
    for table in soup.find_all("table"):
        heads = [clean(th).lower() for th in table.find_all("th")]
        if "weight class" not in heads or "method" not in heads:
            continue
        section = ""
        for tr in table.find_all("tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) < 4:
                # Header rows like "Main card", "Preliminary card", "Early preliminary card"
                label = clean(tr).lower()
                if "early" in label and "prelim" in label:
                    section = "early"
                elif "prelim" in label:
                    section = "prelims"
                elif "main card" in label:
                    section = "main"
                continue
            link_a, link_b = link_title(tds[1]), link_title(tds[3])
            cols = [clean(td) for td in tds] + [""] * 8
            weight, a, sep, b, method, rnd, tme = cols[:7]
            a, b = strip_title(a), strip_title(b)
            if not a or not b:
                continue
            sep_l = sep.lower()
            winner, how = "", ""
            if sep_l.startswith("def"):
                winner = a
            elif "draw" in method.lower():
                winner = "Draw"
            elif "no contest" in method.lower():
                winner = "No contest"
            if winner:
                parts = [method]
                if rnd:
                    parts.append(f"Round {rnd}")
                if tme:
                    parts.append(tme)
                how = " · ".join(p for p in parts if p)
            fights.append({
                "id": fight_id(a, b),
                "fighterA": a,
                "fighterB": b,
                "division": weight,
                "isMain": False,
                "winner": winner,
                "method": how,
                "card": section,
                "_linkA": link_a,
                "_linkB": link_b,
            })
    if fights:
        fights[0]["isMain"] = True
    return fights

# Arab League countries on Wikidata (country of citizenship, P27).
ARAB_COUNTRIES = {
    "Q262", "Q398", "Q970", "Q977", "Q79", "Q796", "Q810", "Q817", "Q822", "Q1016",
    "Q1025", "Q1028", "Q842", "Q219060", "Q846", "Q851", "Q1045", "Q1049", "Q858",
    "Q948", "Q878", "Q805",
}
# Country codes used on Wikipedia's rankings tables.
ARAB_ISO = {"MAR", "ALG", "DZA", "TUN", "LBY", "EGY", "SDN", "MRT", "SOM", "DJI", "COM",
            "SAU", "KSA", "UAE", "ARE", "QAT", "BHR", "KUW", "KWT", "OMA", "OMN", "YEM",
            "IRQ", "JOR", "LBN", "LIB", "SYR", "PLE", "PSE"}


# Corrections for the automatic "Arab fighter" detection. Use the fighter's
# English name as written on Wikipedia. Add names any time.
NOT_ARAB = {
    "Khabib Nurmagomedov", "Islam Makhachev", "Khamzat Chimaev", "Umar Nurmagomedov",
    "Usman Nurmagomedov", "Said Nurmagomedov", "Magomed Ankalaev", "Muhammad Mokaev",
    "Zabit Magomedsharipov", "Shara Magomedov", "Movsar Evloev",
}
ARAB_EXTRA = {
    # Fighters Wikipedia doesn't describe as Arab but who are, e.g. "Some Name",
}


def is_arab(name, auto):
    n = norm(name)
    if any(norm(x) == n for x in NOT_ARAB):
        return False
    if any(norm(x) == n for x in ARAB_EXTRA):
        return True
    return bool(auto)


def fighter_key(name):
    """Permanent ID for a fighter, used for follows."""
    return hashlib.sha1(norm(strip_title(name)).encode()).hexdigest()[:12]



# Arab heritage, only when Wikipedia's own opening paragraph says so, e.g.
# "of Palestinian descent", "a Moroccan-Dutch fighter", "born to Lebanese parents".
_ARAB_ADJ = ("palestinian|moroccan|algerian|tunisian|egyptian|lebanese|syrian|iraqi|jordanian|"
             "libyan|sudanese|yemeni|mauritanian|saudi|kuwaiti|omani|arab")
# Gulf nationalities are often given to naturalised athletes, so they only count
# together with "descent/origin/heritage" wording, never on their own.
_ALL_ADJ = _ARAB_ADJ + "|emirati|qatari|bahraini"
_HERITAGE = [
    re.compile(rf"\b(?:of|with)\s+(?:partial\s+)?(?:{_ALL_ADJ})\b[\w\s,-]{{0,40}}?\b(?:descent|origin|heritage|ancestry|roots|background)\b"),
    re.compile(rf"\bborn to\s+(?:a\s+)?(?:{_ALL_ADJ})\b"),
    re.compile(rf"\b(?:{_ARAB_ADJ})-(?:american|british|canadian|dutch|french|german|swedish|belgian|"
               rf"australian|danish|norwegian|italian|spanish|brazilian|irish|swiss|austrian)\b"),
    re.compile(rf"\bis an?\s+(?:{_ARAB_ADJ})\b(?!-born)"),
]


_ADJ_COUNTRY = {
    "palestinian": "Palestine", "moroccan": "Morocco", "algerian": "Algeria",
    "tunisian": "Tunisia", "egyptian": "Egypt", "lebanese": "Lebanon", "syrian": "Syria",
    "iraqi": "Iraq", "jordanian": "Jordan", "libyan": "Libya", "sudanese": "Sudan",
    "yemeni": "Yemen", "mauritanian": "Mauritania", "saudi": "Saudi Arabia",
    "kuwaiti": "Kuwait", "omani": "Oman", "emirati": "United Arab Emirates",
    "qatari": "Qatar", "bahraini": "Bahrain",
}


def heritage_country(intro):
    """'... of Iraqi descent' -> 'Iraq' (only from the same wording arab_heritage uses)."""
    text = (intro or "").lower()
    for p in _HERITAGE:
        m = p.search(text)
        if m:
            for adj, country in _ADJ_COUNTRY.items():
                if re.search(rf"\b{adj}\b", m.group(0)):
                    return country
    return ""


def arab_heritage(intro):
    text = (intro or "").lower()
    return any(p.search(text) for p in _HERITAGE)


def intros(titles):
    """Opening paragraph (plain text) of each English Wikipedia page."""
    out = {}
    titles = [t for t in titles if t]
    for i in range(0, len(titles), 20):
        chunk = titles[i:i + 20]
        params = {"action": "query", "prop": "extracts", "exintro": "1", "explaintext": "1",
                  "exlimit": "20", "titles": "|".join(chunk), "redirects": "1",
                  "format": "json", "formatversion": "2"}
        try:
            data = requests.get(API, params=params, headers=HEADERS, timeout=30).json().get("query", {})
        except Exception as e:
            print(f"  intro lookup skipped: {e}")
            continue
        back = {n["to"]: n["from"] for n in data.get("normalized", [])}
        for r in data.get("redirects", []):
            back[r["to"]] = back.get(r["from"], r["from"])
        for page in data.get("pages", []):
            out[back.get(page["title"], page["title"])] = page.get("extract", "")
        time.sleep(0.5)
    return out


def fighter_info(titles):
    """English Wikipedia titles -> {"ar": Arabic name, "arab": bool}."""
    out, qids, titles = {}, {}, sorted({t for t in titles if t})
    for i in range(0, len(titles), 50):
        chunk = titles[i:i + 50]
        params = {"action": "query", "prop": "langlinks|pageprops", "lllang": "ar", "lllimit": "max",
                  "ppprop": "wikibase_item", "titles": "|".join(chunk), "redirects": "1",
                  "format": "json", "formatversion": "2"}
        try:
            data = requests.get(API, params=params, headers=HEADERS, timeout=30).json().get("query", {})
        except Exception as e:
            print(f"  fighter lookup skipped: {e}")
            continue
        back = {}
        for n in data.get("normalized", []):
            back[n["to"]] = n["from"]
        for r in data.get("redirects", []):
            back[r["to"]] = back.get(r["from"], r["from"])
        for page in data.get("pages", []):
            original = back.get(page["title"], page["title"])
            info = out.setdefault(original, {"ar": "", "arab": False})
            links = page.get("langlinks") or []
            if links:
                info["ar"] = re.sub(r"\s*\(.*?\)", "", links[0]["title"]).strip()
            qid = (page.get("pageprops") or {}).get("wikibase_item")
            if qid:
                qids[qid] = original
        time.sleep(1)

    ids = sorted(qids)
    citizenship, birthplace = {}, {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        params = {"action": "wbgetentities", "ids": "|".join(chunk), "props": "claims", "format": "json"}
        try:
            data = requests.get("https://www.wikidata.org/w/api.php", params=params,
                                headers=HEADERS, timeout=30).json().get("entities", {})
        except Exception as e:
            print(f"  nationality lookup skipped: {e}")
            continue
        for qid, entity in data.items():
            claims = entity.get("claims") or {}
            def values(prop):
                return {((c.get("mainsnak") or {}).get("datavalue") or {}).get("value", {}).get("id")
                        for c in claims.get(prop, [])} - {None}
            citizenship[qid] = values("P27")
            birthplace[qid] = values("P19")
        time.sleep(1)

    # Country of each place of birth (a city's "country" property, P17).
    places = sorted({p for ps in birthplace.values() for p in ps})
    place_country = {}
    for i in range(0, len(places), 50):
        chunk = places[i:i + 50]
        params = {"action": "wbgetentities", "ids": "|".join(chunk), "props": "claims", "format": "json"}
        try:
            data = requests.get("https://www.wikidata.org/w/api.php", params=params,
                                headers=HEADERS, timeout=30).json().get("entities", {})
        except Exception as e:
            print(f"  birthplace lookup skipped: {e}")
            continue
        for pid, entity in data.items():
            place_country[pid] = {((c.get("mainsnak") or {}).get("datavalue") or {}).get("value", {}).get("id")
                                  for c in (entity.get("claims") or {}).get("P17", [])} - {None}
        time.sleep(1)

    # Nationality = countries of citizenship, with Arabic and English names.
    countries = sorted({c for cs in citizenship.values() for c in cs}
                       | {c for cs in place_country.values() for c in cs})
    labels = {}
    for i in range(0, len(countries), 50):
        chunk = countries[i:i + 50]
        params = {"action": "wbgetentities", "ids": "|".join(chunk), "props": "labels",
                  "languages": "ar|en", "format": "json"}
        try:
            data = requests.get("https://www.wikidata.org/w/api.php", params=params,
                                headers=HEADERS, timeout=30).json().get("entities", {})
        except Exception as e:
            print(f"  country names skipped: {e}")
            continue
        for cid, entity in data.items():
            lab = entity.get("labels") or {}
            labels[cid] = ((lab.get("ar") or {}).get("value", ""), (lab.get("en") or {}).get("value", ""))
        time.sleep(1)
    for qid, title in qids.items():
        names = [labels[c] for c in sorted(citizenship.get(qid, set())) if c in labels]
        if not names:
            # No citizenship on Wikidata: use the country they were born in.
            born = set().union(*[place_country.get(p, set()) for p in birthplace.get(qid, set())]) \
                if birthplace.get(qid) else set()
            names = [labels[c] for c in sorted(born) if c in labels]
        if names:
            out[title]["natAr"] = "، ".join(a or e for a, e in names)
            out[title]["natEn"] = ", ".join(e for a, e in names if e)

    heritage = intros(list(out))
    for title, info in out.items():
        if not info.get("arab") and arab_heritage(heritage.get(title, "")):
            info["arab"] = True
    for qid, title in qids.items():
        born_in = set().union(*[place_country.get(p, set()) for p in birthplace.get(qid, set())]) \
            if birthplace.get(qid) else set()
        arab_citizen = bool(citizenship.get(qid, set()) & ARAB_COUNTRIES)
        # Arab = born in an Arab country. If the birthplace is unknown, only a fighter
        # whose every citizenship is Arab counts (never someone who also holds another).
        if born_in:
            auto = bool(born_in & ARAB_COUNTRIES)
        else:
            cit = citizenship.get(qid, set())
            auto = arab_citizen and cit <= ARAB_COUNTRIES
        out[title]["arab"] = auto or arab_heritage(heritage.get(title, ""))
        # Arab country of origin (birth country, or heritage from the intro),
        # used to show the right flag in the app's Arab fighters section.
        arab_born = sorted(born_in & ARAB_COUNTRIES)
        if arab_born and arab_born[0] in labels:
            out[title]["origin"] = labels[arab_born[0]][1]
        else:
            out[title]["origin"] = heritage_country(heritage.get(title, ""))
    return out


def with_origin(nationality, origin):
    """'Sweden' + 'Iraq' -> 'Sweden, Iraq' (flags only; the first one is shown
    everywhere except the Arab fighters section)."""
    if not origin or origin.lower() in (nationality or "").lower():
        return nationality or origin or ""
    return f"{nationality}, {origin}" if nationality else origin


# ------------------------------------------------------------------- photos
# Fighter photos from Wikimedia Commons. Only FREE licenses (CC BY, CC BY-SA,
# public domain, ...): we save the link and the credit, never the image.
# The app loads the small thumbnail straight from Wikimedia and shows the credit.

def _plain(html):
    """'<a href=..>John Smith</a>' -> 'John Smith'."""
    text = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()[:80]


# Wikipedias checked for a fighter's photo when the English page has none.
PHOTO_LANGS = ["pt", "es", "ru", "fr", "pl", "de", "it", "nl", "uk", "sv", "ar", "ja", "ko"]


def fighter_photos(titles, width=400):
    """English Wikipedia titles -> {title: {"url", "credit", "license", "page"}}.

    1) the free lead photo of the English Wikipedia page;
    2) otherwise the Wikidata photo (P18). Wikidata photos live on Wikimedia
       Commons, which only hosts free files, so this finds extra photos for
       fighters whose English page has none. Licenses are still checked.
    """
    titles = sorted({t for t in titles if t})
    files, qids = {}, {}  # title -> "File:Name.jpg" / Wikidata id
    for i in range(0, len(titles), 50):
        chunk = titles[i:i + 50]
        params = {"action": "query", "prop": "pageimages|pageprops", "piprop": "name",
                  "pilicense": "free", "ppprop": "wikibase_item", "titles": "|".join(chunk),
                  "redirects": "1", "format": "json", "formatversion": "2"}
        try:
            data = requests.get(API, params=params, headers=HEADERS, timeout=30).json().get("query", {})
        except Exception as e:
            print(f"  photo lookup skipped: {e}")
            continue
        back = {n["to"]: n["from"] for n in data.get("normalized", [])}
        for r in data.get("redirects", []):
            back[r["to"]] = back.get(r["from"], r["from"])
        for page in data.get("pages", []):
            title = back.get(page["title"], page["title"])
            name = page.get("pageimage")
            if name:
                files[title] = "File:" + name
            qid = (page.get("pageprops") or {}).get("wikibase_item")
            if qid:
                qids[title] = qid
        time.sleep(1)

    # Wikidata photo for everyone still without one (and their Commons
    # category, P373, used as the last source below).
    cats = {}  # title -> Commons category name
    missing = {qids[t]: t for t in qids if t not in files}
    ids = sorted(missing)
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        params = {"action": "wbgetentities", "ids": "|".join(chunk), "props": "claims", "format": "json"}
        try:
            data = requests.get("https://www.wikidata.org/w/api.php", params=params,
                                headers=HEADERS, timeout=30).json().get("entities", {})
        except Exception as e:
            print(f"  Wikidata photo lookup skipped: {e}")
            continue
        for qid, entity in data.items():
            for claim in (entity.get("claims") or {}).get("P373", []):
                value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
                if isinstance(value, str) and value:
                    cats[missing[qid]] = value
                    break
            for claim in (entity.get("claims") or {}).get("P18", []):
                value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
                if isinstance(value, str) and value:
                    files[missing[qid]] = "File:" + value
                    break
        time.sleep(1)

    # Other-language Wikipedias (Portuguese for Brazilians, Russian for
    # Dagestanis, ...): their free lead photo. Free files from there live on
    # Wikimedia Commons; anything only stored on that local wiki is dropped
    # below, because the license check only reads Commons files.
    still = [t for t in titles if t not in files]
    links = {}  # title -> {lang: local title}
    for i in range(0, len(still), 50):
        chunk = still[i:i + 50]
        params = {"action": "query", "prop": "langlinks", "lllimit": "max",
                  "titles": "|".join(chunk), "redirects": "1", "format": "json", "formatversion": "2"}
        try:
            data = requests.get(API, params=params, headers=HEADERS, timeout=30).json().get("query", {})
        except Exception as e:
            print(f"  language links skipped: {e}")
            continue
        back = {n["to"]: n["from"] for n in data.get("normalized", [])}
        for r in data.get("redirects", []):
            back[r["to"]] = back.get(r["from"], r["from"])
        for page in data.get("pages", []):
            title = back.get(page["title"], page["title"])
            for ll in page.get("langlinks", []):
                if ll.get("lang") in PHOTO_LANGS:
                    links.setdefault(title, {})[ll["lang"]] = ll.get("title", "")
        time.sleep(1)
    for lang in PHOTO_LANGS:
        wanted = {lt: t for t, d in links.items() if t not in files and (lt := d.get(lang))}
        names_here = sorted(wanted)
        for i in range(0, len(names_here), 50):
            chunk = names_here[i:i + 50]
            params = {"action": "query", "prop": "pageimages", "piprop": "name", "pilicense": "free",
                      "titles": "|".join(chunk), "redirects": "1", "format": "json", "formatversion": "2"}
            try:
                data = requests.get(f"https://{lang}.wikipedia.org/w/api.php", params=params,
                                    headers=HEADERS, timeout=30).json().get("query", {})
            except Exception as e:
                print(f"  {lang} photo lookup skipped: {e}")
                continue
            back = {n["to"]: n["from"] for n in data.get("normalized", [])}
            for r in data.get("redirects", []):
                back[r["to"]] = back.get(r["from"], r["from"])
            for page in data.get("pages", []):
                local = back.get(page["title"], page["title"])
                name = page.get("pageimage")
                if name and local in wanted and wanted[local] not in files:
                    files[wanted[local]] = "File:" + name
            time.sleep(1)

    # Last source: the fighter's own Wikimedia Commons category (free event
    # photos people uploaded). Only a file whose name has the fighter's last
    # name and doesn't look like a group or fight photo of two people.
    for title, cat in sorted(cats.items()):
        if title in files:
            continue
        last = _letters((re.sub(r"\s*\(.*?\)", "", title).split() or [""])[-1])  # "Jon Jones (fighter)" -> jones
        if len(last) < 3:
            continue
        params = {"action": "query", "list": "categorymembers", "cmtitle": "Category:" + cat,
                  "cmtype": "file", "cmlimit": "50", "format": "json", "formatversion": "2"}
        try:
            members = requests.get("https://commons.wikimedia.org/w/api.php", params=params,
                                   headers=HEADERS, timeout=30).json().get("query", {}).get("categorymembers", [])
        except Exception as e:
            print(f"  Commons category skipped: {type(e).__name__}")
            continue
        for m in members:
            name = m.get("title", "")
            low = name.lower()
            if (last in _letters(name) and low.endswith((".jpg", ".jpeg", ".png"))
                    and not re.search(r"\bvs?\.?\b|\band\b|&|\bwith\b|poster|logo|signature", low)):
                files[title] = name
                break
        time.sleep(0.5)

    out, names = {}, sorted(set(files.values()))
    info = {}
    for i in range(0, len(names), 50):
        chunk = names[i:i + 50]
        params = {"action": "query", "prop": "imageinfo", "iiprop": "url|extmetadata",
                  "iiurlwidth": str(width), "titles": "|".join(chunk),
                  "format": "json", "formatversion": "2"}
        try:
            # Wikimedia Commons only hosts freely licensed files.
            data = requests.get("https://commons.wikimedia.org/w/api.php", params=params,
                                headers=HEADERS, timeout=30).json().get("query", {})
        except Exception as e:
            print(f"  photo details skipped: {e}")
            continue
        back = {n["to"]: n["from"] for n in data.get("normalized", [])}
        for page in data.get("pages", []):
            if page.get("missing") or page.get("imagerepository") not in (None, "local"):
                continue  # not stored on Commons itself
            ii = (page.get("imageinfo") or [{}])[0]
            meta = ii.get("extmetadata") or {}
            value = lambda k: (meta.get(k) or {}).get("value", "")
            license_name = _plain(value("LicenseShortName"))
            # Skip anything not clearly free.
            if value("NonFree") or not license_name or "fair use" in license_name.lower():
                continue
            if not ii.get("thumburl"):
                continue
            info[back.get(page["title"], page["title"])] = {
                "url": ii["thumburl"],
                "credit": _plain(value("Artist")) or "Wikimedia Commons",
                "license": license_name,
                "page": ii.get("descriptionurl", ""),
            }
        time.sleep(1)
    for title, name in files.items():
        if name in info:
            out[title] = info[name]
    return out


# Openverse (openverse.org, run by WordPress): one search over openly licensed
# photos from Flickr, Wikimedia and others. Only licenses that allow
# commercial use AND edits (we crop to a circle); never "NC" or "ND".
OPENVERSE = "https://api.openverse.org/v1/images/"
# OFF (Oct 26): Flickr users sometimes upload UFC/Getty photos marked as free,
# and a bot can't tell. Photos it already added are removed (source
# "openverse"). Could come back later only with approval in the admin screen.
OPENVERSE_PER_RUN = 0
_OV_LICENSES = {"by": "CC BY", "by-sa": "CC BY-SA", "cc0": "CC0", "pdm": "Public domain"}
_OV_NOT_SOLO = re.compile(r"\bvs?\.?\b|\band\b|&|\bwith\b|\bfans?\b|poster|logo|signature|crowd|team|"
                          r"belt\b.*\bbelt|\bfight\b.*\bfight", re.I)


def openverse_photo(name):
    """A free photo whose title names this fighter (first and last name), or None."""
    words = [w for w in re.split(r"\s+", strip_title(name)) if w]
    if len(words) < 2:
        return None
    full, first, last = _letters(" ".join(words)), _letters(words[0]), _letters(words[-1])
    params = {"q": f'"{" ".join(words)}"', "license": ",".join(_OV_LICENSES), "mature": "false",
              "page_size": "20", "category": "photograph"}
    r = requests.get(OPENVERSE, params=params, headers=HEADERS, timeout=30)
    if r.status_code == 429:
        raise RuntimeError("Openverse daily limit")
    if r.status_code != 200:
        return None
    for it in r.json().get("results") or []:
        title = it.get("title") or ""
        t = _letters(title)
        tokens = {_letters(w) for w in re.split(r"[\s\-_,.|:()]+", title)}
        lic = (it.get("license") or "").lower()
        if lic not in _OV_LICENSES or not (full in t or (first in tokens and last in tokens)):
            continue
        if _OV_NOT_SOLO.search(title) or it.get("mature"):
            continue
        url = it.get("url") or ""
        if not url.startswith("https://") or (it.get("width") or 0) < 300:
            continue
        version = it.get("license_version") or ""
        label = _OV_LICENSES[lic] + (f" {version}" if version and lic not in ("cc0", "pdm") else "")
        return {"url": url, "credit": (it.get("creator") or it.get("source") or "Openverse")[:80],
                "license": label, "page": it.get("foreign_landing_url") or "", "source": "openverse"}
    return None


def add_arabic(fights):
    """Adds Arabic names, Arab flags and permanent fighter IDs to each fight."""
    info = fighter_info([f.get("_linkA") for f in fights] + [f.get("_linkB") for f in fights])
    for f in fights:
        a = info.get(f.pop("_linkA", None) or "", {})
        b = info.get(f.pop("_linkB", None) or "", {})
        f["fighterAAr"], f["fighterBAr"] = a.get("ar", ""), b.get("ar", "")
        f["fighterAArab"] = is_arab(f["fighterA"], a.get("arab"))
        f["fighterBArab"] = is_arab(f["fighterB"], b.get("arab"))
        f["fighterAId"], f["fighterBId"] = fighter_key(f["fighterA"]), fighter_key(f["fighterB"])
        # Nationality for flags, also for fighters without a roster profile yet.
        f["fighterANat"] = with_origin(a.get("natEn", ""), a.get("origin", ""))
        f["fighterBNat"] = with_origin(b.get("natEn", ""), b.get("origin", ""))
    return fights


def event_name_ar(name, fights):
    """'UFC 330: Makhachev vs. Machado Garry' -> 'UFC 330: ماخاتشيف ضد غاري'."""
    prefix, colon, rest = name.partition(":")
    prefix = prefix.replace("Fight Night", "فايت نايت")
    if not colon:
        return prefix
    main = next((f for f in fights if f.get("isMain")), None)
    if main and main.get("fighterAAr") and main.get("fighterBAr"):
        rest = f"{main['fighterAAr'].split()[-1]} ضد {main['fighterBAr'].split()[-1]}"
    else:
        rest = rest.replace(" vs. ", " ضد ").strip()
    return f"{prefix}: {rest}"

# ---------------------------------------------------------- start time guess

# The main card usually starts at a fixed local hour. This is a guess:
# if it's wrong for an event, change it once in the admin screen and the
# bot will leave your time alone from then on.
ZONES = [
    ("Perth", "Australia/Perth", 12), ("Australia", "Australia/Sydney", 13),
    ("New Zealand", "Pacific/Auckland", 15),
    ("United Arab Emirates", "Asia/Dubai", 22), ("Qatar", "Asia/Qatar", 21),
    ("Saudi Arabia", "Asia/Riyadh", 21), ("Azerbaijan", "Asia/Baku", 22),
    ("England", "Europe/London", 20), ("Scotland", "Europe/London", 20),
    ("Northern Ireland", "Europe/London", 20), ("Ireland", "Europe/Dublin", 20),
    ("France", "Europe/Paris", 21), ("Germany", "Europe/Berlin", 21),
    ("Serbia", "Europe/Belgrade", 21), ("Poland", "Europe/Warsaw", 21),
    ("Macau", "Asia/Macau", 20), ("China", "Asia/Shanghai", 20),
    ("Singapore", "Asia/Singapore", 20), ("Japan", "Asia/Tokyo", 20),
    ("South Korea", "Asia/Seoul", 20),
]


def guess_start(date, city):
    for key, zone, hour in ZONES:
        if key in city:
            local = datetime(date.year, date.month, date.day, hour, tzinfo=ZoneInfo(zone))
            return local.astimezone(timezone.utc)
    # Americas: main card around 10 pm US Eastern
    local = datetime(date.year, date.month, date.day, 22, tzinfo=ZoneInfo("America/New_York"))
    return local.astimezone(timezone.utc)


# ------------------------------------------------------------ automatic alerts
# Everyone with notifications on is subscribed (by the app) to "general_ar" or
# "general_en", depending on their language. These alerts use a normal sound,
# not the 60-second fight bell.

_METHODS_AR = [
    ("decision (unanimous)", "قرار بالإجماع"), ("decision (split)", "قرار منقسم"),
    ("decision (majority)", "قرار بالأغلبية"), ("technical decision", "قرار فني"),
    ("decision", "قرار الحكام"), ("tko", "ضربة قاضية فنية"), ("ko", "ضربة قاضية"),
    ("technical submission", "استسلام فني"), ("submission", "استسلام"),
    ("disqualification", "استبعاد"), ("draw", "تعادل"), ("no contest", "لا نتيجة"),
    ("doctor stoppage", "توقف طبي"), ("retirement", "انسحاب"),
]


def method_ar(method):
    parts = []
    for part in method.split(" · "):
        low = part.lower().strip()
        m = re.match(r"round (\d+)$", low)
        if m:
            parts.append(f"الجولة {m.group(1)}")
            continue
        if re.match(r"^\d+:\d+$", low):
            parts.append(part.strip())
            continue
        for en, ar in _METHODS_AR:
            if low.startswith(en):
                parts.append(ar)
                break
        else:
            parts.append(part.strip())
    return " · ".join(p for p in parts if p)


def _ios_sound():
    from firebase_admin import messaging
    return messaging.APNSConfig(payload=messaging.APNSPayload(aps=messaging.Aps(sound="default")))


def send_alert(title_ar, body_ar, title_en, body_en):
    from firebase_admin import messaging
    for topic, title, body in (("general_ar", title_ar, body_ar), ("general_en", title_en, body_en)):
        messaging.send(messaging.Message(
            topic=topic,
            notification=messaging.Notification(title=title, body=body),
            apns=_ios_sound(),  # iPhone: play a sound (without this it arrives silently)
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(channel_id="general_alerts"),
            ),
        ))
    print(f"  alert sent: {title_en} / {body_en}")


# Alert bookkeeping ("this alert was already sent") lives in eventState/{id},
# NOT in the event document: every phone watching an event re-downloads it on
# each change, so writing bookkeeping there cost one read per phone per write.
# Older events still have these fields on the event itself; both are read.
_states = {}


def _state(ref):
    if ref.id not in _states:
        from firebase_admin import firestore
        snap = firestore.client().collection("eventState").document(ref.id).get()
        _states[ref.id] = (snap.to_dict() or {}) if snap.exists else {}
    return _states[ref.id]


def _save_state(ref, data):
    from firebase_admin import firestore
    _state(ref).update(data)
    firestore.client().collection("eventState").document(ref.id).set(data, merge=True)


def _sent(ref, doc, key):
    return bool(doc.get(key) or _state(ref).get(key))


def check_alerts(ref, doc):
    """Event starting within the hour, and main event result. Each is sent once."""
    if MODE == "backfill":
        return
    start = doc.get("start")
    if not start:
        return
    now = datetime.now(timezone.utc)
    minutes = (start - now).total_seconds() / 60
    fights = doc.get("fights") or []
    main = next((f for f in fights if f.get("isMain")), fights[0] if fights else None)
    name_en = doc.get("name", "UFC")
    name_ar = doc.get("nameAr") or name_en

    def names(f):
        a, b = f.get("fighterA", ""), f.get("fighterB", "")
        return a, b, f.get("fighterAAr") or a, f.get("fighterBAr") or b

    # 1) Starting soon: from 70 minutes before until 50 minutes after the start,
    # a 2-hour window, so even the every-2-hours check can't miss it.
    if -50 <= minutes <= 70 and not _sent(ref, doc, "alertStartSent"):
        m = max(0, round(minutes))
        body_en = body_ar = ""
        if main:
            a, b, a_ar, b_ar = names(main)
            body_en, body_ar = f"Main event: {a} vs {b}", f"النزال الرئيسي: {a_ar} ضد {b_ar}"
        if m >= 5:
            send_alert(f"⏰ {name_ar} يبدأ خلال {m} دقيقة", body_ar,
                       f"⏰ {name_en} starts in {m} minutes", body_en)
        else:
            send_alert(f"🔴 {name_ar} بدأ الآن", body_ar, f"🔴 {name_en} is starting now", body_en)
        _save_state(ref, {"alertStartSent": True})

    # 2) Main event result, only for events from the last 36 hours.
    if main and main.get("winner") and -36 * 60 <= minutes <= 0 and not _sent(ref, doc, "alertResultSent"):
        a, b, a_ar, b_ar = names(main)
        w = main["winner"]
        how_en, how_ar = main.get("method", ""), method_ar(main.get("method", ""))
        if w in (a, b):
            loser, w_ar, l_ar = (b, a_ar, b_ar) if w == a else (a, b_ar, a_ar)
            body_en = f"{w} def. {loser}" + (f" · {how_en}" if how_en else "")
            body_ar = f"{w_ar} تغلّب على {l_ar}" + (f" · {how_ar}" if how_ar else "")
        else:
            body_en = f"{a} vs {b}: {how_en or w}"
            body_ar = f"{a_ar} ضد {b_ar}: {how_ar or method_ar(w)}"
        send_alert(f"🏆 نتيجة النزال الرئيسي · {name_ar}", body_ar,
                   f"🏆 Main event result · {name_en}", body_en)
        _save_state(ref, {"alertResultSent": True})


def send_fight_alert(fight_id, title_ar, body_ar, title_en, body_en, bell=False):
    """To fans who tapped the bell on this fight (topic "fight_<id>"). The app
    also puts them in general_ar or general_en, which picks the language."""
    from firebase_admin import messaging
    for lang, title, body in (("ar", title_ar, body_ar), ("en", title_en, body_en)):
        messaging.send(messaging.Message(
            condition=f"'fight_{fight_id}' in topics && 'general_{lang}' in topics",
            notification=messaging.Notification(title=title, body=body),
            apns=_ios_sound(),  # iPhone: play a sound (without this it arrives silently)
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="fight_alerts" if bell else "general_alerts"),
            ),
        ))
    print(f"  fight alert sent: {title_en} / {body_en}")


def check_fight_alerts(ref, doc):
    """Bell alerts for every fight, prelims and early prelims too. Each fight
    gets one "walkouts" alarm, a "fight started" note when round 1 begins, a
    short note at the start of each later round, and one "result" alert
    (saved in fightAlertsSent).
    Walkouts = the fight before it just ended (the first fight: the event
    start time), or BALLDONTLIE already says it's live.
    The main event result already goes to everyone, so it's skipped here."""
    if MODE == "backfill":
        return
    start = doc.get("start")
    fights = doc.get("fights") or []
    if not start or not fights:
        return
    now = datetime.now(timezone.utc)
    if not start - timedelta(hours=6) <= now <= start + timedelta(hours=36):
        return
    sent = set(doc.get("fightAlertsSent") or []) | set(_state(ref).get("fightAlertsSent") or [])
    before = len(sent)
    main = next((f for f in fights if f.get("isMain")), fights[0])
    name_en = doc.get("name", "UFC")
    name_ar = doc.get("nameAr") or name_en

    # The card is saved main event first, so the night runs from the end of the list.
    for i, f in enumerate(fights):
        fid = f.get("id")
        if not fid:
            continue
        a, b = f.get("fighterA", ""), f.get("fighterB", "")
        a_ar, b_ar = f.get("fighterAAr") or a, f.get("fighterBAr") or b
        if fid in _bdl_cancelled:
            continue  # dropped from the card during the night: no alerts for it
        # The fight before it on the card, skipping fights cancelled during the
        # night (otherwise the walkout alarm would wait forever for their result).
        j = i + 1
        while j < len(fights) and fights[j].get("id") in _bdl_cancelled:
            j += 1
        prev = fights[j] if j < len(fights) else None

        live_now = not f.get("winner") and fid in _bdl_live
        if not f.get("winner") and f"start:{fid}" not in sent and not live_now and now <= start + timedelta(hours=8):
            if (prev and prev.get("winner")) or (not prev and now >= start - timedelta(minutes=5)):
                send_fight_alert(fid, f"🔔 دخول المقاتلين الآن: {a_ar} ضد {b_ar}", f"النزال يبدأ خلال دقائق · {name_ar}",
                                 f"🔔 Walkouts now: {a} vs {b}", f"The fight starts in a few minutes · {name_en}", bell=True)
                sent.add(f"start:{fid}")

        # Second note when the fight itself begins (round 1). If the walkout
        # alarm was never sent, this one rings the bell instead.
        rnd = _bdl_round.get(fid, 0)
        if live_now and f"live:{fid}" not in sent:
            send_fight_alert(fid, f"🔴 بدأ النزال: {a_ar} ضد {b_ar}", name_ar,
                             f"🔴 Fight started: {a} vs {b}", name_en, bell=f"start:{fid}" not in sent)
            sent.update({f"live:{fid}", f"start:{fid}"} | {f"round:{fid}:{r}" for r in range(2, rnd + 1)})
        elif live_now and rnd >= 2 and f"round:{fid}:{rnd}" not in sent:
            send_fight_alert(fid, f"⏱️ الجولة {rnd} بدأت: {a_ar} ضد {b_ar}", name_ar,
                             f"⏱️ Round {rnd} underway: {a} vs {b}", name_en)
            sent.update({f"round:{fid}:{r}" for r in range(2, rnd + 1)})

        if f.get("winner") and f is not main and f"result:{fid}" not in sent:
            w = f["winner"]
            how_en, how_ar = f.get("method", ""), method_ar(f.get("method", ""))
            if w in (a, b):
                loser, w_ar, l_ar = (b, a_ar, b_ar) if w == a else (a, b_ar, a_ar)
                body_en = f"{w} def. {loser}" + (f" · {how_en}" if how_en else "")
                body_ar = f"{w_ar} تغلّب على {l_ar}" + (f" · {how_ar}" if how_ar else "")
            else:
                body_en = f"{a} vs {b}: {how_en or w}"
                body_ar = f"{a_ar} ضد {b_ar}: {how_ar or method_ar(w)}"
            send_fight_alert(fid, f"🏆 نتيجة النزال · {name_ar}", body_ar,
                             f"🏆 Fight result · {name_en}", body_en)
            sent.add(f"result:{fid}")
            sent.update({f"start:{fid}", f"live:{fid}"})  # finished: never send "starting" afterwards

        if len(sent) != before:  # save after each fight, so a failure can't cause repeats
            _save_state(ref, {"fightAlertsSent": sorted(sent)})
            before = len(sent)


def check_pick_reminder(ref, doc):
    """'Picks lock soon' reminder, sent once, 3 to 9 hours before the start
    (a 6-hour window, so the every-2-hours run can't miss it)."""
    if MODE == "backfill":
        return
    start = doc.get("start")
    fights = doc.get("fights") or []
    if not start or not fights:
        return
    minutes = (start - datetime.now(timezone.utc)).total_seconds() / 60
    if not 3 * 60 <= minutes <= 9 * 60 or _sent(ref, doc, "pickReminderSent"):
        return
    h = max(3, round(minutes / 60))
    name_en = doc.get("name", "UFC")
    name_ar = doc.get("nameAr") or name_en
    send_alert(f"🎯 التوقعات تُغلق بعد {h} ساعات · {name_ar}",
               "اختر الفائزين الآن واصعد في الترتيب!",
               f"🎯 Picks lock in {h} hours · {name_en}",
               "Make your picks now and climb the leaderboard!")
    _save_state(ref, {"pickReminderSent": True})


def update_pick_split(db, ref, event_id, doc):
    """How many fans picked each fighter, for upcoming events in the next
    14 days. Only counts are saved on the event (nobody's picks are shown).
    pickSplit = {fightId: {"a": n, "b": n}}, pickCount = number of players."""
    if MODE == "backfill":
        return
    start = doc.get("start")
    fights = doc.get("fights") or []
    if not start or not fights:
        return
    minutes = (start - datetime.now(timezone.utc)).total_seconds() / 60
    if not 0 < minutes <= 14 * 24 * 60:
        return
    by_id = {f.get("id"): f for f in fights}
    split = {fid: {"a": 0, "b": 0} for fid in by_id if fid}
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter
        query = db.collection("picks").where(filter=FieldFilter("eventId", "==", event_id))
    except ImportError:
        query = db.collection("picks").where("eventId", "==", event_id)
    players = 0
    for snap in query.stream():
        picks = (snap.to_dict() or {}).get("picks") or {}
        if picks:
            players += 1
        for fid, choice in picks.items():
            f = by_id.get(fid)
            if not f:
                continue
            if choice == f.get("fighterA"):
                split[fid]["a"] += 1
            elif choice == f.get("fighterB"):
                split[fid]["b"] += 1
    split = {k: v for k, v in split.items() if v["a"] or v["b"]}
    if split != (doc.get("pickSplit") or {}) or players != doc.get("pickCount"):
        ref.set({"pickSplit": split, "pickCount": players}, merge=True)
        print(f"  pick split saved: {players} players")


def send_fighter_alert(fighter_id, title_ar, body_ar, title_en, body_en):
    from firebase_admin import messaging
    for lang, title, body in (("ar", title_ar, body_ar), ("en", title_en, body_en)):
        messaging.send(messaging.Message(
            topic=f"fighter_{fighter_id}_{lang}",
            notification=messaging.Notification(title=title, body=body),
            apns=_ios_sound(),  # iPhone: play a sound (without this it arrives silently)
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(channel_id="general_alerts"),
            ),
        ))


def follow_alerts(doc_existed, old_fights, new_fights, name_en, name_ar, start):
    """Tell followers when their fighter is booked, and when they win or lose."""
    if MODE == "backfill" or not start:
        return
    now = datetime.now(timezone.utc)
    minutes = (start - now).total_seconds() / 60
    old_by_id = {f.get("id"): f for f in old_fights or []}
    for f in new_fights or []:
        a, b = f.get("fighterA", ""), f.get("fighterB", "")
        a_ar, b_ar = f.get("fighterAAr") or a, f.get("fighterBAr") or b
        ids = [(f.get("fighterAId"), a, a_ar, b, b_ar), (f.get("fighterBId"), b, b_ar, a, a_ar)]
        old = old_by_id.get(f.get("id"))

        # New booking on an upcoming card (a brand-new event's card counts too).
        if old is None and minutes > 0:
            for fid, me, me_ar, opp, opp_ar in ids:
                if fid:
                    send_fighter_alert(fid, f"📅 نزال جديد لـ {me_ar}", f"ضد {opp_ar} · {name_ar}",
                                       f"📅 New fight for {me}", f"vs {opp} · {name_en}")

        # Fresh result, only for events from the last 36 hours.
        if f.get("winner") and not (old or {}).get("winner") and -36 * 60 <= minutes <= 0:
            how_en, how_ar = f.get("method", ""), method_ar(f.get("method", ""))
            for fid, me, me_ar, opp, opp_ar in ids:
                if not fid:
                    continue
                if f["winner"] == me:
                    send_fighter_alert(fid, f"🏆 {me_ar} فاز!", f"تغلّب على {opp_ar}" + (f" · {how_ar}" if how_ar else ""),
                                       f"🏆 {me} wins!", f"def. {opp}" + (f" · {how_en}" if how_en else ""))
                elif f["winner"] == opp:
                    send_fighter_alert(fid, f"{me_ar} خسر النزال", f"أمام {opp_ar}" + (f" · {how_ar}" if how_ar else ""),
                                       f"{me} lost", f"to {opp}" + (f" · {how_en}" if how_en else ""))
                else:
                    send_fighter_alert(fid, f"نتيجة نزال {me_ar}", f"ضد {opp_ar}: {how_ar or method_ar(f['winner'])}",
                                       f"{me}'s fight result", f"vs {opp}: {how_en or f['winner']}")


def sync_rankings(db):
    """Current UFC rankings per weight class, saved to rankings/current."""
    html = fetch_page_html("UFC rankings")
    if not html:
        print("Rankings: page not available")
        return
    soup = BeautifulSoup(html, "html.parser")
    divisions, seen = [], set()
    for table in soup.find_all("table"):
        grid = expand_table(table)
        if len(grid) < 3:
            continue
        header = [clean(c).lower() for c in grid[0]]
        if not {"rank", "fighter", "record"} <= set(header) or "weight class" in header:
            continue  # not a division table (or the pound-for-pound list)
        heading = table.find_previous(["h2", "h3", "h4"])
        name = clean(heading) if heading else ""
        if not name or "pound" in name.lower() or name.lower() in seen:
            continue  # the page lists older rankings further down: keep the newest
        seen.add(name.lower())
        i_rank, i_name, i_rec = header.index("rank"), header.index("fighter"), header.index("record")
        i_iso = header.index("iso") if "iso" in header else None
        rows = []
        for row in grid[1:]:
            if len(row) <= max(i_rank, i_name, i_rec) or row[i_name].name == "th":
                continue
            fighter = strip_title(clean(row[i_name]))
            if not fighter:
                continue
            iso = clean(row[i_iso]).upper() if i_iso is not None else ""
            rows.append({"rank": clean(row[i_rank]), "name": fighter, "iso": iso,
                         "record": clean(row[i_rec]), "id": fighter_key(fighter),
                         "arab": False, "_link": link_title(row[i_name])})
        if rows:
            divisions.append({"name": name, "rows": rows})

    info = fighter_info([r["_link"] for d in divisions for r in d["rows"]])
    photos = fighter_photos([r["_link"] for d in divisions for r in d["rows"]])
    for d in divisions:
        for r in d["rows"]:
            link = r.pop("_link") or ""
            extra = info.get(link, {})
            r["photo"] = photos.get(link)
            r["nameAr"] = extra.get("ar", "")
            r["arab"] = is_arab(r["name"], extra.get("arab"))
    print(f"Rankings: {len(divisions)} divisions")
    if db is not None and divisions:
        db.collection("rankings").document("current").set(
            {"divisions": divisions, "updated": datetime.now(timezone.utc)})



# ------------------------------------------------------------------ fighters
# Every current UFC fighter from Wikipedia's roster page, plus their last
# five results and next fight from the events the bot has saved.
# Saved to fighters/{fighterId}. Runs twice a day (and in "fighters" mode).

DIVISION_ORDER = ["Heavyweight", "Light heavyweight", "Middleweight", "Welterweight",
                  "Lightweight", "Featherweight", "Bantamweight", "Flyweight",
                  "Women's bantamweight", "Women's flyweight", "Women's strawweight"]


def canonical_division(text):
    t = (text or "").lower()
    women = "women" in t
    if "straw" in t:
        return "Women's strawweight"
    if women and "fly" in t:
        return "Women's flyweight"
    if women and "bantam" in t:
        return "Women's bantamweight"
    if women and "feather" in t:
        return "Women's featherweight"
    if "light heavy" in t or "light-heavy" in t:
        return "Light heavyweight"
    for name in ("Heavyweight", "Middleweight", "Welterweight", "Lightweight",
                 "Featherweight", "Bantamweight", "Flyweight"):
        if name.lower() in t:
            return name
    return ""


def parse_status(text):
    """'Win – UFC 331 (Los Angeles) – Tai Tuivasa' -> last result,
    'UFC 333 (Abu Dhabi) – Rizvan Kuniev' -> next fight."""
    text = (text or "").strip()
    parts = [p.strip() for p in re.split(r"\s+[–-]\s+", text) if p.strip()]
    if len(parts) >= 3 and parts[0].split()[0].lower() in ("win", "loss", "draw", "nc"):
        return {"last": {"result": parts[0].split()[0].capitalize(), "event": re.sub(r"\s*\(.*?\)", "", parts[1]),
                         "opponent": parts[2].split(" (")[0]}}
    if len(parts) >= 2 and parts[0].upper().startswith("UFC") and not text.startswith("("):
        return {"next": {"event": re.sub(r"\s*\(.*?\)", "", parts[0]), "opponent": parts[1].split(" /")[0].split(" (")[0]}}
    return {}


def parse_roster():
    html = fetch_page_html("List of current UFC fighters")
    if not html:
        raise RuntimeError("roster page not available")
    soup = BeautifulSoup(html, "html.parser")
    fighters, table_index = [], 0
    for table in soup.find_all("table"):
        grid = expand_table(table)
        if len(grid) < 2:
            continue
        header = [clean(c).lower() for c in grid[0]]
        if "name" not in header or "age" not in header or "mma record" not in header:
            continue  # not a division roster table
        heading = table.find_previous(["h2", "h3", "h4"])
        division = canonical_division(clean(heading) if heading else "")
        if not division and table_index < len(DIVISION_ORDER):
            division = DIVISION_ORDER[table_index]
        table_index += 1
        col = {h: i for i, h in enumerate(header)}
        status_col = next((i for h, i in col.items() if h.startswith("result")), None)
        ufc_col = next((i for h, i in col.items() if "endeavor" in h), None)
        for row in grid[1:]:
            if len(row) <= col["name"] or row[col["name"]].name == "th":
                continue
            raw_name = clean(row[col["name"]])
            name = strip_title(raw_name.replace("*", "")).strip()
            if not name:
                continue
            get = lambda key: clean(row[col[key]]) if key in col and col[key] < len(row) else ""
            status = clean(row[status_col]) if status_col is not None and status_col < len(row) else ""
            fighters.append({
                "name": name,
                "champion": "(c)" in raw_name.lower(),
                "division": division,
                "iso": get("iso").upper(),
                "_country": cell_country(row[col["iso"]]) if "iso" in col and col["iso"] < len(row) else "",
                "age": get("age"),
                "height": get("ht."),
                "nickname": get("nickname"),
                "status": status,
                "ufcRecord": clean(row[ufc_col]) if ufc_col is not None and ufc_col < len(row) else "",
                "mmaRecord": get("mma record"),
                "_link": link_title(row[col["name"]]),
            })
    return fighters


def fighter_history(db):
    """Last five results and the next fight for every fighter in saved events."""
    now = datetime.now(timezone.utc)
    recent, upcoming = {}, {}
    for snap in db.collection("events").stream():
        ev = snap.to_dict() or {}
        start = ev.get("start")
        if not start:
            continue
        for f in ev.get("fights") or []:
            sides = [(f.get("fighterAId"), f.get("fighterB", ""), f.get("fighterBAr", ""), f.get("fighterA", "")),
                     (f.get("fighterBId"), f.get("fighterA", ""), f.get("fighterAAr", ""), f.get("fighterB", ""))]
            for fid, opp, opp_ar, me in sides:
                if not fid:
                    continue
                base = {"eventId": snap.id, "event": ev.get("name", ""), "eventAr": ev.get("nameAr", ""),
                        "date": start, "opponent": opp, "opponentAr": opp_ar}
                if start > now and not f.get("winner"):
                    if fid not in upcoming or start < upcoming[fid]["date"]:
                        upcoming[fid] = base
                elif f.get("winner"):
                    w = f["winner"]
                    outcome = "W" if w == me else "L" if w == opp else ("D" if w == "Draw" else "NC")
                    recent.setdefault(fid, []).append({**base, "outcome": outcome, "method": f.get("method", "")})
    for fid in recent:
        recent[fid] = sorted(recent[fid], key=lambda r: r["date"], reverse=True)[:5]
    return recent, upcoming


def _keep_recent_clips(rows, old_rows):
    """Clips found earlier stay on the same fight when the list is rebuilt."""
    old = [r for r in old_rows or [] if r.get("clip")]
    out = []
    for r in rows:
        r = dict(r)
        for o in old:
            if (norm(o.get("opponent", "")) == norm(r.get("opponent", "")) and o.get("date") and r.get("date")
                    and abs((o["date"] - r["date"]).days) <= 3):
                r["clip"] = o["clip"]
                if o.get("clips"):
                    r["clips"] = o["clips"]
                break
        out.append(r)
    return out


def sync_fighters(db, with_records=False):
    roster = parse_roster()
    print(f"Fighters: {len(roster)} on the roster")
    if db is None or not roster:
        return
    info = fighter_info([f["_link"] for f in roster])
    photos = fighter_photos([f["_link"] for f in roster])
    print(f"Fighters: {len(photos)} free photos found")
    ranks = {}
    snap = db.collection("rankings").document("current").get()
    for d in (snap.to_dict() or {}).get("divisions", []) if snap.exists else []:
        for r in d.get("rows", []):
            ranks[r.get("id")] = r.get("rank", "")
    recent, upcoming = fighter_history(db)

    existing = {s.id: (s.to_dict() or {}) for s in db.collection("fighters").stream()}
    batch, count, written, seen = db.batch(), 0, 0, set()
    # One small document with every photo, so event syncs can put photos on
    # fights (Results tab) with a single read instead of one per fighter.
    photo_map = {fid: d["photo"] for fid, d in existing.items()
                 if d.get("photo") and d["photo"].get("source") != "openverse"}
    ov_left = OPENVERSE_PER_RUN
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)
    for f in roster:
        fid = fighter_key(f["name"])
        seen.add(fid)
        link = f.pop("_link") or ""
        roster_country = f.pop("_country", "")
        extra = info.get(link, {})
        old = existing.get(fid, {})
        wiki_recent = old.get("recentWiki", [])
        if with_records and link:
            page = fetch_page_html(link)
            if page:
                wiki_recent = parse_record(page)
            time.sleep(0.3)  # be polite to Wikipedia
        doc = {**f,
               "nameAr": extra.get("ar") or old.get("nameAr", ""),
               "arab": is_arab(f["name"], extra.get("arab")),
               "nationality": extra.get("natEn") or old.get("nationality", "") or roster_country,
               "nationalityAr": extra.get("natAr") or old.get("nationalityAr", ""),
               "rank": "C" if f["champion"] else ranks.get(fid, ""),
               "recent": _keep_recent_clips(merge_recent(recent.get(fid, []), wiki_recent), old.get("recent")),
               "recentWiki": wiki_recent,
               "next": upcoming.get(fid) or parse_status(f["status"]).get("next"),
               "last": parse_status(f["status"]).get("last"),
               "active": True}
        # Free photo (link + credit) if Wikipedia has one; keep the old one otherwise.
        doc["photo"] = photos.get(link) or old.get("photo") or None
        if doc["photo"] and doc["photo"].get("source") == "openverse":
            doc["photo"] = None  # Openverse is off: remove the photos it added
        # Still none: try Openverse once a month per fighter (a few dozen a run).
        searched = old.get("photoSearched")
        if not doc["photo"] and ov_left > 0 and not (isinstance(searched, datetime) and searched > month_ago):
            ov_left -= 1
            try:
                doc["photo"] = openverse_photo(f["name"])
                if doc["photo"]:
                    print(f"  Openverse photo: {f['name']} ({doc['photo']['license']})")
            except Exception as e:
                print(f"  Openverse skipped: {type(e).__name__} {e}")
                ov_left = 0
            doc["photoSearched"] = datetime.now(timezone.utc)
        elif searched:
            doc["photoSearched"] = searched
        if doc["photo"]:
            photo_map[fid] = doc["photo"]
        doc["searchTerms"] = search_terms(f["name"] + " " + doc["nameAr"], [])
        if any(old.get(k) != v for k, v in doc.items()):
            batch.set(db.collection("fighters").document(fid), doc, merge=True)
            count += 1
            written += 1
        if count >= 400:
            batch.commit()
            batch, count = db.batch(), 0
    for fid, old in existing.items():  # left the UFC: hide from the roster
        if fid not in seen and old.get("active"):
            batch.set(db.collection("fighters").document(fid), {"active": False}, merge=True)
            count += 1
    batch.commit()
    print(f"Fighters: {written} profiles updated")
    try:
        db.collection("meta").document("fighterPhotos").set({"photos": photo_map})
        print(f"Fighters: photo list saved ({len(photo_map)} photos)")
    except Exception as e:
        print(f"Fighters: photo list skipped: {e}")


def load_photo_map(db):
    """{fighterId: photo} saved by sync_fighters; {} if missing."""
    try:
        snap = db.collection("meta").document("fighterPhotos").get()
        return (snap.to_dict() or {}).get("photos", {}) if snap.exists else {}
    except Exception as e:
        print(f"Photo list not loaded: {e}")
        return {}


def attach_photos(fights, photo_map):
    """Puts each fighter's free photo (link + credit) on the fight, for the
    Results tab. Fighters without a free photo get none (the app shows initials)."""
    for f in fights:
        for side in ("A", "B"):
            photo = photo_map.get(f.get(f"fighter{side}Id") or "")
            if photo:
                f[f"photo{side}"] = photo
            else:
                f.pop(f"photo{side}", None)
    return fights


# ------------------------------------------------------------------- legends
# Every UFC champion in history (from Wikipedia's "List of UFC champions"),
# with the belts they held and their career record from their own page.
# Saved into the same fighters collection with legend: true.


def person_link(cell):
    """First real article link in a cell, skipping flag icons and images."""
    for a in cell.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("/wiki/") or ":" in href[6:] or "redlink" in a.get("class", []):
            continue
        if a.find("img") is not None or a.find_parent(class_="flagicon") is not None:
            continue
        if not a.get_text(strip=True):
            continue
        return a
    return None


def parse_date(text):
    text = re.sub(r"\s+", " ", (text or "").strip())
    for fmt in ("%B %d, %Y", "%d %B %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def parse_record(html, limit=5):
    """Most recent fights from a fighter's 'Mixed martial arts record' table."""
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        grid = expand_table(table)
        if len(grid) < 2:
            continue
        header = [clean(c).lower() for c in grid[0]]
        col = {}
        for i, h in enumerate(header):
            for key in ("res", "opponent", "method", "event", "date", "round", "time"):
                if h.startswith(key) and key not in col:
                    col[key] = i
        if not {"res", "opponent", "method"} <= set(col):
            continue
        fights = []
        for row in grid[1:]:
            get = lambda k: clean(row[col[k]]) if k in col and col[k] < len(row) else ""
            res = get("res").lower()
            outcome = {"win": "W", "loss": "L", "draw": "D", "nc": "NC"}.get(res.split()[0] if res else "")
            if not outcome:
                continue  # upcoming or cancelled
            method = get("method")
            parts = [method]
            if get("round"):
                parts.append(f"Round {get('round')}")
            if get("time"):
                parts.append(get("time"))
            fights.append({"outcome": outcome, "opponent": strip_title(get("opponent")), "opponentAr": "",
                           "method": " · ".join(p for p in parts if p), "event": get("event"),
                           "eventAr": "", "date": parse_date(get("date"))})
            if len(fights) >= limit:
                break
        if fights:
            return fights
    return []


def ufc_record(html):
    """UFC-only win-loss record counted from a fighter's full fight-record table."""
    rows = parse_record(html, limit=10000)
    ufc = [r for r in rows if re.search(r"\bufc\b|ultimate fighter", r.get("event", ""), re.I)]
    if not ufc:
        return ""
    count = {k: sum(1 for r in ufc if r["outcome"] == k) for k in ("W", "L", "D", "NC")}
    text = f"{count['W']}–{count['L']}" + (f"–{count['D']}" if count["D"] else "")
    return text + (f" ({count['NC']} NC)" if count["NC"] else "")


def merge_recent(saved, wiki, limit=5):
    """Our saved fights (with Arabic names) first, completed with Wikipedia's record."""
    out = list(saved or [])
    for w in wiki or []:
        dup = any(norm(w.get("opponent", "")) == norm(s.get("opponent", "")) and (
            not w.get("date") or not s.get("date") or abs((w["date"] - s["date"]).days) <= 3) for s in out)
        if not dup:
            out.append(w)
    far = datetime(1900, 1, 1, tzinfo=timezone.utc)
    out.sort(key=lambda r: r.get("date") or far, reverse=True)
    return out[:limit]


def champion_titles():
    """{link title: {"name": ..., "titles": [{"division", "interim"}]}}"""
    html = fetch_page_html("List of UFC champions")
    if not html:
        raise RuntimeError("champions page not available")
    soup = BeautifulSoup(html, "html.parser")
    champs = {}
    for table in soup.find_all("table"):
        grid = expand_table(table)
        if len(grid) < 2:
            continue
        header = [clean(c).lower() for c in grid[0]]
        name_col = next((i for i, h in enumerate(header) if h.startswith("name")), None)
        if name_col is None or not any("reign" in h for h in header):
            continue  # not a title-history table
        heading = table.find_previous(["h2", "h3", "h4"])
        division = canonical_division(clean(heading) if heading else "")
        if not division:
            continue
        for row in grid[1:]:
            if name_col >= len(row):
                continue
            cell = row[name_col]  # the champion column only, never the defenses
            if cell.name == "th" or not re.search(r"\bdef\b", cell.get_text(" ")):
                continue
            link = person_link(cell)
            if link is None:
                continue
            title = requests.utils.unquote(link["href"][len("/wiki/"):]).replace("_", " ")
            name = strip_title(clean(link))
            if not title or not name:
                continue
            # Interim only when the champion's own cell says so, or the row has no
            # title number (Wikipedia lists interim champions unnumbered, e.g. "—").
            # Never from the defenses/notes columns, which name other fighters' titles.
            # Only the champion's own line counts ("Name (interim) def. ..."),
            # never the notes after "def." (e.g. "previously won the interim title").
            number = clean(row[0]) if row else ""
            head = re.split(r"\bdef\b", cell.get_text(" "), maxsplit=1)[0].lower()
            interim = "interim" in head or not re.match(r"^\d", number)
            entry = champs.setdefault(title, {"name": name, "titles": []})
            t = {"division": division, "interim": interim}
            if t not in entry["titles"]:
                entry["titles"].append(t)
    # A full title beats an interim one in the same division.
    for entry in champs.values():
        full = {t["division"] for t in entry["titles"] if not t["interim"]}
        entry["titles"] = [t for t in entry["titles"] if not (t["interim"] and t["division"] in full)]
    return champs


def infobox_facts(title=None, html=None):
    """Nickname, nationality, years active and career record from a fighter's infobox."""
    if html is None:
        params = {"action": "parse", "page": title, "prop": "text", "section": "0",
                  "format": "json", "formatversion": "2", "redirects": "1"}
        try:
            data = requests.get(API, params=params, headers=HEADERS, timeout=30).json()
            html = data["parse"]["text"]
        except Exception:
            return {}
    box = BeautifulSoup(html, "html.parser").find("table", class_="infobox")
    if box is None:
        return {}
    facts, record, in_record = {}, {}, False
    for tr in box.find_all("tr"):
        th, td = tr.find("th"), tr.find("td")
        label = clean(th).lower() if th else ""
        if th and not td:  # section header row
            in_record = "mixed martial arts record" in label
            continue
        if not th or not td:
            continue
        value = clean(td)
        if label.startswith("nickname"):
            facts["nickname"] = value.split(" , ")[0].strip('"“” ')
        elif label == "nationality":
            facts["nationality"] = value
        elif label.startswith("years active"):
            facts["yearsActive"] = value
        elif label == "born":
            bday = td.find(class_="bday")
            if bday is not None:
                try:
                    b = datetime.strptime(bday.get_text(strip=True), "%Y-%m-%d")
                    today = datetime.now(timezone.utc)
                    facts["_age"] = str(today.year - b.year - ((today.month, today.day) < (b.month, b.day)))
                except ValueError:
                    pass
        elif label == "died":
            facts["_died"] = True
        elif label == "height":
            cm = re.search(r"(\d{3})\s*cm", value)
            m = re.search(r"(\d\.\d{2})\s*m\b", value)
            imperial = re.search(r"\d\s*ft\s*\d{1,2}\s*in", value)
            metres = f"{int(cm.group(1)) / 100:.2f}" if cm else (m.group(1) if m else "")
            if metres:
                facts["height"] = f"{imperial.group(0) if imperial else ''} ({metres} m)".strip()
        elif in_record and label in ("wins", "losses", "draws", "no contests"):
            m = re.match(r"\d+", value)
            if m:
                record[label] = int(m.group())
    if "wins" in record and "losses" in record:
        r = f"{record['wins']}–{record['losses']}"
        if record.get("draws"):
            r += f"–{record['draws']}"
        if record.get("no contests"):
            r += f" ({record['no contests']} NC)"
        facts["careerRecord"] = r
    years = facts.get("yearsActive", "").lower()
    facts["retired"] = bool(years) and "present" not in years
    age = facts.pop("_age", None)
    if age and not facts.pop("_died", False):
        facts["age"] = age
    facts.pop("_died", None)
    return facts


def build_fighter_index(db):
    """One small document with every fighter the app can search (name, Arabic
    name, rank, flag, record, photo link), so the app's search box suggests
    names as you type from a single read instead of a query per keystroke."""
    if db is None:
        return
    rows = []
    for snap in db.collection("fighters").stream():
        d = snap.to_dict() or {}
        if not (d.get("active") or d.get("legend")) or not d.get("name"):
            continue
        photo = d.get("photo") or {}
        row = {"id": snap.id, "name": d["name"]}
        for key in ("nameAr", "rank", "iso", "nationality", "mmaRecord", "careerRecord", "division"):
            if d.get(key):
                row[key] = d[key]
        if d.get("active"):
            row["active"] = True
        if d.get("legend"):
            row["legend"] = True
        if d.get("arab"):
            row["arab"] = True  # green Arab tag in search results
        if photo.get("url"):
            row["photo"] = {"url": photo["url"]}
        rows.append(row)
    rows.sort(key=lambda r: r["name"])
    db.collection("meta").document("fighterIndex").set({"fighters": rows, "updated": datetime.now(timezone.utc)})
    print(f"Fighter search index: {len(rows)} fighters")


def sync_legends(db):
    champs = champion_titles()
    print(f"Legends: {len(champs)} UFC champions in history")
    if db is None or not champs:
        return
    info = fighter_info(list(champs))
    photos = fighter_photos(list(champs))
    batch, count = db.batch(), 0
    existing = {d.id: (d.to_dict() or {}) for d in db.collection("fighters").stream()}
    for title, entry in champs.items():
        page = fetch_page_html(title) or ""
        facts = infobox_facts(html=page) if page else {}
        wiki_recent = parse_record(page) if page else []
        time.sleep(0.5)  # be polite to Wikipedia
        extra = info.get(title, {})
        fid = fighter_key(entry["name"])
        doc = {"name": entry["name"], "legend": True, "titles": entry["titles"],
               "wikiTitle": title, "arab": is_arab(entry["name"], extra.get("arab"))}
        if photos.get(title):
            doc["photo"] = photos[title]
        if extra.get("ar"):
            doc["nameAr"] = extra["ar"]
        for k in ("nickname", "nationality", "yearsActive", "careerRecord", "retired"):
            if facts.get(k) not in (None, ""):
                doc[k] = facts[k]
        if extra.get("natEn"):
            doc["nationality"] = extra["natEn"]
        if extra.get("natAr"):
            doc["nationalityAr"] = extra["natAr"]
        if not existing.get(fid, {}).get("active"):  # roster data is fresher for active fighters
            for k in ("age", "height"):
                if facts.get(k):
                    doc[k] = facts[k]
            record = ufc_record(page) if page else ""
            if record:
                doc["ufcRecord"] = record
        doc["searchTerms"] = search_terms(entry["name"] + " " + doc.get("nameAr", ""), [])
        if wiki_recent:
            doc["recentWiki"] = wiki_recent
            doc["recent"] = merge_recent(existing.get(fid, {}).get("recent", []) if existing.get(fid, {}).get("active") else [], wiki_recent)
        batch.set(db.collection("fighters").document(fid), doc, merge=True)
        count += 1
        if count >= 400:
            batch.commit()
            batch, count = db.batch(), 0
    champion_ids = {fighter_key(e["name"]) for e in champs.values()}
    removed = 0
    for fid, old in existing.items():
        if old.get("legend") and fid not in champion_ids:
            batch.set(db.collection("fighters").document(fid), {"legend": False, "titles": []}, merge=True)
            removed += 1
    batch.commit()
    print(f"Legends: {len(champs)} profiles saved, {removed} wrong legend marks removed")


# ---------------------------------------------------------------------- quiz
# Every Monday: 5 questions built from real data (recent results, champions,
# nicknames, legends), in Arabic and English. Saved to quizzes/current (the
# app reads this) and quizzes/{week} (archive).

import random

DIV_AR = {
    "Heavyweight": "الوزن الثقيل", "Light heavyweight": "الوزن الثقيل الخفيف",
    "Middleweight": "الوزن المتوسط", "Welterweight": "وزن الويلتر",
    "Lightweight": "الوزن الخفيف", "Featherweight": "وزن الريشة",
    "Bantamweight": "وزن الديك", "Flyweight": "وزن الذبابة",
    "Women's bantamweight": "وزن الديك للسيدات", "Women's flyweight": "وزن الذبابة للسيدات",
    "Women's strawweight": "وزن القش للسيدات", "Women's featherweight": "وزن الريشة للسيدات",
}


def week_id(day):
    iso = day.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _q(rng, q_ar, q_en, correct, wrong, explain_ar, explain_en):
    """correct and wrong options are (ar, en) pairs; returns a shuffled question."""
    options = [correct] + [w for w in wrong if w != correct][:3]
    if len(options) < 3:
        return None
    rng.shuffle(options)
    return {"q_ar": q_ar, "q_en": q_en,
            "options": [{"ar": a, "en": e} for a, e in options],
            "answer": options.index(correct),
            "explain_ar": explain_ar, "explain_en": explain_en}


def _kind(kind, q):
    """Marks a question 'history', 'recent' or 'champs' (the app shows a tag)."""
    if q:
        q["kind"] = kind
    return q


def _nm(name, name_ar):
    return (name_ar or name, name)


def build_quiz(db, rng):
    now = datetime.now(timezone.utc)
    events = [s.to_dict() or {} for s in db.collection("events").stream()]
    recent = [e for e in events if e.get("start") and now - timedelta(days=60) < e["start"] < now]
    fights = [(e, f) for e in recent for f in e.get("fights") or []
              if f.get("winner") in (f.get("fighterA"), f.get("fighterB"))]
    fighters = [s.to_dict() or {} for s in db.collection("fighters").stream()]
    active = [f for f in fighters if f.get("active")]
    names = [_nm(f.get("name", ""), f.get("nameAr", "")) for f in active if f.get("name")]
    questions = []

    def pick_others(exclude, pool, n=3):
        pool = [p for p in pool if p[1] not in exclude]
        return rng.sample(pool, min(n, len(pool)))

    # 1. Who won the main event of a recent card?
    mains = [(e, f) for e, f in fights if f.get("isMain")]
    if mains:
        e, f = rng.choice(mains)
        w_is_a = f["winner"] == f["fighterA"]
        w = _nm(f["fighterA"], f.get("fighterAAr")) if w_is_a else _nm(f["fighterB"], f.get("fighterBAr"))
        l = _nm(f["fighterB"], f.get("fighterBAr")) if w_is_a else _nm(f["fighterA"], f.get("fighterAAr"))
        ev_ar, ev_en = e.get("nameAr") or e.get("name", ""), e.get("name", "")
        questions.append(_kind("recent", _q(rng, f"من فاز بالنزال الرئيسي في {ev_ar}؟", f"Who won the main event of {ev_en}?",
                            w, [l] + pick_others({w[1], l[1]}, names, 2),
                            f"{w[0]} تغلّب على {l[0]} · {method_ar(f.get('method', ''))}",
                            f"{w[1]} def. {l[1]} · {f.get('method', '')}")))

    # 2. Who is the current champion of a division?
    snap = db.collection("rankings").document("current").get()
    for d in rng.sample((snap.to_dict() or {}).get("divisions", []) if snap.exists else [], 
                        len((snap.to_dict() or {}).get("divisions", [])) if snap.exists else 0):
        rows = d.get("rows", [])
        champ = next((r for r in rows if r.get("rank") == "C"), None)
        others = [r for r in rows if r.get("rank") not in ("C", "IC")][:10]
        division = canonical_division(d.get("name", ""))
        if champ and len(others) >= 3 and division:
            c = _nm(champ["name"], champ.get("nameAr"))
            wrong = [_nm(r["name"], r.get("nameAr")) for r in rng.sample(others, 3)]
            questions.append(_kind("champs", _q(rng, f"من هو بطل {DIV_AR.get(division, division)} الحالي في UFC؟",
                                f"Who is the current UFC {division} champion?", c, wrong,
                                f"{c[0]} هو بطل {DIV_AR.get(division, division)}.", f"{c[1]} holds the {division} title.")))
            break

    # 3. How did a recent fight end?
    kinds = {"KO/TKO": ("ضربة قاضية", "KO/TKO"), "Submission": ("استسلام", "Submission"),
             "Decision": ("قرار الحكام", "Decision")}
    def kind(method):
        m = method.lower()
        return "Decision" if m.startswith("decision") else "Submission" if m.startswith("submission") \
            else "KO/TKO" if m.startswith(("ko", "tko")) else None
    typed = [(e, f) for e, f in fights if kind(f.get("method", ""))]
    if typed:
        e, f = rng.choice(typed)
        w_is_a = f["winner"] == f["fighterA"]
        w = _nm(f["fighterA"], f.get("fighterAAr")) if w_is_a else _nm(f["fighterB"], f.get("fighterBAr"))
        l = _nm(f["fighterB"], f.get("fighterBAr")) if w_is_a else _nm(f["fighterA"], f.get("fighterAAr"))
        right = kinds[kind(f["method"])]
        questions.append(_kind("recent", _q(rng, f"كيف فاز {w[0]} على {l[0]}؟", f"How did {w[1]} beat {l[1]}?",
                            right, [v for v in kinds.values() if v != right],
                            f"{method_ar(f['method'])} · {e.get('nameAr') or e.get('name', '')}",
                            f"{f['method']} · {e.get('name', '')}")))

    # 4. Which fighter has this nickname?
    nicknamed = [f for f in active if 2 < len(f.get("nickname", "")) < 30]
    if nicknamed:
        f = rng.choice(nicknamed)
        same = [_nm(o["name"], o.get("nameAr")) for o in active
                if o.get("division") == f.get("division") and o.get("name") != f.get("name")]
        me = _nm(f["name"], f.get("nameAr"))
        questions.append(_kind("champs", _q(rng, f"من هو المقاتل الملقب بـ \"{f['nickname']}\"؟",
                            f"Which fighter is nicknamed \"{f['nickname']}\"?", me,
                            rng.sample(same, min(3, len(same))),
                            f"\"{f['nickname']}\" هو لقب {me[0]}.", f"\"{f['nickname']}\" is {me[1]}'s nickname.")))

    # 5. A legend's career record
    legends = [f for f in fighters if f.get("legend") and re.match(r"^\d+–\d+", f.get("careerRecord", ""))]
    if legends:
        f = rng.choice(legends)
        m = re.match(r"^(\d+)–(\d+)", f["careerRecord"])
        w, l = int(m.group(1)), int(m.group(2))
        variants = {f"{w + dw}–{max(0, l + dl)}" for dw, dl in [(-2, 1), (1, 2), (-1, 3), (3, 0), (-3, 2)]}
        variants.discard(f"{w}–{l}")
        me = _nm(f["name"], f.get("nameAr"))
        right = (f"{w}–{l}", f"{w}–{l}")
        questions.append(_kind("champs", _q(rng, f"ما هو السجل الاحترافي للأسطورة {me[0]} (فوز–خسارة)؟",
                            f"What was legend {me[1]}'s pro record (wins–losses)?", right,
                            [(v, v) for v in rng.sample(sorted(variants), 3)],
                            f"سجل {me[0]}: {f['careerRecord']}.", f"{me[1]}'s record: {f['careerRecord']}.")))

    return [q for q in questions if q]


QUIZ_START = datetime(2026, 10, 27).date()  # day 0 of the history question order


def quiz_day(now=None):
    """The quiz day as seen in the Gulf (UTC+3), where the new quiz arrives at
    18:00, e.g. '2026-10-27'. Phones pick the quiz by their own local time
    (also at 18:00), so 'current' carries yesterday's and tomorrow's quiz too."""
    return ((now or datetime.now(timezone.utc)) + timedelta(hours=3 + 6)).date()


def history_question(day, rng, extra=0):
    """One UFC history question a day, in a fixed shuffled order: a question
    only comes back after the whole bank has been used. extra=1/2: the extra
    questions of a quiet day, from a second order (a fresh block of spares
    each day, so consecutive days don't share them)."""
    from quiz_bank import HISTORY
    n = (day - QUIZ_START).days
    order = list(range(len(HISTORY)))
    random.Random("lcs-history-extra" if extra else "lcs-history").shuffle(order)
    i = order[(29 * n + extra - 1 if extra else n) % len(HISTORY)]
    qa, qe, ans, wrong, ea, ee = HISTORY[i]
    return _kind("history", _q(rng, qa, qe, ans, wrong, ea, ee))


def quiz_reminder(db, did):
    """One gentle reminder a week: Monday 20:00-22:59 Gulf time (18:00-20:59 in
    Morocco), after the new quiz has arrived everywhere in the Arab world; never twice."""
    local = datetime.now(timezone.utc) + timedelta(hours=3)
    if local.weekday() != 0 or not 20 <= local.hour <= 22:
        return
    ref = db.collection("meta").document("quizReminder")
    snap = ref.get()
    if snap.exists and (snap.to_dict() or {}).get("day") == did:
        return
    ref.set({"day": did})
    try:
        send_alert("🧠 كويز اليوم جاهز!", "3 أسئلة في 30 ثانية · حافظ على سلسلتك 🔥",
                   "🧠 Today's quiz is ready!", "3 questions in 30 seconds · keep your streak 🔥")
    except Exception as e:
        print(f"  quiz alert failed: {e}")


def make_quiz(db, day):
    """The 3-question quiz of one day (1 UFC history question + 1 about the
    latest events + 1 about champions/nicknames/legends), made once and kept:
    fans may already have played it."""
    did = day.isoformat()
    ref = db.collection("quizzes").document(did)
    snap = ref.get()
    if snap.exists and (snap.to_dict() or {}).get("questions"):
        return snap.to_dict()
    rng = random.Random(did)
    auto = build_quiz(db, rng)
    # Questions used in the 7 days before aren't repeated.
    used = set()
    for k in range(1, 8):
        old = db.collection("quizzes").document((day - timedelta(days=k)).isoformat()).get()
        for q in (old.to_dict() or {}).get("questions", []) if old.exists else []:
            used.add(q.get("q_en"))
    # History questions: the day's own, then spares (none used in the last 7 days).
    history = []
    for extra in range(0, 30):
        q = history_question(day, rng, extra)
        if q and q["q_en"] not in used and q["q_en"] not in {h["q_en"] for h in history}:
            history.append(q)
    picked = history[:1]
    for kind in ("recent", "champs"):
        options = [q for q in auto if q.get("kind") == kind and q.get("q_en") not in used]
        if options:
            picked.append(rng.choice(options))
    # Not enough recent/champion questions (quiet weeks): more history.
    for q in history[1:]:
        if len(picked) >= 3:
            break
        picked.append(q)
    picked = [q for q in picked if q]
    doc = {"weekId": did, "prevWeekId": (day - timedelta(days=1)).isoformat(), "daily": True,
           "questions": picked, "created": datetime.now(timezone.utc)}
    ref.set(doc)
    print(f"Quiz: {len(picked)} questions for {did}")
    return doc


def sync_quiz(db, force=False):
    """A new quiz every day at 18:00 local time in each country. The bot keeps
    tomorrow's quiz ready; 'current' (the one document phones listen to) holds
    today's quiz in the Gulf (read as-is by older app versions) plus
    'prevQuiz' and 'nextQuiz', and the phone picks the one for its own clock.
    It changes once a day. No daily notification: only a reminder on Mondays."""
    day = quiz_day()
    did = day.isoformat()
    current = db.collection("quizzes").document("current").get()
    cur = (current.to_dict() or {}) if current.exists else {}
    if force or cur.get("weekId") != did or (cur.get("nextQuiz") or {}).get("weekId") != (day + timedelta(days=1)).isoformat():
        today = make_quiz(db, day)
        nxt = make_quiz(db, day + timedelta(days=1))
        prev = db.collection("quizzes").document((day - timedelta(days=1)).isoformat()).get()
        slim = lambda d: {"weekId": d["weekId"], "prevWeekId": d["prevWeekId"], "questions": d["questions"]}
        doc = dict(today)
        doc["nextQuiz"] = slim(nxt)
        if prev.exists and (prev.to_dict() or {}).get("questions"):
            doc["prevQuiz"] = slim(prev.to_dict())
        db.collection("quizzes").document("current").set(doc)
        print(f"Quiz current: {did} (+ prev/next)")
    quiz_reminder(db, did)


# ---------------------------------------------------------------- predictions
# Fans save picks privately in picks/{eventId}_{uid}. About 12 hours after an
# event starts, the bot scores each person once: 1 point per correct pick,
# 3 for the main event. It writes their result on their own picks document and
# adds to their private totals (scores/{uid}). Nothing is public.

MAIN_EVENT_POINTS = 3


def method_kind(method):
    """"KO (punches) · Round 2 · 3:14" -> ("ko", 2). Kinds: ko, sub, dec, or ""."""
    low = (method or "").lower()
    first = low.split("·")[0].strip()
    if re.search(r"\bt?ko\b", first):
        kind = "ko"
    elif "submission" in first:
        kind = "sub"
    elif "decision" in first:
        kind = "dec"
    else:
        kind = ""
    m = re.search(r"round (\d+)", low)
    return kind, int(m.group(1)) if m else 0


def season_of(start):
    """Monthly leaderboard season of an event, e.g. '2026-11'. Uses the
    event's own day (US Saturday-night cards end after midnight UTC)."""
    return (start - timedelta(hours=8)).strftime("%Y-%m")


def score_event(db, ref, event_id, doc):
    from firebase_admin import firestore
    fights = doc.get("fights") or []
    results = {f.get("id"): f.get("winner", "") for f in fights}
    how = {f.get("id"): method_kind(f.get("method", "")) for f in fights}
    main_id = next((f.get("id") for f in fights if f.get("isMain")), None)
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter
        query = db.collection("picks").where(filter=FieldFilter("eventId", "==", event_id))
    except ImportError:
        query = db.collection("picks").where("eventId", "==", event_id)

    batch, count, players = db.batch(), 0, 0
    for snap in query.stream():
        pick = snap.to_dict() or {}
        uid = pick.get("uid")
        if not uid:
            continue
        points = correct = total = method_hits = round_hits = 0
        methods = pick.get("methods") or {}
        rounds = pick.get("rounds") or {}
        for fight_id, choice in (pick.get("picks") or {}).items():
            winner = results.get(fight_id)
            if not winner or winner in ("Draw", "No contest"):
                continue  # no result, or no winner to guess
            total += 1
            if choice == winner:
                correct += 1
                points += MAIN_EVENT_POINTS if fight_id == main_id else 1
                # Bonus: +1 for the right method, +1 more for the right round
                # (round only counts for KO/TKO and submission finishes).
                kind, rnd = how.get(fight_id, ("", 0))
                if kind and methods.get(fight_id) == kind:
                    points += 1
                    method_hits += 1
                    try:
                        picked_round = int(rounds.get(fight_id) or 0)
                    except (TypeError, ValueError):
                        picked_round = 0
                    if kind != "dec" and rnd and picked_round == rnd:
                        points += 1
                        round_hits += 1
        batch.set(snap.reference, {"result": {"points": points, "correct": correct, "total": total,
                                              "methodHits": method_hits, "roundHits": round_hits}},
                  merge=True)
        batch.set(db.collection("scores").document(uid), {
            "points": firestore.Increment(points),
            "correct": firestore.Increment(correct),
            "events": firestore.Increment(1),
            # Counted from now on, for the accuracy % in the app.
            "tracked": firestore.Increment(total),
            "trackedCorrect": firestore.Increment(correct),
            "methodHits": firestore.Increment(method_hits),
            "roundHits": firestore.Increment(round_hits),
            # Monthly season (leaderboard tab: this month / each past month).
            "months": {season_of(doc["start"]): {"points": firestore.Increment(points),
                                                 "correct": firestore.Increment(correct),
                                                 "events": firestore.Increment(1)}},
        }, merge=True)
        players += 1
        count += 2
        if count >= 400:  # Firestore allows 500 writes per batch
            batch.commit()
            batch, count = db.batch(), 0
    batch.set(ref, {"scored": True}, merge=True)
    batch.commit()
    print(f"  scored predictions: {players} players")

# -------------------------------------------------------------- leaderboard
# Public list of every player who chose a nickname (profiles/{uid}), plus every
# player's own rank saved in scores/{uid}. Built during normal runs only.

BLOCKED_WORDS = {
    # English
    "fuck", "shit", "bitch", "cunt", "dick", "pussy", "nigger", "nigga", "faggot", "whore",
    "slut", "nazi", "hitler", "porn", "asshole", "bastard",
    # Arabic / Darija
    # (short words like "nik" are left out: they appear inside normal names)
    "شرموط", "قحب", "منيك", "زامل", "حمار", "كلب",
    "zamel", "9a7ba", "qahba", "t9awed",
    # Impersonation
    "admin", "lcs", "moderator", "official", "ufc",
}


def clean_nickname(raw):
    """A safe nickname, or "" if it should not be shown."""
    name = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not 3 <= len(name) <= 20:
        return ""
    if not re.fullmatch(r"[\w .\-\u0600-\u06FF]+", name):
        return ""  # letters, digits, spaces, _ . - only (Arabic allowed)
    squashed = re.sub(r"[^a-z0-9\u0600-\u06FF]", "", name.lower())
    squashed = squashed.translate(str.maketrans("013457@$", "oieastas"))
    if any(w in squashed for w in BLOCKED_WORDS):
        return ""
    return name


def backfill_seasons(db, scores):
    """Once: monthly totals for events scored before seasons existed, rebuilt
    exactly from each player's scored picks (safe to run again)."""
    flag = db.collection("meta").document("seasons")
    snap = flag.get()
    if snap.exists and (snap.to_dict() or {}).get("backfilled"):
        return
    starts = {s.id: (s.to_dict() or {}).get("start") for s in db.collection("events").stream()}
    months = {}
    for p in db.collection("picks").stream():
        d = p.to_dict() or {}
        res, start = d.get("result"), starts.get(d.get("eventId"))
        if not isinstance(res, dict) or not d.get("uid") or not start:
            continue
        m = months.setdefault(d["uid"], {}).setdefault(season_of(start), {"points": 0, "correct": 0, "events": 0})
        m["points"] += int(res.get("points") or 0)
        m["correct"] += int(res.get("correct") or 0)
        m["events"] += 1
    batch, count = db.batch(), 0
    for uid, ms in months.items():
        batch.set(db.collection("scores").document(uid), {"months": ms}, merge=True)
        scores.setdefault(uid, {})["months"] = ms
        count += 1
        if count % 400 == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()
    flag.set({"backfilled": True}, merge=True)
    print(f"Seasons: monthly totals rebuilt for {len(months)} player(s)")


def build_leaderboard(db):
    scores = {s.id: (s.to_dict() or {}) for s in db.collection("scores").stream()}
    if not scores:
        return
    try:
        backfill_seasons(db, scores)
    except Exception as e:
        print(f"Seasons backfill failed: {type(e).__name__}")
    nicknames = {s.id: (s.to_dict() or {}).get("nickname", "") for s in db.collection("profiles").stream()}

    def num(d, k):
        try:
            return int(d.get(k) or 0)
        except (TypeError, ValueError):
            return 0

    ranked = sorted(scores.items(), key=lambda kv: (-num(kv[1], "points"), -num(kv[1], "correct")))
    top, prev, rank, used = [], None, 0, set()
    batch, count = db.batch(), 0
    for i, (uid, sc) in enumerate(ranked, start=1):
        key = (num(sc, "points"), num(sc, "correct"))
        if key != prev:  # same points and correct picks = same rank
            rank, prev = i, key
        nick = clean_nickname(nicknames.get(uid, ""))
        # Up to 5000 names fit safely in one Firestore document (1 MB limit).
        if nick and nick.lower() not in used and len(top) < 5000:
            used.add(nick.lower())
            top.append({"rank": rank, "nickname": nick, "points": key[0],
                        "correct": key[1], "events": num(sc, "events")})
        if sc.get("rank") != rank or sc.get("players") != len(ranked):
            batch.set(db.collection("scores").document(uid), {"rank": rank, "players": len(ranked)}, merge=True)
            count += 1
            if count >= 400:
                batch.commit()
                batch, count = db.batch(), 0
    # Monthly seasons: one public list per month (leaderboard/m_2026-11).
    season_keys = sorted({k for sc in scores.values() for k in (sc.get("months") or {})}, reverse=True)
    for key in season_keys:
        rows = [(uid, (sc.get("months") or {}).get(key) or {}) for uid, sc in scores.items()]
        rows = [(uid, m) for uid, m in rows if m]
        rows.sort(key=lambda r: (-num(r[1], "points"), -num(r[1], "correct")))
        mtop, mprev, mrank, mused = [], None, 0, set()
        for i, (uid, m) in enumerate(rows, start=1):
            k = (num(m, "points"), num(m, "correct"))
            if k != mprev:
                mrank, mprev = i, k
            nick = clean_nickname(nicknames.get(uid, ""))
            if nick and nick.lower() not in mused and len(mtop) < 5000:
                mused.add(nick.lower())
                mtop.append({"rank": mrank, "nickname": nick, "points": k[0], "correct": k[1],
                             "events": num(m, "events")})
        batch.set(db.collection("leaderboard").document(f"m_{key}"),
                  {"top": mtop, "players": len(rows), "month": key, "updated": datetime.now(timezone.utc)})
    batch.set(db.collection("leaderboard").document("current"),
              {"top": top, "players": len(ranked), "months": season_keys, "updated": datetime.now(timezone.utc)})
    batch.commit()
    print(f"Leaderboard: {len(top)} shown of {len(ranked)} players; seasons: {', '.join(season_keys) or 'none'}")


# ----------------------------------------------------------------- Firebase

def firestore_client():
    import firebase_admin
    from firebase_admin import credentials, firestore
    info = json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"])
    firebase_admin.initialize_app(credentials.Certificate(info))
    return firestore.client()


def search_terms(name, fights):
    """Lowercase words people might search for: event name and fighter names."""
    words = set()
    texts = [name] + [f.get(k, "") for f in fights for k in ("fighterA", "fighterB", "fighterAAr", "fighterBAr")]
    for text in texts:
        for w in re.split(r"[^\w]+", text.lower()):
            if len(w) >= 2:
                words.add(w)
    num = re.search(r"UFC (\d+)", name)
    if num:
        words.add("ufc" + num.group(1))
    return sorted(words)


def doc_id(name):
    return "wiki_" + re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:120]


def find_same_event(db, start, fights):
    """Events already saved within 36 hours of this one that share its fights.

    Wikipedia often renames an event (e.g. "UFC Fight Night 294" becomes
    "UFC Fight Night: Prochazka vs. Stirling"). The new name would create a
    second copy, so we find the copy we already have instead.
    """
    ids = {f.get("id") for f in fights if f.get("id")}
    if not ids or not start:
        return []
    lo, hi = start - timedelta(hours=36), start + timedelta(hours=36)
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter
        query = (db.collection("events")
                 .where(filter=FieldFilter("start", ">=", lo))
                 .where(filter=FieldFilter("start", "<=", hi)))
    except ImportError:
        query = db.collection("events").where("start", ">=", lo).where("start", "<=", hi)
    need = 1 if len(ids) < 3 else 2
    found = []
    for snap in query.stream():
        d = snap.to_dict() or {}
        if d.get("source") != "wikipedia":
            continue  # never touch events you made by hand
        shared = ids & {f.get("id") for f in d.get("fights") or []}
        if len(shared) >= need:
            found.append(snap)
    return found


def has_picks(db, event_id):
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter
        q = db.collection("picks").where(filter=FieldFilter("eventId", "==", event_id))
    except ImportError:
        q = db.collection("picks").where("eventId", "==", event_id)
    return any(True for _ in q.limit(1).stream())


def pick_event_ref(db, name, start, fights):
    """The document to save this event in, removing leftover duplicate copies.

    Keeps the copy that fans already made predictions in (so their picks still
    get scored), otherwise the one under the current name.
    """
    wanted = doc_id(name)
    same = find_same_event(db, start, fights) if fights else []
    if not same:
        return db.collection("events").document(wanted)
    with_picks = [s for s in same if has_picks(db, s.id)]
    keep = (with_picks[0] if with_picks
            else next((s for s in same if s.id == wanted), None) or same[0])
    for s in same:
        if s.id != keep.id and s not in with_picks:
            s.reference.delete()
            print(f"  removed duplicate copy {s.id}")
    if keep.id != wanted:
        print(f"  same event as {keep.id} (renamed on Wikipedia)")
    return keep.reference


# ---------------------------------------------------------------- BALLDONTLIE
# Paid live results (MMA ALL-STAR plan). Used ONLY to fill in results that
# Wikipedia hasn't posted yet, around fight night. Wikipedia stays the source
# for cards, Arabic names, Arab fighters, nationalities and legends.
# The key comes from the GitHub secret BALLDONTLIE_API_KEY. Without it, or if
# the service fails, the bot works exactly as before.
BDL_KEY = os.environ.get("BALLDONTLIE_API_KEY", "").strip()
BDL_BASE = "https://api.balldontlie.io/mma/v1"
_bdl_cache = {}
_bdl_event_ids = {}  # event day -> BALLDONTLIE UFC event ids (fetched once per run)
_bdl_live = set()  # our fight ids that BALLDONTLIE says are happening right now
_bdl_cancelled = set()  # our fight ids BALLDONTLIE marks cancelled/postponed (dropped during the night)
_bdl_round = {}  # our fight id -> round in progress, when BALLDONTLIE gives it
_bdl_logged = set()


def _bdl_get(path, params):
    r = requests.get(BDL_BASE + path, params=params, headers={"Authorization": BDL_KEY}, timeout=20)
    r.raise_for_status()
    return r.json()


def _bdl_all(path, params):
    out, cursor = [], None
    for _ in range(10):
        q = dict(params, per_page=100)
        if cursor:
            q["cursor"] = cursor
        data = _bdl_get(path, q)
        out += data.get("data") or []
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not cursor:
            break
    return out


def _bdl_pair(a, b):
    return "|".join(sorted([norm(a), norm(b)]))


def _bdl_last(name):
    parts = re.sub(r"[^a-z ]", "", (name or "").lower()).split()
    return parts[-1] if parts else ""


def _bdl_method(f):
    method = (f.get("result_method") or "").strip()
    detail = (f.get("result_method_detail") or "").strip()
    if detail and detail.lower() not in method.lower():
        method = f"{method} ({detail})" if method else detail
    parts = [method]
    if f.get("result_round"):
        parts.append(f"Round {f['result_round']}")
    if f.get("result_time"):
        parts.append(str(f["result_time"]))
    return " · ".join(p for p in parts if p)


def _bdl_current_round(bout):
    """The round in progress. BALLDONTLIE's exact field isn't documented for
    live fights, so try the usual names, then text like "Round 2" / "R2"."""
    for k in ("current_round", "status_period", "period", "round", "status_round"):
        try:
            n = int(bout.get(k) or 0)
        except (TypeError, ValueError):
            n = 0
        if 1 <= n <= 5:
            return n
    for k in ("status_detail", "status_description", "status_short_detail", "status"):
        m = re.search(r"\b(?:round|rd|r)\s*([1-5])\b", str(bout.get(k) or ""), re.I)
        if m:
            return int(m.group(1))
    return 0


def bdl_fill_results(start, fights):
    """Fill missing winners/methods from BALLDONTLIE, from 6 hours before the
    event until 36 hours after. Never overwrites a result that's already there."""
    if not BDL_KEY or not fights or MODE == "backfill":
        return fights
    now = datetime.now(timezone.utc)
    if not (start - timedelta(hours=6) <= now <= start + timedelta(hours=36)):
        return fights
    if all(f.get("winner") for f in fights):
        return fights
    try:
        cache_key = start.date().isoformat()
        if cache_key not in _bdl_cache:
            if cache_key not in _bdl_event_ids:
                ids = []
                for d in (start.date() - timedelta(days=1), start.date(), start.date() + timedelta(days=1)):
                    for e in _bdl_all("/events", {"date": d.isoformat()}):
                        league = (e.get("league") or {}).get("abbreviation", "")
                        if league and league.upper() != "UFC":
                            continue
                        ids.append(e["id"])
                _bdl_event_ids[cache_key] = ids
            bouts = []
            for eid in _bdl_event_ids[cache_key]:
                bouts += _bdl_all("/fights", {"event_ids[]": eid})
            _bdl_cache[cache_key] = bouts
        bouts = _bdl_cache[cache_key]
    except Exception as e:  # never let this stop the sync
        print(f"  BALLDONTLIE skipped: {e}")
        return fights

    by_pair, by_last = {}, {}
    for b in bouts:
        n1 = (b.get("fighter1") or {}).get("name", "")
        n2 = (b.get("fighter2") or {}).get("name", "")
        if n1 and n2:
            by_pair[_bdl_pair(n1, n2)] = b
            by_last["|".join(sorted([_bdl_last(n1), _bdl_last(n2)]))] = b

    filled = 0
    out = []
    for f in fights:
        f = dict(f)
        if not f.get("winner"):
            a, b_ = f.get("fighterA", ""), f.get("fighterB", "")
            bout = by_pair.get(_bdl_pair(a, b_)) or by_last.get("|".join(sorted([_bdl_last(a), _bdl_last(b_)])))
            status = " ".join(str(bout.get(k) or "") for k in ("status_state", "status", "status_detail",
                                                                "status_description")).lower() if bout else ""
            if bout and re.search(r"cancel|postpon|scratch|called off|removed", status):
                _bdl_cancelled.add(f.get("id"))
            if bout and (bout.get("status_state") or "").lower() in ("in", "live", "in_progress", "inprogress"):
                _bdl_live.add(f.get("id"))
                rnd = _bdl_current_round(bout)
                if rnd:
                    _bdl_round[f.get("id")] = rnd
                if "live" not in _bdl_logged:  # so we can see what BALLDONTLIE sends during a fight
                    _bdl_logged.add("live")
                    print("  BALLDONTLIE live fight fields:",
                          {k: v for k, v in bout.items() if not isinstance(v, (dict, list))})
            if bout and (bout.get("status_state") == "final" or bout.get("winner")):
                w = (bout.get("winner") or {}).get("name", "")
                method = _bdl_method(bout)
                low = method.lower()
                if w:
                    if norm(w) == norm(a) or _bdl_last(w) == _bdl_last(a):
                        f["winner"] = a
                    elif norm(w) == norm(b_) or _bdl_last(w) == _bdl_last(b_):
                        f["winner"] = b_
                    else:
                        f["winner"] = w
                elif "draw" in low:
                    f["winner"] = "Draw"
                elif "no contest" in low or "overturned" in low:
                    f["winner"] = "No contest"
                if f.get("winner"):
                    f["method"] = method
                    filled += 1
        out.append(f)
    if filled:
        print(f"  BALLDONTLIE filled {filled} result(s)")
    return out


LIVE_MINUTES = int(os.environ.get("LIVE_MINUTES", "50"))
LIVE_EVERY = 60  # seconds between BALLDONTLIE checks during a live event


def live_watch(active):
    """While an event is on, check BALLDONTLIE every minute for about
    LIVE_MINUTES, so walkout alarms, round notes and results go out within
    a minute or two. (GitHub's own timer runs 20-35 minutes apart, far too
    slow for that.) Only touches tonight's event, never Wikipedia."""
    if not BDL_KEY or not active:
        return
    print(f"Live watch: {len(active)} event(s), every {LIVE_EVERY}s for up to {LIVE_MINUTES} min")
    deadline = time.time() + LIVE_MINUTES * 60
    rounds = 0
    while active and time.time() + LIVE_EVERY < deadline:
        time.sleep(LIVE_EVERY)
        rounds += 1
        _bdl_cache.clear()
        _bdl_live.clear()
        _bdl_cancelled.clear()
        still = []
        for ref, start in active:
            try:
                doc = ref.get().to_dict() or {}
                fights = doc.get("fights") or []
                filled = bdl_fill_results(start, fights)
                if filled != fights:
                    ref.set({"fights": filled}, merge=True)
                    print(f"  {doc.get('name', ref.id)}: result saved")
                doc["fights"] = filled
                check_alerts(ref, doc)
                check_fight_alerts(ref, doc)
                if rounds % 3 == 0:  # official clips: every ~3 minutes
                    _clip_cache.clear()
                    try:
                        find_clips(ref, doc)
                    except Exception as e:
                        print(f"  clips failed: {type(e).__name__}")
                if (not all(f.get("winner") for f in filled)
                        and datetime.now(timezone.utc) <= start + timedelta(hours=9)):
                    still.append((ref, start))
            except Exception as e:  # keep watching the other events
                print(f"  live watch error: {e}")
                still.append((ref, start))
        active = still
    print("Live watch finished")


# ------------------------------------------------------------------- clips
# Exact highlight clip of each finished fight, from OFFICIAL UFC accounts only:
# - Instagram: Meta's official Business Discovery API (reads @ufc's latest
#   public posts with our own linked account; secret IG_PAGE_TOKEN).
# - YouTube: the official UFC channel's public uploads feed (no key needed).
# A clip is saved only on a strict name match, and never replaced once found.
# The app's "Watch the clip" button opens it in Instagram / YouTube.
IG_TOKEN = os.environ.get("IG_PAGE_TOKEN", "").strip()
IG_USER_ID = "17841422164804906"      # @historygoneweird23, linked to our Meta app
IG_TARGET = "ufc"
GRAPH = "https://graph.facebook.com/v26.0"
UFC_YT_CHANNEL = "UCvgfXK4nTYKudb0rFR6noLA"
CLIP_HOURS = 48                        # look for clips this long after an event starts
# Clips count from the start of the prelims (~6 hours before the main card time
# saved as the event start), never earlier: the day before, UFC posts old
# "Free Fight" videos of that weekend's fighters, which aren't this fight.
CLIP_EARLIEST = timedelta(hours=6)
_clip_cache = {}

# Words in YouTube titles that mean "not the fight itself".
_YT_SKIP = ("interview", "press", "conference", "faceoff", "face off", "weighin", "weigh in",
            "embedded", "preview", "prediction", "breakdown", "countdown", "reaction", "media day")


def _letters(text):
    import unicodedata
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", text.lower())


def _name_keys(name):
    """'Petr Yan' -> {'petryan'}; 'Alexander Volkanovski' -> {'alexandervolkanovski', 'volkanovski'}.
    Short last names (Yan, Lee) only match together with the first name."""
    words = [w for w in re.split(r"\s+", strip_title(name or "")) if w]
    keys = set()
    if len(words) >= 2:
        keys.add(_letters("".join(words)))
    if words and len(_letters(words[-1])) >= 5:
        keys.add(_letters(words[-1]))
    return {k for k in keys if k}


def _mentions(text_letters, name):
    return any(k in text_letters for k in _name_keys(name))


def _parse_time(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00").replace("+0000", "+00:00"))
    except Exception:
        return None


def _ig_recent():
    """@ufc's latest public posts as dicts (text, url, at, video)."""
    if "ig" in _clip_cache:
        return _clip_cache["ig"]
    out = []
    if IG_TOKEN:
        fields = ("business_discovery.username(%s){media.limit(40)"
                  "{caption,permalink,media_type,media_product_type,timestamp}}" % IG_TARGET)
        try:
            r = requests.get(f"{GRAPH}/{IG_USER_ID}", params={"fields": fields, "access_token": IG_TOKEN},
                             timeout=30)
            data = r.json()
            if r.status_code != 200:
                err = (data.get("error") or {}) if isinstance(data, dict) else {}
                print(f"  Instagram clips skipped: HTTP {r.status_code} {err.get('code', '')} {err.get('message', '')[:120]}")
            else:
                for m in ((data.get("business_discovery") or {}).get("media") or {}).get("data", []):
                    at = _parse_time(m.get("timestamp", ""))
                    url = m.get("permalink", "")
                    if at and url.startswith("https://www.instagram.com/"):
                        out.append({"text": _letters(m.get("caption", "")), "url": url, "at": at,
                                    "video": m.get("media_type") == "VIDEO" or m.get("media_product_type") == "REELS",
                                    "source": "instagram"})
        except Exception as e:  # never print the request (it holds the token)
            print(f"  Instagram clips skipped: {type(e).__name__}")
    _clip_cache["ig"] = out
    return out


def _yt_recent():
    """Latest uploads of the official UFC YouTube channel (Shorts included)."""
    if "yt" in _clip_cache:
        return _clip_cache["yt"]
    out = []
    try:
        r = requests.get("https://www.youtube.com/feeds/videos.xml",
                         params={"channel_id": UFC_YT_CHANNEL}, headers=HEADERS, timeout=30)
        r.raise_for_status()
        import xml.etree.ElementTree as ET
        ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
        root = ET.fromstring(r.content)
        author = root.findtext("a:author/a:name", default="", namespaces=ns).strip()
        if author != "UFC":
            print(f"  YouTube clips skipped: feed author is {author!r}, not the official UFC channel")
        else:
            for e in root.findall("a:entry", ns):
                title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
                vid = (e.findtext("yt:videoId", default="", namespaces=ns) or "").strip()
                at = _parse_time(e.findtext("a:published", default="", namespaces=ns) or "")
                low = title.lower()
                if vid and at and not any(w in low for w in _YT_SKIP):
                    out.append({"text": _letters(title), "url": f"https://www.youtube.com/watch?v={vid}",
                                "at": at, "video": True, "both_required": True, "source": "youtube"})
    except Exception as e:
        print(f"  YouTube clips skipped: {type(e).__name__}")
    _clip_cache["yt"] = out
    return out


# Posts from before or around the fight, not the fight itself: arrivals,
# walkouts, faceoffs, interviews... (matched on the letters-only text).
_CLIP_NOT_FIGHT = ("arriv", "walkout", "walksout", "walkingout", "warmup", "warmingup", "backstage",
                   "lockerroom", "faceoff", "staredown", "weighin", "weighs", "mediaday", "pressconference",
                   "presser", "countdown", "embedded", "preview", "fightweek", "workout", "whowins",
                   "whowillwin", "whosgot", "prediction", "predict", "makeyourpick", "yourpicks", "isofficial",
                   "booked", "announce", "comingup", "upnext", "livenow", "getready", "buildup", "trailer",
                   "interview", "postfight", "prefight", "reaction", "reacts", "callsout", "callout",
                   "behindthescenes", "entrance", "hotel", "airport", "landed", "fanfest", "autograph",
                   "photoshoot", "mediascrum", "scrum", "ceremonial", "openworkout", "sparring", "training")
# Words that show the post is about the fight's action or result.
_CLIP_FIGHT = ("knockout", "knocks", "knocked", "tko", "submission", "submits", "submitted", "finish",
               "highlight", "decision", "defeat", "beats", "victory", "andstill", "andnew", "newchamp",
               "retains", "wins", "thewin", "fullfight", "freefight", "nocaut", "finaliza", "mejoresmomentos",
               "melhoresmomentos", "peleacompleta", "lutacompleta", "flatline", "slept", "lightsout", "choke",
               "armbar", "stoppage", "stops", "putsaway", "dropped", "drops", "elbows", "spinning")


def _clip_text_ok(letters, platform):
    """False for arrivals, walkouts, interviews... Instagram posts must also
    show the fight itself (a finish/result word): UFC posts many videos of
    both fighters on fight day that aren't the fight."""
    if any(w in letters for w in _CLIP_NOT_FIGHT):
        return False
    return platform != "instagram" or any(w in letters for w in _CLIP_FIGHT)


def _best_clip(fight, start, posts, until):
    """The post that is clearly about this fight, or None.
    Both fighters named = match. On Instagram, a Reel naming only the winner
    also matches (UFC often captions finish clips with just the winner).
    Instagram must be a video, and never arrivals/walkouts/interviews."""
    best = None
    for p in posts:
        if not (start - CLIP_EARLIEST <= p["at"] <= until):
            continue
        if p["source"] == "instagram" and not p["video"]:
            continue
        if not _clip_text_ok(p["text"], p["source"]):
            continue
        a, b = _mentions(p["text"], fight.get("fighterA", "")), _mentions(p["text"], fight.get("fighterB", ""))
        winner = fight.get("winner", "")
        if a and b:
            score = 3
        elif not p.get("both_required") and winner and (
                (a and winner == fight.get("fighterA")) or (b and winner == fight.get("fighterB"))):
            score = 1
        else:
            continue
        # Prefer the stronger match, then the earliest post (the finish clip).
        key = (-score, p["at"])
        if best is None or key < best[0]:
            best = (key, p)
    return best[1] if best else None


def _clip_list(f):
    """Every clip saved on a fight: 'clips' (one per platform), or the old single 'clip'."""
    return list(f.get("clips") or ([f["clip"]] if f.get("clip") else []))


CLIPS_MAX = {"youtube": 3, "instagram": 2}  # sources kept per fight, so fans can pick


def _clip_rank(c):
    """YouTube first, then the official UFC accounts, then others."""
    return (c.get("source") != "youtube", c.get("channel") not in ("UFC", "@ufc"))


def _merge_clips(f, new):
    """Adds new official clips to a fight (or last-5 row): different channels
    only, at most CLIPS_MAX per platform. 'clip' = the preferred one (what
    older app versions open). True if something changed."""
    clips = _clip_list(f)
    urls = {c.get("url") for c in clips}
    changed = False
    for c in new:
        same = [x for x in clips if x.get("source") == c.get("source")]
        if (c.get("url") in urls or len(same) >= CLIPS_MAX.get(c.get("source"), 1)
                or c.get("channel") in {x.get("channel") for x in same if x.get("channel")}):
            continue
        clips.append(c)
        urls.add(c.get("url"))
        changed = True
    if changed or (clips and f.get("clip") != sorted(clips, key=_clip_rank)[0]):
        clips.sort(key=_clip_rank)
        f["clips"], f["clip"] = clips, clips[0]
        return True
    return False


def _add_clip(f, clip):
    return _merge_clips(f, [clip])


def find_clips(ref, doc, hours=CLIP_HOURS):
    """Saves the official clip on each finished fight of this event that has none yet."""
    if MODE == "backfill":
        return
    start = doc.get("start")
    fights = doc.get("fights") or []
    now = datetime.now(timezone.utc)
    if not start or not fights or not (start - timedelta(hours=1) <= now <= start + timedelta(hours=hours)):
        return
    todo = [f for f in fights if f.get("winner") and len(_clip_list(f)) < 2]
    if not todo:
        return
    posts = _ig_recent() + _yt_recent()
    if not posts:
        return
    until = start + timedelta(hours=hours)
    changed = False
    out = []
    for f in fights:
        f = dict(f)
        if f.get("winner"):
            for platform in ("instagram", "youtube"):
                if platform in {c.get("source") for c in _clip_list(f)}:
                    continue
                p = _best_clip(f, start, [x for x in posts if x["source"] == platform], until)
                if p and _add_clip(f, {"url": p["url"], "source": p["source"], "at": p["at"],
                                       "channel": "@ufc" if platform == "instagram" else "UFC"}):
                    changed = True
                    print(f"  clip found ({p['source']}): {f.get('fighterA')} vs {f.get('fighterB')}")
        out.append(f)
    if changed:
        ref.set({"fights": out}, merge=True)


def run_clips_now(db, days=7):
    """SYNC_MODE=clips: look for clips of every event of the last few days now."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    snaps = list(db.collection("events").where("start", ">=", since).stream())
    print(f"Clips: checking {len(snaps)} recent event(s)")
    for snap in snaps:
        doc = snap.to_dict() or {}
        print(f"- {doc.get('name', snap.id)}")
        find_clips(snap.reference, doc, hours=days * 24)
    ig, yt = _clip_cache.get("ig", []), _clip_cache.get("yt", [])
    print(f"Clips: read {len(ig)} Instagram post(s) and {len(yt)} YouTube video(s)")


# ---------------------------------------------------- YouTube clips (official)
# Clips for past fights too: an index of video titles from OFFICIAL channels
# only, read with the YouTube Data API (secret YOUTUBE_API_KEY, 10,000 free
# units a day). Each channel is checked through the API (exact channel name +
# a big subscriber count) before it's used; one that doesn't pass is skipped.
# Titles must name BOTH fighters. Never print the key: logs are public.
YT_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
YT_API = "https://www.googleapis.com/youtube/v3"
YT_MIN_SUBS = 200_000
# (channel id or @handle, exact channel names allowed, only titles with "UFC")
YT_CHANNELS = [
    (UFC_YT_CHANNEL, ("UFC",), False),
    ("@UFCEspanol", ("UFC Español", "UFC Espanol", "ufcespanol"), False),
    ("@UFCBrasil", ("UFC Brasil",), False),
    ("@UFCFightPass", ("UFC FIGHT PASS", "UFC Fight Pass"), False),
    ("@espnmma", ("ESPN MMA",), True),
    ("@TNTSports", ("TNT Sports",), True),
    ("@CBSSports", ("CBS Sports",), True),
    # Official UFC broadcasters (only their videos with "UFC" in the title).
    ("@paramountplus", ("Paramount+", "Paramount Plus"), True),
    ("@espn", ("ESPN",), True),
    ("@canalcombate", ("Combate", "Canal Combate"), True),
    ("@tsn", ("TSN",), True),
]
YT_CHUNK = 4000  # titles per Firestore document (a document holds at most 1 MB)
_YT_SKIP_MORE = ("top ", "best of", "every ", "compilation", "recap", "full event", "full card", "odds",
                 "betting", "picks", "podcast", "vlog", "ranking", "entrevista", "conferencia", "previa",
                 "pesaje", "cara a cara", "coletiva", "pesagem", "encarada", "predicciones", "palpites",
                 "bastidores", "trailer", "promo", "live stream", "watch party", "walkout", "walk out",
                 "entrance", "backstage", "behind the scenes", "fight week", "workout", "calls out", "callout")
# Finish/highlight words: with one of these, a title naming only the winner
# counts too (UFC often titles finish clips "Jean Silva Brutal Finish").
_YT_FINISH = ("knockout", "knocks out", "knocked out", " ko", "tko", "submission", "submits", "finish",
              "highlight", "free fight", "full fight", "nocaut", "finaliza", "mejores momentos",
              "melhores momentos", "pelea completa", "luta completa")
_YT_GOOD = (("fullfight", 4), ("freefight", 4), ("peleacompleta", 4), ("lutacompleta", 4),
            ("highlight", 3), ("mejoresmomentos", 3), ("melhoresmomentos", 3),
            ("knockout", 2), ("ko", 1), ("submission", 2), ("finish", 2), ("nocaut", 2),
            ("finalizacion", 2), ("finalizacao", 2))


class _YTQuota(Exception):
    pass


def _yt_get(path, params):
    r = requests.get(f"{YT_API}/{path}", params={**params, "key": YT_KEY}, timeout=30)
    if r.status_code != 200:
        reason = ""
        try:
            reason = ((r.json().get("error") or {}).get("errors") or [{}])[0].get("reason", "")
        except Exception:
            pass
        if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
            raise _YTQuota(reason)
        raise RuntimeError(f"YouTube API HTTP {r.status_code} {reason}")  # no URL: it holds the key
    return r.json()


def _yt_channel_rows(ids):
    out = []
    for i in range(0, len(ids), 50):
        out += _yt_get("channels", {"part": "snippet,contentDetails,statistics",
                                     "id": ",".join(ids[i:i + 50])}).get("items") or []
    return out


def _yt_uploads(c):
    return ((c.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads", "")


def _yt_subs(c):
    stats = c.get("statistics") or {}
    return 0 if stats.get("hiddenSubscriberCount") else int(stats.get("subscriberCount") or 0)


def yt_official_channels():
    """[(channel id, title, ufc_only, uploads playlist)] that pass the check:
    our list (exact name + big subscriber count), plus every channel the
    official UFC channel itself features on its page."""
    out = []
    for ref, names, ufc_only in YT_CHANNELS:
        try:
            q = {"forHandle": ref} if ref.startswith("@") else {"id": ref}
            items = _yt_get("channels", {"part": "snippet,contentDetails,statistics", **q}).get("items") or []
            if not items:
                print(f"  YouTube channel {ref}: not found, skipped")
                continue
            c = items[0]
            title, subs, uploads = (c.get("snippet") or {}).get("title", ""), _yt_subs(c), _yt_uploads(c)
            if title.lower() not in {n.lower() for n in names} or subs < YT_MIN_SUBS or not uploads:
                print(f"  YouTube channel {ref}: NOT verified ({title!r}, {subs} subscribers), skipped")
                continue
            print(f"  YouTube channel verified: {title} ({ref} = {c['id']}, {subs:,} subscribers)")
            out.append((c["id"], title, ufc_only, uploads))
        except _YTQuota:
            raise
        except Exception as e:
            print(f"  YouTube channel {ref}: check failed ({type(e).__name__})")
    try:
        featured = []
        for sec in _yt_get("channelSections", {"part": "contentDetails",
                                               "channelId": UFC_YT_CHANNEL}).get("items") or []:
            featured += (sec.get("contentDetails") or {}).get("channels") or []
        have = {o[0] for o in out}
        for c in _yt_channel_rows([f for f in dict.fromkeys(featured) if f not in have]):
            title, subs, uploads = (c.get("snippet") or {}).get("title", ""), _yt_subs(c), _yt_uploads(c)
            if subs >= 50_000 and uploads:
                print(f"  YouTube channel featured by UFC: {title} ({c['id']}, {subs:,} subscribers)")
                out.append((c["id"], title, False, uploads))
            else:
                print(f"  YouTube channel featured by UFC but small, skipped: {title} ({subs} subscribers)")
        if not featured:
            print("  UFC's channel page features no other channels")
    except _YTQuota:
        raise
    except Exception as e:
        print(f"  UFC featured channels: check failed ({type(e).__name__})")
    return out


def _yt_keep(title, ufc_only):
    low = title.lower()
    if not title or low in ("private video", "deleted video"):
        return False
    if any(w in low for w in _YT_SKIP + _YT_SKIP_MORE):
        return False
    if ufc_only and "ufc" not in low:
        return False
    return " vs" in low or " v " in low or any(w in low for w in _YT_FINISH)


def yt_load_index(db, cid, coll="ytIndex"):
    meta = db.collection(coll).document(cid).get()
    meta = meta.to_dict() if meta.exists else {}
    rows = []
    for n in range(int(meta.get("chunks") or 0)):
        snap = db.collection(coll).document(f"{cid}_{n}").get()
        rows += (snap.to_dict() or {}).get("v", []) if snap.exists else []
    return meta, rows


def yt_save_index(db, cid, meta, rows, coll="ytIndex", size=YT_CHUNK):
    old_chunks = int(meta.get("chunks") or 0)
    chunks = [rows[i:i + size] for i in range(0, len(rows), size)] or [[]]
    for n, part in enumerate(chunks):
        db.collection(coll).document(f"{cid}_{n}").set({"v": part})
    for n in range(len(chunks), old_chunks):
        db.collection(coll).document(f"{cid}_{n}").delete()
    db.collection(coll).document(cid).set({**meta, "chunks": len(chunks), "count": len(rows)})


def yt_update_index(db, full=False):
    """Reads new uploads of each official channel (1 unit per 50 videos) and
    returns the whole index as [(video id, published, title letters, source)].
    The first run reads every upload; later runs stop at known videos."""
    if not YT_KEY or db is None:
        return []
    index = []
    try:
        channels = yt_official_channels()
    except _YTQuota:
        print("  YouTube quota used up for today: index not updated")
        channels = []
    for cid, title, ufc_only, uploads in channels:
        meta, rows = yt_load_index(db, cid)
        known = {r.split("|", 2)[0] for r in rows}
        complete = bool(meta.get("complete"))
        token = None if complete else meta.get("resume")
        newest = int(meta.get("newest") or 0)
        top = newest
        new_rows, pages = [], 0
        try:
            while True:
                q = {"part": "snippet,contentDetails", "playlistId": uploads, "maxResults": 50}
                if token:
                    q["pageToken"] = token
                data = _yt_get("playlistItems", q)
                pages += 1
                oldest = None
                for it in data.get("items") or []:
                    sn, cd = it.get("snippet") or {}, it.get("contentDetails") or {}
                    vid = cd.get("videoId") or (sn.get("resourceId") or {}).get("videoId", "")
                    added = _parse_time(sn.get("publishedAt") or "")
                    if added:
                        ts = int(added.timestamp())
                        oldest = ts if oldest is None else min(oldest, ts)
                        top = max(top, ts)
                    if not vid or vid in known:
                        continue
                    known.add(vid)
                    t = sn.get("title", "")
                    at = _parse_time(cd.get("videoPublishedAt") or sn.get("publishedAt") or "")
                    if at and _yt_keep(t, ufc_only):
                        new_rows.append(f"{vid}|{int(at.timestamp())}|{_letters(t)[:160]}")
                token = data.get("nextPageToken")
                if not token:
                    complete = True
                    break
                # Uploads come newest first: stop once we're back at what we had
                # (2 days of overlap in case a video was added late).
                if meta.get("complete") and not full and oldest is not None and oldest < newest - 2 * 86400:
                    break
        except _YTQuota:
            print(f"  YouTube quota used up during {title}: saved progress, continues next run")
        except Exception as e:
            print(f"  YouTube index {title} stopped: {type(e).__name__}")
        if new_rows or complete != bool(meta.get("complete")) or token != meta.get("resume"):
            rows = new_rows + rows
            meta.update({"title": title, "complete": complete, "newest": top if complete else newest})
            if complete:
                meta.pop("resume", None)
            else:
                meta["resume"] = token
            yt_save_index(db, cid, meta, rows)
        print(f"  YouTube index {title}: {len(rows)} fight videos ({len(new_rows)} new, {pages} page(s) read)")
        for r in rows:
            vid, ts, letters = r.split("|", 2)
            index.append({"url": f"https://www.youtube.com/watch?v={vid}", "platform": "youtube",
                          "at": datetime.fromtimestamp(int(ts), timezone.utc), "letters": letters,
                          "label": title, "video": True})
    return index


def _yt_score(letters):
    return max([w for k, w in _YT_GOOD if k in letters] or [1])


def _pair_key(a, b):
    return "|".join(sorted([norm(a), norm(b)]))


def clip_candidates(a, b, when, until, index, winner="", platform="youtube"):
    """Official videos of fighter a vs fighter b on one platform, best first,
    posted between 'when' (fight day) and 'until' (the next time they met).
    Both names = match. A post naming only the winner also counts when it came
    out within 4 days (YouTube: with a finish word; Instagram: a video/Reel).
    Instagram posts must be videos (fight graphics are photos)."""
    out = []
    lo = when - CLIP_EARLIEST
    soon = when + timedelta(days=4)
    ka, kb = _name_keys(a), _name_keys(b)
    wa, wb = bool(winner) and norm(winner) == norm(a), bool(winner) and norm(winner) == norm(b)
    for e in index:
        if e["platform"] != platform or not (lo <= e["at"] < until):
            continue
        if platform == "instagram" and not e["video"]:
            continue
        letters = e["letters"]
        ma, mb = any(k in letters for k in ka), any(k in letters for k in kb)
        if not (ma or mb) or not _clip_text_ok(letters, platform):
            continue
        score = _yt_score(letters)
        if ma and mb:
            tier = 0
        elif e["at"] <= soon and (platform == "instagram" or score >= 2) and ((ma and wa) or (mb and wb)):
            tier = 1
        else:
            continue
        out.append(((tier, -score, 0 if e["label"] in ("UFC", "@ufc") else 1, e["at"]), e))
    out.sort(key=lambda x: x[0])
    return [e for _, e in out]


def _clip_from(e):
    return {"url": e["url"], "source": e["platform"], "at": e["at"], "channel": e["label"]}


def clip_match(a, b, when, until, index, winner="", platform="youtube"):
    c = clip_candidates(a, b, when, until, index, winner, platform)
    return _clip_from(c[0]) if c else None


def yt_match(a, b, when, until, index, winner=""):
    return clip_match(a, b, when, until, index, winner, "youtube")


# ------------------------------------------------ Instagram index (official)
# The same Meta connection as the live clips reads more OFFICIAL accounts and
# goes back in time, so past fights get Reels too. An account is used only if
# it exists with a big following; the log lists them so the owner can confirm
# each has Instagram's blue tick.
IG_ACCOUNTS = ("ufc", "ufc_brasil", "ufcespanol", "ufceurope", "ufcindia", "ufcfightpass", "espnmma")
IG_MIN_FOLLOWERS = 300_000
IG_PAGES_PER_RUN = 40  # 50 posts per page, per account
IG_CALLS_PER_RUN = 120  # Meta allows ~200 calls an hour; leave room for live clips
IG_CHUNK = 1500


class _IGLimit(Exception):
    pass


def _ig_page(username, after=None):
    media = "media" + (f".after({after})" if after else "") + ".limit(50)"
    fields = ("business_discovery.username(%s){username,name,followers_count,%s"
              "{caption,permalink,media_type,media_product_type,timestamp}}" % (username, media))
    r = requests.get(f"{GRAPH}/{IG_USER_ID}", params={"fields": fields, "access_token": IG_TOKEN}, timeout=30)
    data = r.json() if r.content else {}
    if r.status_code != 200:
        err = (data.get("error") or {}) if isinstance(data, dict) else {}
        if err.get("code") in (4, 17, 32, 613) or err.get("error_subcode") == 2207051:
            raise _IGLimit()
        raise RuntimeError(f"HTTP {r.status_code} {err.get('code', '')}")  # never the URL: it holds the token
    return data.get("business_discovery") or {}


def ig_update_index(db):
    """Reads the official accounts' posts, newest first, up to IG_PAGES_PER_RUN
    pages each per run, until the whole history is in (then only new posts)."""
    if not IG_TOKEN or db is None:
        return []
    index = []
    limited = False
    calls = 0
    for username in IG_ACCOUNTS:
        meta, rows = yt_load_index(db, username, "igIndex")
        if not limited:
            complete = bool(meta.get("complete"))
            after = None if complete else meta.get("resume")
            newest = int(meta.get("newest") or 0)
            top, new_rows, pages = newest, [], 0
            known = {r.split("|", 1)[0] for r in rows}
            try:
                while pages < IG_PAGES_PER_RUN:
                    if calls >= IG_CALLS_PER_RUN:
                        limited = True
                        print("  Instagram: enough reading for this run, continues next run")
                        break
                    bd = _ig_page(username, after)
                    pages += 1
                    calls += 1
                    if pages == 1 and not meta.get("verified"):
                        followers, name = int(bd.get("followers_count") or 0), bd.get("name", "")
                        if followers < IG_MIN_FOLLOWERS:
                            print(f"  Instagram @{username}: NOT verified ({name!r}, {followers} followers), skipped")
                            break
                        meta.update({"verified": True, "name": name, "followers": followers})
                        print(f"  Instagram account verified: @{username} ({name}, {followers:,} followers)")
                    media = bd.get("media") or {}
                    oldest = None
                    for m in media.get("data") or []:
                        at = _parse_time(m.get("timestamp", ""))
                        url = m.get("permalink", "")
                        if not at or not url.startswith("https://www.instagram.com/"):
                            continue
                        ts = int(at.timestamp())
                        oldest = ts if oldest is None else min(oldest, ts)
                        top = max(top, ts)
                        if url in known:
                            continue
                        known.add(url)
                        video = m.get("media_type") == "VIDEO" or m.get("media_product_type") == "REELS"
                        letters = _letters(m.get("caption", ""))[:400]
                        if video and letters:
                            new_rows.append(f"{url}|{ts}|1|{letters}")
                    after = ((media.get("paging") or {}).get("cursors") or {}).get("after")
                    if not after or not (media.get("paging") or {}).get("next"):
                        complete = True
                        break
                    if meta.get("complete") and oldest is not None and oldest < newest - 2 * 86400:
                        break  # caught up with what we already have
            except _IGLimit:
                print("  Instagram hourly limit reached: continues next run")
                limited = True
            except Exception as e:
                print(f"  Instagram @{username} stopped: {type(e).__name__} {e}")
            if meta.get("verified") and (new_rows or complete != bool(meta.get("complete"))
                                         or after != meta.get("resume")):
                rows = new_rows + rows
                meta.update({"complete": complete, "newest": top if complete else newest})
                if complete:
                    meta.pop("resume", None)
                else:
                    meta["resume"] = after
                yt_save_index(db, username, meta, rows, "igIndex", IG_CHUNK)
            if meta.get("verified"):
                print(f"  Instagram index @{username}: {len(rows)} videos ({len(new_rows)} new, {pages} page(s) read"
                      + ("" if complete else ", more to read next run") + ")")
        if meta.get("verified"):
            for r in rows:
                url, ts, video, letters = r.split("|", 3)
                index.append({"url": url, "platform": "instagram", "at": datetime.fromtimestamp(int(ts), timezone.utc),
                              "letters": letters, "label": f"@{username}", "video": video == "1"})
    return index


def _meetings(events):
    """Pair of fighters -> sorted dates they fought (for rematches)."""
    met = {}
    for ev in events:
        start = ev.get("start")
        for f in ev.get("fights") or []:
            if start and f.get("winner"):
                met.setdefault(_pair_key(f.get("fighterA", ""), f.get("fighterB", "")), []).append(start)
    return {k: sorted(v) for k, v in met.items()}


def _until(met, a, b, when):
    later = [d for d in met.get(_pair_key(a, b), []) if d > when + timedelta(days=2)]
    return (min(later) - timedelta(days=1)) if later else datetime.now(timezone.utc) + timedelta(days=1)


# Viewers are mostly in Arab countries: skip videos YouTube blocks there
# (regional UFC channels often allow only their own country).
YT_MUST_PLAY_IN = {"MA", "SA", "EG", "AE", "DZ", "IQ", "JO", "KW", "QA", "TN", "LB", "OM", "BH", "LY"}


class YTRegion:
    """Which videos play in our viewers' countries, cached in meta/ytRegion."""

    def __init__(self, db):
        self.db = db
        snap = db.collection("meta").document("ytRegion").get()
        self.ok = dict((snap.to_dict() or {}).get("ok") or {}) if snap.exists else {}
        self.dirty = False

    def check(self, vids):
        todo = [v for v in dict.fromkeys(vids) if v not in self.ok]
        if not YT_KEY:
            return
        try:
            for i in range(0, len(todo), 50):
                part = todo[i:i + 50]
                found = {}
                for it in _yt_get("videos", {"part": "contentDetails,status", "id": ",".join(part)}).get("items") or []:
                    rr = (it.get("contentDetails") or {}).get("regionRestriction") or {}
                    public = (it.get("status") or {}).get("privacyStatus") == "public"
                    allowed, blocked = set(rr.get("allowed") or []), set(rr.get("blocked") or [])
                    found[it["id"]] = public and (not allowed or YT_MUST_PLAY_IN <= allowed) \
                        and not (blocked & YT_MUST_PLAY_IN)
                for v in part:
                    self.ok[v] = found.get(v, False)  # missing = deleted or private
                self.dirty = True
        except Exception as e:
            print(f"  YouTube region check stopped: {type(e).__name__}")

    def plays(self, url):
        vid = url.rsplit("v=", 1)[-1]
        return self.ok.get(vid, True)  # unknown (not checked yet) = allowed

    def save(self):
        if self.dirty:
            self.db.collection("meta").document("ytRegion").set({"ok": self.ok})
            blocked = sum(1 for v in self.ok.values() if not v)
            print(f"  YouTube region check: {len(self.ok)} videos checked, {blocked} don't play in Arab countries")


def _vid(url):
    return url.rsplit("v=", 1)[-1]


def _pick(cands, platform, region):
    """Up to CLIPS_MAX clips from different channels, best first."""
    out, seen = [], set()
    for e in cands:
        if e["label"] in seen or (platform == "youtube" and not region.plays(e["url"])):
            continue
        seen.add(e["label"])
        out.append(_clip_from(e))
        if len(out) >= CLIPS_MAX[platform]:
            break
    return out


def yt_clips_for_events(db, index, region=None, days=None):
    """Adds official clips (several sources when there are) to every finished
    fight; days=None means all events, past ones too."""
    if not index or db is None:
        return
    region = region or YTRegion(db)
    now = datetime.now(timezone.utc)
    events = [(snap, snap.to_dict() or {}) for snap in db.collection("events").stream()]
    met = _meetings([d for _, d in events])
    # First the candidates, then one YouTube call per 50 videos for the region check.
    plan, vids = [], []
    for snap, doc in events:
        start = doc.get("start")
        if not start or start > now or (days and start < now - timedelta(days=days)):
            continue
        for k, f in enumerate(doc.get("fights") or []):
            if f.get("winner"):
                a, b = f.get("fighterA", ""), f.get("fighterB", "")
                until = _until(met, a, b, start)
                cands = {p: clip_candidates(a, b, start, until, index, f.get("winner", ""), p)
                         for p in ("youtube", "instagram")}
                vids += [_vid(e["url"]) for e in cands["youtube"][:6]]
                plan.append((snap.id, k, cands))
    region.check(vids)
    by_event = {}
    for eid, k, cands in plan:
        by_event.setdefault(eid, []).append((k, cands))
    found = have = total = multi = 0
    report = []
    for snap, doc in events:
        fights = [dict(f) for f in doc.get("fights") or []]
        changed = False
        for k, cands in by_event.get(snap.id, []):
            f = fights[k]
            new = _pick(cands["youtube"], "youtube", region) + _pick(cands["instagram"], "instagram", region)
            before = len(_clip_list(f))
            if _merge_clips(f, new):
                changed = True
                found += max(0, len(_clip_list(f)) - before)
        if changed:
            snap.reference.set({"fights": fights}, merge=True)
        if snap.id in by_event:
            done = [f for f in fights if f.get("winner")]
            ev_have = sum(1 for f in done if f.get("clip"))
            multi += sum(1 for f in done if len(_clip_list(f)) > 1)
            have, total = have + ev_have, total + len(done)
            report.append((doc["start"], doc.get("name", snap.id), ev_have, len(done)))
    report.sort(reverse=True)
    for start, name, h, t in report[:12]:
        print(f"  {name}: {h}/{t} fights have a clip")
    print(f"Official clips: {found} new. Finished fights checked: {have}/{total} have a clip"
          + (f" ({100 * have // total}%)" if total else "") + f", {multi} with a choice of sources")


def yt_clips_for_fighters(db, index, region=None):
    """Adds clips to each row of a fighter's last 5 fights: the event's clips
    when we have that fight, plus matches from the index."""
    if db is None:
        return
    region = region or YTRegion(db)
    clips = {}
    evs = [s.to_dict() or {} for s in db.collection("events").stream()]
    met = _meetings(evs)
    for ev in evs:
        for f in ev.get("fights") or []:
            if f.get("clip") and ev.get("start"):
                clips[(_pair_key(f.get("fighterA", ""), f.get("fighterB", "")), ev["start"].date())] = _clip_list(f)
    docs = [(snap, snap.to_dict() or {}) for snap in db.collection("fighters").stream()]
    plan, vids = {}, []
    for snap, d in docs:
        for k, r in enumerate(d.get("recent") or []):
            when = r.get("date")
            if not when or not index:
                continue
            me, opp = d.get("name", ""), r.get("opponent", "")
            won = me if r.get("outcome") == "W" else opp if r.get("outcome") == "L" else ""
            cands = {p: clip_candidates(me, opp, when, _until(met, me, opp, when), index, won, p)
                     for p in ("youtube", "instagram")}
            vids += [_vid(e["url"]) for e in cands["youtube"][:6]]
            plan[(snap.id, k)] = cands
    region.check(vids)
    batch, count, rows_with, rows_all = db.batch(), 0, 0, 0
    for snap, d in docs:
        recent, changed = [dict(r) for r in d.get("recent") or []], False
        for k, r in enumerate(recent):
            when = r.get("date")
            if when:
                pk = _pair_key(d.get("name", ""), r.get("opponent", ""))
                new = next((c for (key, day), c in clips.items()
                            if key == pk and abs((day - when.date()).days) <= 2), [])
                cands = plan.get((snap.id, k))
                if cands:
                    new = new + _pick(cands["youtube"], "youtube", region) + _pick(cands["instagram"], "instagram", region)
                changed |= _merge_clips(r, new)
            rows_all += 1
            rows_with += 1 if r.get("clip") else 0
        if changed:
            batch.set(snap.reference, {"recent": recent}, merge=True)
            count += 1
            if count % 400 == 0:
                batch.commit()
                batch = db.batch()
    batch.commit()
    print(f"Fighter last-5 clips: {count} fighter(s) updated. {rows_with}/{rows_all} rows have a clip"
          + (f" ({100 * rows_with // rows_all}%)" if rows_all else ""))


def _purge_rows(rows, is_bad, when=None):
    """Removes the clips is_bad(clip) rejects from fights / last-5 rows (and,
    with 'when', clips posted before that card started). True if changed."""
    changed = False
    for r in rows:
        start = when or r.get("date")
        clips = _clip_list(r)
        keep = [c for c in clips if not is_bad(c) and not (
            isinstance(start, datetime) and isinstance(c.get("at"), datetime) and c["at"] < start - CLIP_EARLIEST)]
        if len(keep) != len(clips):
            changed = True
            r.pop("clip", None)
            r.pop("clips", None)
            if keep:
                keep.sort(key=_clip_rank)
                r["clip"], r["clips"] = keep[0], keep
    return changed


def purge_bad_clips(db, index, region=None):
    """Rechecks every saved clip against the current rules (via its text in the
    index) and removes the ones that aren't the fight (arrivals, walkouts...)
    or that YouTube blocks in our viewers' countries."""
    text = {e["url"]: (e["letters"], e["platform"], e["video"]) for e in index}
    bad = {u for u, (letters, platform, video) in text.items()
           if not _clip_text_ok(letters, platform) or (platform == "instagram" and not video)}
    # The Instagram index keeps only videos: an Instagram clip from a time the
    # index covers but that isn't in it was a photo (or was deleted).
    ig_from = min((e["at"] for e in index if e["platform"] == "instagram"), default=None)

    events = [(snap, snap.to_dict() or {}) for snap in db.collection("events").stream()]
    fighters = [(snap, snap.to_dict() or {}) for snap in db.collection("fighters").stream()]
    if region is not None:
        region.check([_vid(c.get("url", "")) for _, d in events for f in d.get("fights") or [] for c in _clip_list(f)
                      if c.get("source") == "youtube"]
                     + [_vid(c.get("url", "")) for _, d in fighters for r in d.get("recent") or []
                        for c in _clip_list(r) if c.get("source") == "youtube"])

    def is_bad(c):
        url = c.get("url", "")
        if url in bad:
            return True
        if region is not None and c.get("source") == "youtube" and not region.plays(url):
            return True
        at = c.get("at")
        return bool(c.get("source") == "instagram" and url not in text and ig_from and isinstance(at, datetime)
                    and at >= ig_from)

    removed = 0
    for snap, d in events:
        fights = [dict(f) for f in d.get("fights") or []]
        before = sum(len(_clip_list(f)) for f in fights)
        if _purge_rows(fights, is_bad, d.get("start")):
            snap.reference.set({"fights": fights}, merge=True)
            removed += before - sum(len(_clip_list(f)) for f in fights)
    for snap, d in fighters:
        recent = [dict(r) for r in d.get("recent") or []]
        if _purge_rows(recent, is_bad):
            snap.reference.set({"recent": recent}, merge=True)
    print(f"Clips recheck: removed {removed} clip(s) (not the fight itself, posted before the card, or blocked in Arab countries)")


def yt_clips(db, full=False, fighters=False):
    """Update the YouTube + Instagram indexes, then fill clips on events
    (and fighters' last 5)."""
    index = []
    if YT_KEY:
        index += yt_update_index(db, full=full)
    else:
        print("YouTube clips: no YOUTUBE_API_KEY yet, skipped")
    try:
        index += ig_update_index(db)
    except Exception as e:
        print(f"Instagram index failed: {type(e).__name__}")
    yt = sum(1 for e in index if e["platform"] == "youtube")
    print(f"Official clips: {yt} YouTube and {len(index) - yt} Instagram videos in the index")
    if not index:
        return
    region = YTRegion(db)
    every = full or fighters  # the backfill and the twice-a-day run: all events + recheck
    if every:
        try:
            purge_bad_clips(db, index, region)
        except Exception as e:
            print(f"Clips recheck failed: {type(e).__name__}")
    yt_clips_for_events(db, index, region, days=None if every else 45)
    if fighters:
        yt_clips_for_fighters(db, index, region)
    region.save()


# ------------------------------------------------------------ news covers
# Finds the fighters a news post is about (names in its title or summary, in
# Arabic or English) and saves them with their free photo on the post, so the
# app can show a fighters cover. Each post is checked once (newsTagged).
NEWS_TAG_VERSION = 2  # 2: re-tag after Openverse photos were removed


def _ar_norm(text):
    text = re.sub(r"[\u064B-\u0652\u0640]", "", text or "")  # harakat, tatweel
    return (text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
            .replace("ى", "ي").replace("ة", "ه"))


def tag_news(db):
    if db is None:
        return
    todo = [s for s in db.collection("articles").stream()
            if (s.to_dict() or {}).get("newsTagged") != NEWS_TAG_VERSION]
    if not todo:
        return
    fighters = [(s.id, s.to_dict() or {}) for s in db.collection("fighters").stream()]
    keys = {}  # key -> fighter ids using it
    for fid, d in fighters:
        ar = _ar_norm(d.get("nameAr", "")).strip()
        words = ar.split()
        cand = set()
        if len(words) >= 2:
            cand.add(("ar", ar))
        if words and len(words[-1]) >= 4:
            cand.add(("ar", words[-1]))
        for k in _name_keys(d.get("name", "")):
            cand.add(("en", k))
        for c in cand:
            keys.setdefault(c, set()).add(fid)
    unique = {k: next(iter(v)) for k, v in keys.items() if len(v) == 1}  # skip shared names (Silva...)
    info = dict(fighters)
    tagged = 0
    for snap in todo:
        a = snap.to_dict() or {}
        found = {}
        for weight, text in ((0, a.get("title", "")), (10000, a.get("summary", ""))):
            ar_text, en_text = _ar_norm(text), _letters(text)
            for (lang, k), fid in unique.items():
                pos = (ar_text if lang == "ar" else en_text).find(k)
                if pos >= 0:
                    found[fid] = min(found.get(fid, 10 ** 9), weight + pos)
        top = sorted(found, key=found.get)[:2]
        out = [{"id": fid, "name": info[fid].get("name", ""), "nameAr": info[fid].get("nameAr", ""),
                "photo": info[fid].get("photo") or None} for fid in top]
        snap.reference.set({"fighters": out, "newsTagged": NEWS_TAG_VERSION}, merge=True)
        tagged += 1
        if out:
            print(f"  news: {a.get('title', '')[:50]!r} -> {', '.join(f['name'] for f in out)}")
    print(f"News covers: {tagged} post(s) checked")


def merge_fights(mine, wiki):
    """Keep hand-edited fights, only fill in results that are still missing."""
    by_pair = {"|".join(sorted([norm(f["fighterA"]), norm(f["fighterB"])])): f for f in wiki}
    out = []
    for f in mine:
        f = dict(f)
        if not f.get("winner"):
            w = by_pair.get("|".join(sorted([norm(f.get("fighterA", "")), norm(f.get("fighterB", ""))])))
            if w and w["winner"]:
                if norm(w["winner"]) == norm(f.get("fighterA", "")):
                    f["winner"] = f["fighterA"]
                elif norm(w["winner"]) == norm(f.get("fighterB", "")):
                    f["winner"] = f["fighterB"]
                else:
                    f["winner"] = w["winner"]
                f["method"] = w["method"]
            if w:
                f.setdefault("fighterAAr", "")
                f.setdefault("fighterBAr", "")
        if not f.get("fighterAId"):
            f["fighterAId"] = fighter_key(f.get("fighterA", ""))
        if not f.get("fighterBId"):
            f["fighterBId"] = fighter_key(f.get("fighterB", ""))
        out.append(f)
    return out


def now_utc_weekday_is_monday_noon():
    n = datetime.now(timezone.utc)
    return n.weekday() == 0 and n.hour == 12


def run_fighters_now():
    return MODE == "fighters" or (MODE == "full" and datetime.now(timezone.utc).hour in (0, 12))


def main():
    if MODE == "quiz":
        db = firestore_client() if "FIREBASE_SERVICE_ACCOUNT" in os.environ else None
        if db is not None:
            sync_quiz(db, force=True)
        print("Done.")
        return
    if MODE in ("clips", "clips_backfill"):
        db = firestore_client() if "FIREBASE_SERVICE_ACCOUNT" in os.environ else None
        if db is not None:
            try:
                tag_news(db)
            except Exception as e:
                print(f"News covers failed: {type(e).__name__}")
            if MODE == "clips":
                run_clips_now(db)
            try:
                yt_clips(db, full=MODE == "clips_backfill", fighters=True)
            except Exception as e:
                print(f"YouTube clips failed: {type(e).__name__}")
        print("Done.")
        return
    if MODE == "fighters":
        db = firestore_client() if "FIREBASE_SERVICE_ACCOUNT" in os.environ else None
        sync_rankings(db)
        sync_fighters(db, with_records=True)
        try:
            sync_legends(db)
        except Exception as e:
            print(f"Legends failed: {e}")
        try:
            build_fighter_index(db)
        except Exception as e:
            print(f"Fighter search index failed: {type(e).__name__}")
        if db is not None:
            try:
                build_leaderboard(db)
            except Exception as e:
                print(f"Leaderboard failed: {e}")
        print("Done.")
        return
    events = list_events()
    print(f"Mode: {MODE}. Found {len(events)} UFC events to check on Wikipedia")
    db = firestore_client() if "FIREBASE_SERVICE_ACCOUNT" in os.environ else None
    if db is None:
        print("No FIREBASE_SERVICE_ACCOUNT set: dry run, nothing is saved.")

    photo_map = load_photo_map(db) if db is not None else {}
    scored_any = False
    live = []  # events happening now, for live_watch
    for ev in events:
        fights = []
        if ev["title"]:
            html = fetch_page_html(ev["title"])
            if html:
                fights = add_arabic(parse_card(html))
            time.sleep(1)  # be polite to Wikipedia
        start = guess_start(ev["date"], ev["city"])
        fights = bdl_fill_results(start, fights)
        print(f"- {ev['name']} | {start:%Y-%m-%d %H:%M} UTC | {len(fights)} fights")
        if db is None:
            continue

        try:
            ref = pick_event_ref(db, ev["name"], start, fights)
        except Exception as e:  # never let this stop the sync
            print(f"  duplicate check skipped: {e}")
            ref = db.collection("events").document(doc_id(ev["name"]))
        snap = ref.get()
        old = snap.to_dict() if snap.exists else {}
        # Keep results we already saved (e.g. live ones from BALLDONTLIE) until
        # Wikipedia posts them too, so they never disappear.
        if fights and old.get("fights"):
            prev = {f.get("id"): f for f in old["fights"] if f.get("winner")}
            fights = [
                {**f, "winner": prev[f["id"]]["winner"], "method": prev[f["id"]].get("method", "")}
                if not f.get("winner") and f.get("id") in prev else f
                for f in fights
            ]
            # Keep highlight clips found earlier (Wikipedia doesn't have them).
            clips = {f.get("id"): f for f in old["fights"] if f.get("clip")}
            for f in fights:
                if f.get("id") in clips and not f.get("clip"):
                    f["clip"] = clips[f["id"]]["clip"]
                    if clips[f["id"]].get("clips"):
                        f["clips"] = clips[f["id"]]["clips"]
        if photo_map:
            attach_photos(fights, photo_map)
        new = {
            "name": ev["name"],
            "nameAr": event_name_ar(ev["name"], fights),
            "location": ev["location"],
            "source": "wikipedia",
            "wikiTitle": ev["title"] or "",
        }
        if old.get("nameArLocked"):
            new.pop("nameAr")
        if not old.get("startLocked"):
            new["start"] = start
        if old.get("fightsLocked"):
            new["fights"] = merge_fights(old.get("fights", []), fights)
            if photo_map:
                attach_photos(new["fights"], photo_map)
        elif fights or not old.get("fights"):
            new["fights"] = fights

        new["searchTerms"] = search_terms(ev["name"] + " " + new.get("nameAr", old.get("nameAr", "")), new.get("fights", old.get("fights", [])))

        changed = any(old.get(k) != v for k, v in new.items() if k != "start") or (
            "start" in new and (not old.get("start") or old["start"].timestamp() != new["start"].timestamp()))
        if changed:
            ref.set(new, merge=True)
            print("  saved")
        try:
            check_alerts(ref, {**old, **new})
            merged = {**old, **new}
            follow_alerts(snap.exists, old.get("fights", []), merged.get("fights", []),
                          merged.get("name", ""), merged.get("nameAr") or merged.get("name", ""),
                          merged.get("start"))
        except Exception as e:  # an alert problem should never stop the sync
            print(f"  alert failed: {e}")
        try:
            check_fight_alerts(ref, {**old, **new})
        except Exception as e:
            print(f"  fight alert failed: {e}")
        try:
            find_clips(ref, {**old, **new})
        except Exception as e:
            print(f"  clips failed: {type(e).__name__}")
        on_card = new.get("fights", old.get("fights")) or []
        start_at = ({**old, **new}).get("start")
        if (MODE in ("full", "fightnight") and start_at and on_card
                and not all(f.get("winner") for f in on_card)
                and start_at - timedelta(hours=1) <= datetime.now(timezone.utc) <= start_at + timedelta(hours=9)):
            live.append((ref, start_at))
        try:
            check_pick_reminder(ref, {**old, **new})
        except Exception as e:
            print(f"  pick reminder failed: {e}")
        try:
            update_pick_split(db, ref, ref.id, {**old, **new})
        except Exception as e:
            print(f"  pick split failed: {e}")
        try:
            start_at = ({**old, **new}).get("start")
            if (MODE != "backfill" and start_at and not old.get("scored")
                    and datetime.now(timezone.utc) - start_at > timedelta(hours=12)):
                score_event(db, ref, ref.id, {**old, **new})
                scored_any = True
        except Exception as e:
            print(f"  scoring failed: {e}")
    try:
        live_watch(live)
    except Exception as e:
        print(f"Live watch failed: {e}")
    if MODE == "full":
        try:
            sync_rankings(db)
        except Exception as e:
            print(f"Rankings failed: {e}")
    if run_fighters_now():
        try:
            weekly = now_utc_weekday_is_monday_noon()
            sync_fighters(db, with_records=weekly)
        except Exception as e:
            print(f"Fighters failed: {e}")
        try:
            build_fighter_index(db)
        except Exception as e:
            print(f"Fighter search index failed: {type(e).__name__}")
    if MODE == "full" and db is not None:
        try:
            tag_news(db)
        except Exception as e:
            print(f"News covers failed: {type(e).__name__}")
        try:
            yt_clips(db, fighters=run_fighters_now())
        except Exception as e:
            print(f"YouTube clips failed: {type(e).__name__}")
    if db is not None and (scored_any or run_fighters_now()):
        try:
            build_leaderboard(db)
        except Exception as e:
            print(f"Leaderboard failed: {e}")
    now_utc = datetime.now(timezone.utc)
    if MODE in ("full", "fightnight") and db is not None:
        try:
            sync_quiz(db)  # only creates one per day
        except Exception as e:
            print(f"Quiz failed: {e}")
    if MODE == "full" and now_utc.weekday() == 0 and now_utc.hour == 0:
        try:
            sync_legends(db)
        except Exception as e:
            print(f"Legends failed: {e}")
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Sync failed: {e}")
        sys.exit(1)
