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
def fetch_news() -> tuple[list[dict], list[dict]]:
    import feedparser  # type: ignore

    items: list[dict] = []
    sources: list[dict] = []
    per = int(CONFIG.get("news_per_source", 10))
    for feed in CONFIG["feeds"]:
        key, name, url = feed["key"], feed["name"], feed["url"]
        try:
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
            total += 1
            row = {"name": item["name"], "ticker": item["ticker"], "currency": item.get("currency", "NOK"),
                   "price": None, "change_pct": None}
            try:
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

    licences = {k: v for k, v in KONSESJONER.items() if not k.startswith("_")}
    data["licences"] = licences

    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"skrev {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
