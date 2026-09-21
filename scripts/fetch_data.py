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
from datetime import datetime, timezone
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

    licences = {k: v for k, v in KONSESJONER.items() if not k.startswith("_")}
    data["licences"] = licences

    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"skrev {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
