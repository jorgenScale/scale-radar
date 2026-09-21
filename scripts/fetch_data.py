#!/usr/bin/env python3
"""
Scale Radar – henter data og skriver data/data.json.

Kjøres av GitHub Actions hver time (se .github/workflows/update-data.yml),
eller manuelt:  python scripts/fetch_data.py

Prinsipp: hver seksjon hentes uavhengig. Feiler én kilde, beholdes forrige
verdi for den seksjonen og status settes til "error" – siden viser da tydelig
hva som er gammelt. Ingen tall anslås.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "content" / "config.json").read_text(encoding="utf-8"))
KONSESJONER = json.loads((ROOT / "content" / "konsesjoner.json").read_text(encoding="utf-8"))
OUT = ROOT / "data" / "data.json"

try:
    PREVIOUS = json.loads(OUT.read_text(encoding="utf-8"))
except Exception:  # første kjøring
    PREVIOUS = {}


def log(msg: str) -> None:
    print(f"[fetch_data] {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- nyheter

def scrape_html(feed: dict) -> list[dict]:
    """Leser artikkelkort fra en forside uten RSS.

    feed["item"]   – CSS-selektor for hvert kort
    feed["title"]  – selektor for tittel innenfor kortet
    feed["link"]   – selektor for lenke innenfor kortet, eller "self" hvis kortet selv er en <a>
    feed["time"]   – (valgfri) selektor for <time datetime=...>
    feed["link_pattern"] – (valgfri) regex som artikkel-URL må matche (filtrerer bort menyer/kampanjer)
    """
    import re
    from urllib.parse import urljoin

    import requests  # type: ignore
    from bs4 import BeautifulSoup  # type: ignore

    html = requests.get(feed["url"], timeout=30, headers={"User-Agent": "Mozilla/5.0 ScaleRadar/1.0"}).text
    soup = BeautifulSoup(html, "html.parser")
    pattern = re.compile(feed["link_pattern"]) if feed.get("link_pattern") else None
    out: list[dict] = []
    seen: set[str] = set()
    for card in soup.select(feed["item"]):
        title_el = card.select_one(feed["title"])
        if not title_el:
            continue
        link_el = card if feed.get("link", "self") == "self" else card.select_one(feed["link"])
        href = link_el.get("href") if link_el else None
        if not href:
            continue
        url = urljoin(feed["url"], href)
        if pattern and not pattern.search(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        published = None
        if feed.get("time"):
            t = card.select_one(feed["time"])
            if t and t.get("datetime"):
                published = t["datetime"][:10]
        out.append({"title": title_el.get_text(" ", strip=True), "url": url, "published": published})
    return out

def fetch_news() -> tuple[list[dict], list[dict]]:
    import feedparser  # type: ignore

    items: list[dict] = []
    sources: list[dict] = []
    per = int(CONFIG.get("news_per_source", 10))
    for feed in CONFIG["feeds"]:
        key, name, url = feed["key"], feed["name"], feed["url"]
        try:
            if feed.get("type", "rss") == "html":
                entries = scrape_html(feed)[:per]
                if not entries:
                    raise RuntimeError("fant ingen artikler på siden – selektorene i config.json må sjekkes")
                for e in entries:
                    items.append(dict(e, source_key=key, source=name))
                sources.append({"key": key, "name": name, "status": "ok", "count": len(entries)})
                log(f"nyheter {name}: {len(entries)} saker (html)")
                continue
            parsed = feedparser.parse(url, request_headers={"User-Agent": "ScaleRadar/1.0"})
            entries = parsed.entries[:per]
            if not entries:
                raise RuntimeError(f"tom feed ({parsed.get('status', 'ingen status')})")
            for e in entries:
                published = None
                for attr in ("published_parsed", "updated_parsed"):
                    t = e.get(attr)
                    if t:
                        published = time.strftime("%Y-%m-%d", t)
                        break
                items.append({
                    "source_key": key,
                    "source": name,
                    "published": published,
                    "title": (e.get("title") or "").strip(),
                    "url": e.get("link"),
                })
            sources.append({"key": key, "name": name, "status": "ok", "count": len(entries)})
            log(f"nyheter {name}: {len(entries)} saker")
        except Exception as exc:  # noqa: BLE001
            log(f"nyheter {name}: FEIL {exc}")
            # behold gamle saker fra denne kilden
            old = [n for n in PREVIOUS.get("news", []) if n.get("source_key") == key]
            items.extend(old)
            sources.append({"key": key, "name": name, "status": "error", "count": len(old), "error": str(exc)[:200]})
    items.sort(key=lambda n: n.get("published") or "", reverse=True)
    return items, sources


# --------------------------------------------------------------------------- aksjer
def fetch_stocks() -> dict:
    import yfinance as yf  # type: ignore

    groups_out = []
    ok = 0
    total = 0
    as_of = None
    for group in CONFIG["stock_groups"]:
        rows = []
        for item in group["items"]:
            total += 1 if item.get("yahoo") else 0
            row = {"name": item["name"], "ticker": item["ticker"], "currency": item.get("currency", "NOK"),
                   "price": None, "change_pct": None}
            try:
                if not item.get("yahoo"):
                    row["note"] = item.get("note", "Ingen kurskilde konfigurert")
                    rows.append(row)
                    continue
                hist = yf.Ticker(item["yahoo"]).history(period="10d", interval="1d", auto_adjust=False)
                hist = hist.dropna(subset=["Close"])
                if len(hist) >= 1:
                    last = float(hist["Close"].iloc[-1])
                    row["price"] = round(last, 2)
                    as_of = max(as_of or "", hist.index[-1].strftime("%Y-%m-%d"))
                    if len(hist) >= 2:
                        prev = float(hist["Close"].iloc[-2])
                        if prev:
                            row["change_pct"] = round((last / prev - 1) * 100, 2)
                    ok += 1
                else:
                    raise RuntimeError("ingen kursdata")
            except Exception as exc:  # noqa: BLE001
                log(f"aksje {item['yahoo']}: FEIL {exc}")
                row["error"] = str(exc)[:120]
            rows.append(row)
        groups_out.append({"name": group["name"], "items": rows})

    status = "ok" if ok == total else ("partial" if ok else "error")
    note = None
    if status != "ok":
        note = f"{ok} av {total} kurser hentet. Manglende tickere er merket med strek – sjekk Yahoo-symbolet i content/config.json."
    return {
        "status": status,
        "as_of": as_of or PREVIOUS.get("stocks", {}).get("as_of"),
        "note": note,
        "groups": groups_out,
        "company_news": PREVIOUS.get("stocks", {}).get("company_news", []),
        "source": "Yahoo Finance (uoffisiell, ca. 15 min forsinket)",
    }


# --------------------------------------------------------------------------- laksepris (SSB)
def fetch_salmon_price() -> dict:
    """Henter ukentlig eksportpris og volum fra SSB tabell 03024 (JSON-stat2).

    Kodene slås opp fra tabellens metadata så skriptet ikke er avhengig av
    hardkodede kodeverdier.
    """
    import requests  # type: ignore

    api = CONFIG["ssb"]["api"]
    weeks = int(CONFIG["ssb"].get("weeks", 16))
    meta = requests.get(api, timeout=30).json()

    def pick(var_text_contains: str, value_text_contains: list[str]) -> tuple[str, list[str]]:
        for var in meta["variables"]:
            if var_text_contains.lower() in (var.get("text") or var.get("code", "")).lower() or \
               var_text_contains.lower() in var.get("code", "").lower():
                chosen = []
                for code, text in zip(var["values"], var["valueTexts"]):
                    if any(s.lower() in text.lower() for s in value_text_contains):
                        chosen.append(code)
                if chosen:
                    return var["code"], chosen
        raise RuntimeError(f"fant ikke variabel '{var_text_contains}' i SSB-metadata")

    vare_code, vare_vals = pick("vare", ["fersk", "fros", "frys"])
    cont_code, cont_vals = pick("contents", ["kilo", "pris", "vekt", "tonn"])
    time_code = next(v["code"] for v in meta["variables"] if v.get("time") or v["code"].lower() == "tid")

    query = {
        "query": [
            {"code": vare_code, "selection": {"filter": "item", "values": vare_vals}},
            {"code": cont_code, "selection": {"filter": "item", "values": cont_vals}},
            {"code": time_code, "selection": {"filter": "top", "values": [str(weeks)]}},
        ],
        "response": {"format": "json-stat2"},
    }
    ds = requests.post(api, json=query, timeout=60).json()

    dims = ds["id"]
    sizes = ds["size"]
    index_of = {d: {code: i for i, code in enumerate(ds["dimension"][d]["category"]["index"])} for d in dims}
    labels = {d: ds["dimension"][d]["category"]["label"] for d in dims}
    values = ds["value"]

    def value_at(**coords) -> float | None:
        pos = 0
        mult = 1
        for d, n in zip(reversed(dims), reversed(sizes)):
            pos += index_of[d][coords[d]] * mult
            mult *= n
        return values[pos]

    fresh = next(c for c in index_of[vare_code] if "fersk" in labels[vare_code][c].lower())
    frozen = next((c for c in index_of[vare_code] if "fros" in labels[vare_code][c].lower() or "frys" in labels[vare_code][c].lower()), None)
    price_c = next(c for c in index_of[cont_code] if "pris" in labels[cont_code][c].lower() or "kilo" in labels[cont_code][c].lower())
    vol_c = next((c for c in index_of[cont_code] if "tonn" in labels[cont_code][c].lower() or "vekt" in labels[cont_code][c].lower()), None)

    series = []
    for t in index_of[time_code]:
        p = value_at(**{vare_code: fresh, cont_code: price_c, time_code: t})
        if p is None:
            continue
        row = {"week": t, "price": round(float(p), 2)}
        if vol_c:
            v = value_at(**{vare_code: fresh, cont_code: vol_c, time_code: t})
            if v is not None:
                row["volume"] = int(round(float(v)))
        series.append(row)
    series.sort(key=lambda r: r["week"])

    out = {
        "status": "ok",
        "unit": "kr/kg",
        "published": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "source": "SSB tabell 03024",
        "source_url": "https://www.ssb.no/statbank/table/03024",
        "note": "Ukentlig gjennomsnittlig eksportpris for fersk laks (alle vektklasser, inkl. kontrakt). Publiseres av SSB onsdag for foregående uke.",
        "series": series,
    }
    if frozen and series:
        last = series[-1]["week"]
        fp = value_at(**{vare_code: frozen, cont_code: price_c, time_code: last})
        fv = value_at(**{vare_code: frozen, cont_code: vol_c, time_code: last}) if vol_c else None
        if fp is not None:
            out["frozen"] = {"week": last, "price": round(float(fp), 2), "volume": int(round(float(fv))) if fv is not None else None}
    log(f"laksepris: {len(series)} uker, siste {series[-1] if series else '-'}")
    return out



# --------------------------------------------------------------------------- lokalitetssøknader (Fiskeridirektoratet)
def fetch_applications() -> dict:
    """Henter Fiskeridirektoratets CSV-eksport av akvakultursøknader per søknadstype.

    Kolonner i eksporten (sept. 2026): Søknadsnummer, Søknadstype, Status, Tittel,
    Søkers navn, Organisasjonsnummer, Innsendt, Trukket, Fylke, Kommune,
    Produksjonsområde, Lokalitetsnummer, Lokalitet, Art, Breddegrad, Lengdegrad.
    """
    import csv
    import io

    import requests  # type: ignore

    acfg = CONFIG["applications"]
    out: dict = {"status": "ok", "source_url": "https://www.fiskeridir.no/akvakultur/akvakultursoknader",
                 "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "types": {}}

    def num(v: str) -> float | None:
        v = (v or "").strip().replace(",", ".")
        try:
            return round(float(v), 5) if v else None
        except ValueError:
            return None

    for t in acfg["types"]:
        apptypes = t.get("apptypes") or [t.get("apptype")]
        rows: list[dict] = []
        for apptype in apptypes:
            r = requests.get(acfg["url"], params={"apptype": apptype, "format": "csv"}, timeout=60,
                             headers={"User-Agent": "Mozilla/5.0 ScaleRadar/1.0"})
            r.raise_for_status()
            text = r.content.decode("utf-8-sig")
            part = list(csv.DictReader(io.StringIO(text), delimiter=";"))
            log(f"søknader {apptype}: {len(part)} rader")
            rows.extend(part)
        if not rows:
            raise RuntimeError(f"tom eksport for {apptypes}")
        items = []
        seen: set[str] = set()
        for row in rows:
            if (row.get("Søknadsnummer") or "") in seen:
                continue
            seen.add(row.get("Søknadsnummer") or "")
            g = lambda k: (row.get(k) or "").strip()  # noqa: E731
            app_id = g("Søknadsnummer")
            items.append({
                "id": app_id,
                "type": g("Søknadstype"),
                "status": g("Status"),
                "title": g("Tittel"),
                "applicant": g("Søkers navn"),
                "orgnr": g("Organisasjonsnummer") or None,
                "submitted": g("Innsendt") or None,
                "withdrawn": g("Trukket") or None,
                "county": g("Fylke") or None,
                "municipality": g("Kommune") or None,
                "po": g("Produksjonsområde") or None,
                "site_no": g("Lokalitetsnummer") or None,
                "site": g("Lokalitet") or None,
                "species": g("Art") or None,
                "lat": num(g("Breddegrad")),
                "lon": num(g("Lengdegrad")),
                "url": acfg["detail_base"] + app_id.lower() if app_id else None,
            })
        items.sort(key=lambda x: x["submitted"] or "", reverse=True)
        out["types"][t["key"]] = {"apptype": apptypes[0], "apptypes": apptypes, "label": t["label"], "count": len(items), "items": items}
        log(f"søknader {t['label']}: {len(items)} rader totalt")

    if acfg.get("details", {}).get("enabled", True):
        try:
            enrich_details(out)
        except Exception as exc:  # noqa: BLE001
            log(f"detaljer: FEIL {exc}")
            out["details_error"] = str(exc)[:160]
    if acfg.get("einnsyn", {}).get("enabled", False):
        try:
            enrich_einnsyn(out)
        except Exception as exc:  # noqa: BLE001
            log(f"einnsyn: FEIL {exc}")
            out["einnsyn_error"] = str(exc)[:160]
    return out


DETAILS_PATH = ROOT / "data" / "details.json"


def enrich_details(apps: dict) -> None:
    """Henter detaljsiden per søknad (MTB, planlagt produksjon, utfall, saksbehandler …) med cache.

    Cache i data/details.json: {søknadsnummer: {status, fetched_at, title, fields, norm}}.
    Hentes på nytt når søknaden er ny, når status i eksporten har endret seg, eller når en sak
    under behandling er eldre enn refresh_days. Antall sider per kjøring er begrenset.
    """
    import re
    import time as _time

    import requests  # type: ignore
    from bs4 import BeautifulSoup  # type: ignore

    dcfg = CONFIG["applications"].get("details", {})
    max_per_run = int(dcfg.get("max_per_run", 60))
    refresh_days = float(dcfg.get("refresh_days", 3))
    pause = float(dcfg.get("pause_seconds", 0.4))

    try:
        cache: dict = json.loads(DETAILS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        cache = {}

    now = datetime.now(timezone.utc)

    def age_days(iso: str | None) -> float:
        if not iso:
            return 1e9
        try:
            return (now - datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() / 86400
        except ValueError:
            return 1e9

    all_items = [i for t in apps["types"].values() for i in t["items"]]
    todo: list[tuple[int, dict]] = []
    for it in all_items:
        c = cache.get(it["id"])
        if not it.get("url"):
            continue
        if c is None:
            todo.append((0, it))                                   # ny søknad – høyest prioritet
        elif c.get("status") != it["status"]:
            todo.append((1, it))                                   # status endret
        elif it["status"] == "Under behandling" and age_days(c.get("fetched_at")) > refresh_days:
            todo.append((2, it))                                   # gammel sak under behandling
    # prioritet først, deretter nyeste innsendt først
    todo.sort(key=lambda x: (x[0], -(int((x[1].get("submitted") or "0000-00-00").replace("-", "") or 0))))
    batch = [it for _, it in todo[:max_per_run]]
    log(f"detaljer: {len(cache)} i cache, {len(todo)} å hente, tar {len(batch)} nå")

    def to_int(v: str) -> int | None:
        m = re.search(r"-?[\d\s]+", v.replace("\xa0", " "))
        if not m:
            return None
        digits = re.sub(r"\D", "", m.group(0))
        return int(digits) if digits else None

    def to_date(v: str) -> str | None:
        m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", v.strip())
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None

    skip = {"Søknadsnummer", "Søknadstype", "Status", "Søkers navn", "Organisasjonsnummer"}
    fetched = 0
    for it in batch:
        try:
            r = requests.get(it["url"], timeout=30, headers={"User-Agent": "Mozilla/5.0 ScaleRadar/1.0"})
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            main = soup.find("main") or soup
            fields: dict[str, str] = {}
            for dt in main.select("dl dt"):
                dd = dt.find_next_sibling("dd")
                if dd is None:
                    continue
                k = dt.get_text(" ", strip=True)
                v = dd.get_text(" ", strip=True)
                if k and k not in skip:
                    fields[k] = v
            h1 = main.find("h1")
            norm = {
                "mtb_tonn": to_int(fields.get("Maksimal tillatt biomasse (MTB)", "")) if "Maksimal tillatt biomasse (MTB)" in fields else None,
                "planned_production_tonn": to_int(fields.get("Planlagt produksjon per produksjonssyklus", "")) if "Planlagt produksjon per produksjonssyklus" in fields else None,
                "cycle_months": to_int(fields.get("Produksjonssykluslengde", "")) if "Produksjonssykluslengde" in fields else None,
                "feed_per_cycle_tonn": to_int(fields.get("Planlagt fôrforbruk per produksjonssyklus", "")) if "Planlagt fôrforbruk per produksjonssyklus" in fields else None,
                "max_monthly_feed_tonn": to_int(fields.get("Maksimal månedlig fôring", "")) if "Maksimal månedlig fôring" in fields else None,
                "species": fields.get("Art"),
                "decided": to_date(fields.get("Ferdigbehandlet", "")),
                "withdrawn": to_date(fields.get("Trukket", "")),
                "result": fields.get("Resultat"),
                "case_handler": fields.get("Ansvarlig saksbehandler"),
            }
            cache[it["id"]] = {
                "status": it["status"],
                "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "title": h1.get_text(" ", strip=True) if h1 else None,
                "fields": fields,
                "norm": {k: v for k, v in norm.items() if v is not None},
            }
            fetched += 1
        except Exception as exc:  # noqa: BLE001
            log(f"detaljer {it['id']}: FEIL {exc}")
        _time.sleep(pause)

    DETAILS_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
    log(f"detaljer: hentet {fetched}, cache nå {len(cache)}")

    known = {i["id"] for i in all_items}
    for it in all_items:
        c = cache.get(it["id"])
        if c:
            it["details"] = dict(c.get("norm", {}), fetched_at=c.get("fetched_at"))
            it["fields"] = c.get("fields", {})
    apps["details_summary"] = {
        "cached": sum(1 for k in cache if k in known),
        "pending": max(0, len(todo) - len(batch)),
        "fetched_now": fetched,
    }



# --------------------------------------------------------------------------- saksgang fra eInnsyn
EINNSYN_CACHE = ROOT / "data" / "einnsyn.json"
EINNSYN_DIR = ROOT / "data" / "einnsyn"

STEP_RULES = [
    # (nøkkel, etikett, regex på etat/avsender/mottaker, regex på tittel) – første regel som treffer gjelder
    ("klage",          "Klage",                  None,                                  r"\bklage"),
    ("mattilsynet",    "Mattilsynet",            r"mattilsynet",                         None),
    ("statsforvalter", "Statsforvalteren",       r"statsforvalter|fylkesmann",           None),
    ("kystverket",     "Kystverket",             r"kystverket",                          None),
    ("fiskeridir",     "Fiskeridirektoratet",    r"fiskeridirektoratet",                 None),
    ("kommune",        "Kommunen",               r"(?<!fylkes)kommune",                  r"offentlig ettersyn|høring|utlegging|kunngjøring|kommunal uttalelse|kommunen"),
    ("vedtak",         "Vedtak fylkeskommune",   r"fylkeskommune",                       r"vedtak|tillatelse|godkjenn|avslag|avslår|innvilg|klarering"),
]


def applicant_core(name: str) -> str:
    """'Måsøval Lisens AS' -> 'Måsøval'. Første betydningsfulle ord, uten selskapsform."""
    import re
    words = [w for w in re.split(r"\s+", (name or "").strip()) if w]
    stop = {"as", "asa", "sa", "da", "ans", "ba", "holding", "group", "norway", "aquaculture", "seafood", "farming", "havbruk", "oppdrett", "sjø", "lisens", "salmon"}
    for w in words:
        if w.lower().strip("().,") not in stop and len(w) > 2:
            return w.strip("().,")
    return words[0] if words else ""


def classify_steps(posts: list[dict]) -> dict:
    import re
    steps: dict[str, dict] = {}
    for p in sorted(posts, key=lambda x: x.get("date") or ""):
        who = " ".join(filter(None, [p.get("enhet")] + p.get("from", []) + p.get("to", []))).lower()
        title = (p.get("title") or "").lower()
        for key, label, who_rx, title_rx in STEP_RULES:
            hit = False
            if key == "klage":
                hit = bool(re.search(title_rx, title))
            elif key == "kommune":
                hit = bool(re.search(who_rx, who)) or bool(re.search(title_rx, title))
            elif key == "vedtak":
                hit = bool(re.search(who_rx, who)) and bool(re.search(title_rx, title)) and p.get("type") == "ut"
            else:
                hit = bool(re.search(who_rx, who))
            if hit:
                st = steps.setdefault(key, {"label": label, "first": p.get("date"), "count": 0})
                st["count"] += 1
                st["last"] = p.get("date")
                st["last_title"] = p.get("title")
                break
    return steps


def enrich_einnsyn(apps: dict) -> None:
    """Søker eInnsyn per søknad (lokalitetsnavn + søker) og lagrer journalposter med cache."""
    import re
    import time as _time
    from urllib.parse import quote

    import requests  # type: ignore

    ecfg = CONFIG["applications"].get("einnsyn", {})
    api = ecfg.get("api", "https://api.einnsyn.no/search")
    web = ecfg.get("web", "https://einnsyn.no").rstrip("/")
    max_per_run = int(ecfg.get("max_per_run", 100))
    ref_active = float(ecfg.get("refresh_days_active", 1))
    ref_closed = float(ecfg.get("refresh_days_closed", 30))
    pause = float(ecfg.get("pause_seconds", 0.3))
    limit = int(ecfg.get("limit", 100))
    before_days = int(ecfg.get("days_before_submitted", 60))
    max_posts = int(ecfg.get("max_posts_per_application", 60))

    try:
        cache: dict = json.loads(EINNSYN_CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        cache = {}
    EINNSYN_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    def age_days(iso: str | None) -> float:
        try:
            return (now - datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() / 86400 if iso else 1e9
        except ValueError:
            return 1e9

    all_items = [i for t in apps["types"].values() for i in t["items"]]
    todo: list[tuple[int, dict]] = []
    for it in all_items:
        name = (it.get("title") or it.get("site") or "").strip()
        if not name:
            continue
        c = cache.get(it["id"])
        active = it.get("status") == "Under behandling"
        if c is None:
            todo.append((0, it))
        elif active and age_days(c.get("fetched_at")) > ref_active:
            todo.append((1, it))
        elif not active and age_days(c.get("fetched_at")) > ref_closed:
            todo.append((2, it))
    todo.sort(key=lambda x: (x[0], -(int((x[1].get("submitted") or "0000-00-00").replace("-", "") or 0))))
    batch = [it for _, it in todo[:max_per_run]]
    log(f"einnsyn: {len(cache)} i cache, {len(todo)} å hente, tar {len(batch)} nå")

    def names(parts: list, kind_rx: str) -> list[str]:
        out = []
        for k in parts or []:
            if re.search(kind_rx, k.get("korrespondanseparttype") or ""):
                n = k.get("korrespondansepartNavnSensitiv") or k.get("korrespondansepartNavn")
                if n:
                    out.append(n)
        return out

    fetched = 0
    for it in batch:
        name = (it.get("title") or it.get("site") or "").strip()
        core = applicant_core(it.get("applicant") or "")
        query = f'"{name}" {core}'.strip()
        params = [("query", query), ("entity", "Journalpost"), ("expand", "korrespondansepart"), ("expand", "journalenhet"),
                  ("expand", "saksmappe"), ("limit", str(limit)), ("sortBy", "journaldato"), ("sortOrder", "desc")]
        if it.get("submitted"):
            try:
                frm = datetime.strptime(it["submitted"], "%Y-%m-%d") - timedelta(days=before_days)
                params.append(("journaldatoFrom", frm.strftime("%Y-%m-%d")))
            except ValueError:
                pass
        try:
            r = requests.get(api, params=params, timeout=30, headers={"User-Agent": "ScaleRadar/1.0", "Accept": "application/json"})
            r.raise_for_status()
            items = r.json().get("items", [])
            posts = []
            name_rx = re.compile(re.escape(name.lower()))
            core_rx = re.compile(re.escape(core.lower())) if core else None

            def parsed(jp: dict) -> tuple[str, list[str], list[str], dict, str]:
                title = jp.get("offentligTittelSensitiv") or jp.get("offentligTittel") or ""
                frm_ = names(jp.get("korrespondansepart"), r"^avsender|^intern_avsender")
                to_ = names(jp.get("korrespondansepart"), r"^mottaker|^intern_mottaker")
                sm = jp.get("saksmappe") if isinstance(jp.get("saksmappe"), dict) else {}
                enhet = (jp.get("journalenhet") or {}).get("navn") if isinstance(jp.get("journalenhet"), dict) else None
                return title, frm_, to_, sm, enhet or ""

            # Pass 1: strenge treff – lokalitetsnavn i tittel + søker eller akvakulturkontekst
            strict: set[int] = set()
            cases: set[tuple[str, str]] = set()
            for idx, jp in enumerate(items):
                title, frm_, to_, sm, enhet = parsed(jp)
                hay = (title + " " + " ".join(frm_ + to_)).lower()
                if not name_rx.search(title.lower()):
                    continue
                if core_rx and not core_rx.search(hay) and not re.search(r"akvakultur|lokalitet|oppdrett", title.lower()):
                    continue
                strict.add(idx)
                if sm.get("saksnummer"):
                    cases.add((enhet, sm["saksnummer"]))
            # Pass 2: ta med øvrige journalposter i de samme saksmappene (samme etat + saksnummer)
            for idx, jp in enumerate(items):
                title, frm_, to_, sm, enhet = parsed(jp)
                if idx not in strict and not (sm.get("saksnummer") and (enhet, sm["saksnummer"]) in cases):
                    continue
                jt = jp.get("journalposttype") or ""
                url = None
                if sm.get("externalId") and jp.get("externalId"):
                    url = f"{web}/saksmappe?id={quote(sm['externalId'], safe='')}&jid={quote(jp['externalId'], safe='')}"
                posts.append({
                    "date": jp.get("journaldato") or jp.get("publisertDato"),
                    "type": "inn" if jt.startswith("inng") else ("ut" if jt.startswith("utg") else "intern"),
                    "enhet": enhet or None,
                    "same_case": idx not in strict,
                    "from": [n for n in frm_ if not n.lower().startswith("intern")][:3],
                    "to": to_[:3],
                    "title": title,
                    "saksnummer": sm.get("saksnummer"),
                    "url": url,
                })
            posts.sort(key=lambda x: x.get("date") or "", reverse=True)
            posts = posts[:max_posts]
            entry = {
                "fetched_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": it.get("status"),
                "query": query,
                "search_url": f"{web}/sok?searchTerm={quote(query)}",
                "count": len(posts),
                "last": posts[0]["date"] if posts else None,
                "steps": classify_steps(posts),
                "posts": posts,
            }
            cache[it["id"]] = entry
            (EINNSYN_DIR / f"{it['id']}.json").write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
            fetched += 1
        except Exception as exc:  # noqa: BLE001
            log(f"einnsyn {it['id']}: FEIL {exc}")
        _time.sleep(pause)

    EINNSYN_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    log(f"einnsyn: hentet {fetched}, cache nå {len(cache)}")

    known = {i["id"] for i in all_items}
    for it in all_items:
        c = cache.get(it["id"])
        if c:
            it["einnsyn"] = {"count": c.get("count", 0), "last": c.get("last"), "steps": c.get("steps", {}),
                             "fetched_at": c.get("fetched_at"), "search_url": c.get("search_url")}
    apps["einnsyn_summary"] = {"cached": sum(1 for k in cache if k in known), "pending": max(0, len(todo) - len(batch)), "fetched_now": fetched}

# --------------------------------------------------------------------------- endringssporing + RSS
CHANGES_PATH = ROOT / "data" / "changes.json"
FEEDS_DIR = ROOT / "data" / "feeds"


def slugify(name: str) -> str:
    import re
    import unicodedata

    s = (name or "").lower().replace("æ", "ae").replace("ø", "oe").replace("å", "aa")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "ukjent"


def eff_status(it: dict) -> str:
    d = it.get("details") or {}
    if it.get("status") == "Ferdigbehandlet" and d.get("result"):
        r = d["result"]
        return "Godkjent" if "godkjent" in r.lower() or "innvilget" in r.lower() else ("Avslått" if "avsl" in r.lower() else r)
    return it.get("status") or ""


def track_changes(apps: dict) -> dict:
    """Sammenligner med forrige data.json og logger hendelser per søknad.

    Hendelser: ny søknad, statusendring, utfall (innen result_window_days), endret MTB.
    Første kjøring for en fane setter bare baseline. Skriver data/changes.json og RSS-feeder
    (alle hendelser + én per søker) til data/feeds/.
    """
    ccfg = CONFIG["applications"].get("changes", {})
    keep_days = float(ccfg.get("keep_days", 90))
    window = float(ccfg.get("result_window_days", 60))
    recent_days = float(ccfg.get("recent_days_on_page", 30))
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        logd = json.loads(CHANGES_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logd = {"baselines": {}, "events": []}
    logd.setdefault("baselines", {})
    logd.setdefault("events", [])

    prev_types = PREVIOUS.get("applications", {}).get("types", {})

    def parse_iso(v: str | None):
        try:
            return datetime.strptime(v[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc) if v else None
        except ValueError:
            return None

    def ev(it: dict, key: str, kind: str, text: str) -> dict:
        d = it.get("details") or {}
        return {
            "at": now_iso, "kind": kind, "text": text,
            "id": it["id"], "type_key": key, "type": it.get("type"),
            "applicant": it.get("applicant"), "title": it.get("title") or it.get("site") or it["id"],
            "municipality": it.get("municipality"), "county": it.get("county"),
            "status": eff_status(it), "mtb_tonn": d.get("mtb_tonn"), "url": it.get("url"),
        }

    new_events: list[dict] = []
    for key, t in apps["types"].items():
        prev_items = {i["id"]: i for i in prev_types.get(key, {}).get("items", [])}
        if key not in logd["baselines"] or not prev_items:
            logd["baselines"][key] = now_iso          # første gang vi ser denne fanen – ingen hendelser
            continue
        for it in t["items"]:
            p = prev_items.get(it["id"])
            d = it.get("details") or {}
            if p is None:
                new_events.append(ev(it, key, "ny", f"Ny søknad – {it.get('status')}"))
                continue
            pd = p.get("details") or {}
            if p.get("status") != it.get("status"):
                new_events.append(ev(it, key, "status", f"Status: {p.get('status')} → {it.get('status')}"))
            decided = parse_iso(d.get("decided"))
            if d.get("result") and not pd.get("result") and (decided is None or (now - decided).days <= window):
                new_events.append(ev(it, key, "utfall", f"Utfall: {d['result']}"))
            if pd.get("mtb_tonn") and d.get("mtb_tonn") and pd["mtb_tonn"] != d["mtb_tonn"]:
                new_events.append(ev(it, key, "mtb", f"MTB endret: {pd['mtb_tonn']} → {d['mtb_tonn']} tonn"))

    events = new_events + logd["events"]
    cutoff = now.timestamp() - keep_days * 86400
    events = [e for e in events if (parse_iso(e.get("at")) or now).timestamp() >= cutoff]
    events.sort(key=lambda e: e.get("at") or "", reverse=True)
    logd["events"] = events
    logd["updated_at"] = now_iso
    CHANGES_PATH.write_text(json.dumps(logd, ensure_ascii=False, indent=0), encoding="utf-8")
    log(f"endringer: {len(new_events)} nye hendelser, {len(events)} i loggen")

    # ---- RSS
    site = CONFIG.get("site", {}).get("url", "https://jorgenscale.github.io/scale-radar/").rstrip("/") + "/"
    FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    (FEEDS_DIR / "soker").mkdir(exist_ok=True)

    def rfc822(iso: str) -> str:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")

    def x(v) -> str:
        from xml.sax.saxutils import escape
        return escape(str(v if v is not None else ""))

    def rss(title: str, desc: str, link: str, evs: list[dict]) -> str:
        items = []
        for e in evs[:200]:
            body = f"{x(e.get('text'))}<br>Søker: {x(e.get('applicant'))}<br>Kommune: {x(e.get('municipality'))}{', ' + x(e.get('county')) if e.get('county') else ''}<br>Søknadstype: {x(e.get('type'))}"
            if e.get("mtb_tonn"):
                body += f"<br>MTB: {x(e['mtb_tonn'])} tonn"
            items.append(
                "<item>"
                f"<title>{x(e.get('applicant'))} – {x(e.get('title'))}: {x(e.get('text'))}</title>"
                f"<link>{x(e.get('url') or site)}</link>"
                f"<guid isPermaLink=\"false\">{x(e['id'])}-{x(e['kind'])}-{x(e['at'])}</guid>"
                f"<pubDate>{rfc822(e['at'])}</pubDate>"
                f"<description><![CDATA[{body}]]></description>"
                "</item>"
            )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
            f"<title>{x(title)}</title><link>{x(link)}</link><description>{x(desc)}</description>"
            f"<language>nb</language><lastBuildDate>{rfc822(now_iso)}</lastBuildDate>"
            + "".join(items) + "</channel></rss>\n"
        )

    (FEEDS_DIR / "alle.xml").write_text(
        rss("Scale Radar – alle endringer i lokalitetssøknader", "Nye søknader, statusendringer og utfall fra Fiskeridirektoratet", site + "#apps", events),
        encoding="utf-8")

    slugs: dict[str, str] = {}
    all_items = [i for t in apps["types"].values() for i in t["items"]]
    for name in sorted({i.get("applicant") for i in all_items if i.get("applicant")}):
        slug = slugify(name)
        slugs[name] = slug
        mine = [e for e in events if e.get("applicant") == name]
        (FEEDS_DIR / "soker" / f"{slug}.xml").write_text(
            rss(f"Scale Radar – {name}", f"Endringer i lokalitetssøknader fra {name}", site + "#apps", mine), encoding="utf-8")
    log(f"rss: alle.xml + {len(slugs)} søkerfeeder")

    recent_cut = now.timestamp() - recent_days * 86400
    return {
        "updated_at": now_iso,
        "baselines": logd["baselines"],
        "feeds": {"all": site + "data/feeds/alle.xml", "applicant_base": site + "data/feeds/soker/", "slugs": slugs},
        "events": [e for e in events if (parse_iso(e.get("at")) or now).timestamp() >= recent_cut][:300],
    }

# --------------------------------------------------------------------------- main
def main() -> int:
    data: dict = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "live",
    }

    try:
        news, sources = fetch_news()
        data["news"], data["sources"] = news, sources
    except Exception as exc:  # noqa: BLE001
        log(f"nyheter: FEIL {exc}")
        data["news"] = PREVIOUS.get("news", [])
        data["sources"] = [dict(s, status="error") for s in PREVIOUS.get("sources", [])]

    try:
        data["stocks"] = fetch_stocks()
    except Exception as exc:  # noqa: BLE001
        log(f"aksjer: FEIL {exc}")
        data["stocks"] = dict(PREVIOUS.get("stocks", {}), status="error", note=f"Kursfeed feilet: {str(exc)[:120]}")

    try:
        data["salmon_price"] = fetch_salmon_price()
    except Exception as exc:  # noqa: BLE001
        log(f"laksepris: FEIL {exc}")
        data["salmon_price"] = dict(PREVIOUS.get("salmon_price", {}), status="error")

    try:
        data["applications"] = fetch_applications()
    except Exception as exc:  # noqa: BLE001
        log(f"søknader: FEIL {exc}")
        data["applications"] = dict(PREVIOUS.get("applications", {}), status="error", error=str(exc)[:160])

    try:
        if data["applications"].get("types"):
            data["changes"] = track_changes(data["applications"])
    except Exception as exc:  # noqa: BLE001
        log(f"endringer: FEIL {exc}")
        data["changes"] = dict(PREVIOUS.get("changes", {}), error=str(exc)[:160])

    licences = {k: v for k, v in KONSESJONER.items() if not k.startswith("_")}
    data["licences"] = licences

    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"skrev {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
