#!/usr/bin/env python3
"""
Scale Radar – fiskehelse fra BarentsWatch (lus, behandlinger, sykdom, temperatur, rømming).

Kjøres av GitHub Actions etter fetch_data.py. Krever secrets BW_CLIENT_ID og BW_CLIENT_SECRET
(BarentsWatch API-klient, OAuth2 client credentials). Uten secrets skriver skriptet en
statusfil og avslutter uten feil.

Prinsipp (BarentsWatch ber om én tråd og moderat tempo):
  1. Ukesbilder: POST /v2/geodata/fishhealth/locality/{år}/{uke} gir alle lokaliteter for én uke
     (lus, temperatur, brakklegging, behandlinger, sykdom). Vi henter uker vi mangler, nyeste først,
     begrenset per kjøring, og bygger kompakte serier per lokalitet i data/fishhealth/loc/<nr>.json.
  2. Detaljer: GET /v2/geodata/fishhealth/locality/{nr}/{år}/{uke} gir produksjonsområde, MTB,
     driftsselskap, arter, rømminger. Hentes trinnvis for aktive lokaliteter, friskes opp månedlig.
  3. Indeks (data/fishhealth/index.json) med siste uke per lokalitet for kart og liste, og
     aggregat (data/fishhealth/agg.json) per uke nasjonalt og per produksjonsområde.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "content" / "config.json").read_text(encoding="utf-8")).get("fishhealth", {})
OUT_DIR = ROOT / "data" / "fishhealth"
LOC_DIR = OUT_DIR / "loc"
STATE_PATH = OUT_DIR / "state.json"
INDEX_PATH = OUT_DIR / "index.json"
AGG_PATH = OUT_DIR / "agg.json"
STATUS_PATH = OUT_DIR / "status.json"

TOKEN_URL = CONFIG.get("token_url", "https://id.barentswatch.no/connect/token")
API = CONFIG.get("api_base", "https://www.barentswatch.no/bwapi").rstrip("/")
HISTORY_WEEKS = int(CONFIG.get("history_weeks", 260))
WEEKS_PER_RUN = int(CONFIG.get("weeks_per_run", 60))
DETAILS_PER_RUN = int(CONFIG.get("details_per_run", 150))
DETAIL_REFRESH_DAYS = float(CONFIG.get("details_refresh_days", 30))
PAUSE = float(CONFIG.get("pause_seconds", 0.6))
MIN_HOURS_BETWEEN_RUNS = float(CONFIG.get("min_hours_between_runs", 20))
LIMIT = 0.5          # grense for voksne hunnlus per fisk
SPRING_LIMIT = 0.2   # vårgrense (uke 16–21 sør for PO8, uke 21–26 fra PO8 og nordover)


def log(msg: str) -> None:
    print(f"[fishhealth] {msg}", file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_status(status: str, **extra) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(dict({"status": status, "updated_at": now_iso()}, **extra), ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- hjelpere
def iso_weeks_back(n: int) -> list[tuple[int, int]]:
    """Siste n ISO-uker, nyeste først. Nyeste = forrige uke (rapportene for inneværende uke er ikke komplette)."""
    d = date.today() - timedelta(days=7)
    out = []
    for _ in range(n):
        y, w, _ = d.isocalendar()
        out.append((y, w))
        d -= timedelta(days=7)
    return out


def wk(y: int, w: int) -> str:
    return f"{y}-{w:02d}"


def spring_limit_applies(po: int | None, week: int) -> bool:
    if po is None:
        return False
    return (16 <= week <= 21) if po <= 7 else (21 <= week <= 26)


class BW:
    def __init__(self) -> None:
        import requests  # type: ignore

        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "ScaleRadar/1.0 (ScaleAQ)", "Accept": "application/json"})
        self.token_exp = 0.0

    def auth(self) -> None:
        if time.time() < self.token_exp - 60:
            return
        r = self.s.post(TOKEN_URL, data={
            "client_id": os.environ["BW_CLIENT_ID"],
            "client_secret": os.environ["BW_CLIENT_SECRET"],
            "scope": "api",
            "grant_type": "client_credentials",
        }, timeout=30)
        r.raise_for_status()
        tok = r.json()
        self.s.headers["Authorization"] = f"Bearer {tok['access_token']}"
        self.token_exp = time.time() + float(tok.get("expires_in", 3600))

    def get(self, path: str):
        self.auth()
        r = self.s.get(f"{API}{path}", timeout=60)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        time.sleep(PAUSE)
        return r.json()

    def post(self, path: str, body: dict):
        self.auth()
        r = self.s.post(f"{API}{path}", json=body, timeout=120)
        r.raise_for_status()
        time.sleep(PAUSE)
        return r.json()


# --------------------------------------------------------------------------- lagring per lokalitet
def load_loc(no: int) -> dict:
    p = LOC_DIR / f"{no}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"no": no, "series": [], "diseases": [], "weeks": {}}


def save_loc(doc: dict) -> None:
    doc["series"].sort(key=lambda s: (s[0], s[1]))
    # serien er fasiten; "weeks" er bare indeks for dedup under bygging
    doc.pop("weeks", None)
    (LOC_DIR / f"{doc['no']}.json").write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def num(v):
    """Tall direkte, eller objekt {average, averageOfPreviousWeek, trend} / {value}."""
    if v is None:
        return None
    if isinstance(v, dict):
        for k in ("average", "value", "avg", "count"):
            if v.get(k) is not None:
                v = v[k]
                break
        else:
            return None
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return None


def treat_flags(t: dict | None) -> list[int]:
    if not t:
        return [0, 0, 0, 0, 0, 0]
    def f(k):
        v = t.get(k)
        return 1 if (v is True or (isinstance(v, (int, float)) and v > 0) or (isinstance(v, list) and len(v) > 0)) else 0
    return [f("medicinalTreatments"), f("nonMedicinalTreatments"), f("bathTreatments"), f("mechanicalRemovalTreatment"), f("cleanerFishTreatment"), f("inFeedTreatments")]


def ingest_week(items: list[dict], y: int, w: int, docs: dict) -> int:
    """Legger ett ukesbilde inn i per-lokalitet-dokumentene (i minne)."""
    n = 0
    for it in items:
        loc = it.get("locality") or {}
        no = loc.get("no") or it.get("localityNo")
        if not no:
            continue
        no = int(no)
        doc = docs.get(no)
        if doc is None:
            doc = load_loc(no)
            doc["weeks"] = {f"{s[0]}-{s[1]:02d}": i for i, s in enumerate(doc["series"])}
            docs[no] = doc
        doc["name"] = loc.get("name") or doc.get("name")
        doc["isOnLand"] = bool(loc.get("isOnLand", doc.get("isOnLand", False)))
        mun = it.get("municipality")
        if isinstance(mun, dict):
            doc["municipality"] = mun.get("name") or doc.get("municipality")
            doc["municipalityNo"] = mun.get("no") or doc.get("municipalityNo")
        elif mun:
            doc["municipality"] = mun
        doc["hasSalmonoidLicense"] = bool(it.get("hasSalmonoidLicense", doc.get("hasSalmonoidLicense", False)))
        doc["isSlaughterHoldingCage"] = bool(it.get("isSlaughterHoldingCage", doc.get("isSlaughterHoldingCage", False)))
        g = it.get("geometry") or {}
        coords = g.get("coordinates") if isinstance(g, dict) else None
        if coords and len(coords) >= 2:
            doc["lon"], doc["lat"] = round(float(coords[0]), 5), round(float(coords[1]), 5)
        lr = it.get("liceReport") or {}
        row = [y, w,
               num(lr.get("adultFemaleLice")), num(lr.get("mobileLice")), num(lr.get("stationaryLice")), num(lr.get("seaTemperature")),
               1 if lr.get("isFallow") else 0, 1 if lr.get("hasReported") else 0] + treat_flags(it.get("liceTreatments"))
        key = wk(y, w)
        if key in doc["weeks"]:
            doc["series"][doc["weeks"][key]] = row
        else:
            doc["weeks"][key] = len(doc["series"])
            doc["series"].append(row)
        # ukesbildet gir sykdommer som navn (strenger); detaljkallet gir fulle objekter
        names = [d for d in (it.get("diseases") or []) if isinstance(d, str)]
        if (y, w) >= tuple(doc.get("latest_week") or (0, 0)):
            doc["latest_week"] = [y, w]
            doc["dis_latest"] = sorted(set(names))
        for d in it.get("diseases") or []:
            if not isinstance(d, dict):
                continue
            sig = (d.get("name"), d.get("subType"), d.get("suspicionDate") or d.get("diagnosisDate"))
            existing = next((x for x in doc["diseases"] if (x.get("name"), x.get("subType"), x.get("suspicionDate") or x.get("diagnosisDate")) == sig), None)
            rec = {k: d.get(k) for k in ("name", "subType", "status", "closureType", "suspicionDate", "diagnosisDate", "emptiedDate", "closureDate")}
            rec["lastSeenWeek"] = key
            if existing:
                existing.update(rec)
            else:
                doc["diseases"].append(rec)
        n += 1
    return n


# --------------------------------------------------------------------------- hovedløp
def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOC_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG.get("enabled", True):
        write_status("disabled")
        return 0
    if not os.environ.get("BW_CLIENT_ID") or not os.environ.get("BW_CLIENT_SECRET"):
        log("BW_CLIENT_ID/BW_CLIENT_SECRET mangler – hopper over")
        write_status("missing_secrets", message="Legg inn BW_CLIENT_ID og BW_CLIENT_SECRET som secrets i GitHub Actions")
        return 0

    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        state = {"weeks_done": [], "details": {}, "last_run": None}
    if state.get("last_run"):
        try:
            last = datetime.strptime(state["last_run"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - last).total_seconds() < MIN_HOURS_BETWEEN_RUNS * 3600 and not os.environ.get("BW_FORCE"):
                log(f"sist kjørt {state['last_run']} – venter (min {MIN_HOURS_BETWEEN_RUNS} t mellom kjøringer)")
                return 0
        except ValueError:
            pass

    bw = BW()
    try:
        bw.auth()
    except Exception as exc:  # noqa: BLE001
        log(f"autentisering feilet: {exc}")
        write_status("auth_failed", message=str(exc)[:200])
        return 0

    # ---- 1) ukesbilder
    wanted = iso_weeks_back(HISTORY_WEEKS)
    done = set(state.get("weeks_done", []))
    # nyeste uke hentes alltid på nytt (rapporter kommer etter hvert), ellers bare manglende
    todo = [(y, w) for (y, w) in wanted if wk(y, w) not in done or (y, w) == wanted[0]][:WEEKS_PER_RUN]
    docs: dict[int, dict] = {}
    fetched_weeks = 0
    for y, w in todo:
        try:
            items = bw.post(f"/v2/geodata/fishhealth/locality/{y}/{w}", {})
            if isinstance(items, str):
                items = json.loads(items)
            if isinstance(items, dict):
                items = items.get("localities") or items.get("items") or []
            items = [i for i in items if isinstance(i, dict)]
            if items and fetched_weeks == 0:
                sample = dict(items[0]); sample.pop("geometry", None)
                log(f"eksempel på ukesbilde-element: {json.dumps(sample, ensure_ascii=False)[:600]}")
            if not items:
                # noen varianter krever eksplisitte filterflagg
                items = bw.post(f"/v2/geodata/fishhealth/locality/{y}/{w}", {"allWithReport": True, "allWithoutReport": True, "allFallowWithoutReport": True, "allReportNotMandatoryWithoutReport": True, "allNotFallowWithoutReport": True})
                if isinstance(items, str):
                    items = json.loads(items)
                if isinstance(items, dict):
                    items = items.get("localities") or items.get("items") or []
                items = [i for i in items if isinstance(i, dict)]
            n = ingest_week(items, y, w, docs)
            done.add(wk(y, w))
            fetched_weeks += 1
            log(f"uke {wk(y, w)}: {n} lokaliteter")
        except Exception as exc:  # noqa: BLE001
            log(f"uke {wk(y, w)}: FEIL {exc}")
            break
    state["weeks_done"] = sorted(done, reverse=True)[:HISTORY_WEEKS + 10]

    # ---- 2) detaljer per lokalitet (trinnvis)
    # kandidater: alle lokaliteter vi har serie for, aktive (rapportert siste 26 uker) først
    all_nos = sorted({int(p.stem) for p in LOC_DIR.glob("*.json")} | set(docs.keys()))
    def doc_for(no: int) -> dict:
        return docs.get(no) or load_loc(no)
    latest_y, latest_w = wanted[0]
    details_state = state.setdefault("details", {})
    def needs_detail(no: int) -> int | None:
        d = details_state.get(str(no))
        if not d:
            return 0
        try:
            age = (datetime.now(timezone.utc) - datetime.strptime(d, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).days
        except ValueError:
            return 0
        return 1 if age > DETAIL_REFRESH_DAYS else None
    cand = []
    for no in all_nos:
        pri = needs_detail(no)
        if pri is None:
            continue
        doc = doc_for(no)
        recent = [s for s in doc["series"] if (s[0], s[1]) >= wanted[min(25, len(wanted) - 1)]]
        active = any(s[7] for s in recent)  # har rapportert siste halvår
        cand.append((pri, 0 if active else 1, no))
    cand.sort()
    fetched_details = 0
    for pri, _, no in cand[:DETAILS_PER_RUN]:
        try:
            det = bw.get(f"/v2/geodata/fishhealth/locality/{no}/{latest_y}/{latest_w}")
            if det is None:
                details_state[str(no)] = now_iso()
                continue
            doc = doc_for(no)
            docs[no] = doc
            if "weeks" not in doc:
                doc["weeks"] = {f"{s[0]}-{s[1]:02d}": i for i, s in enumerate(doc["series"])}
            acr = det.get("aquaCultureRegister") or {}
            pa = det.get("productionArea") or {}
            for d in det.get("diseases") or []:
                if not isinstance(d, dict):
                    continue
                sig = (d.get("name"), d.get("subType"), d.get("suspicionDate") or d.get("diagnosisDate"))
                existing = next((x for x in doc["diseases"] if (x.get("name"), x.get("subType"), x.get("suspicionDate") or x.get("diagnosisDate")) == sig), None)
                rec = {k: d.get(k) for k in ("name", "subType", "status", "closureType", "suspicionDate", "diagnosisDate", "emptiedDate", "closureDate")}
                if existing:
                    existing.update(rec)
                else:
                    doc["diseases"].append(rec)
            doc["detail"] = {
                "po": pa.get("id"), "po_name": pa.get("name"),
                "capacity": acr.get("capacity"), "unit": acr.get("unit"),
                "organizations": [{"name": o.get("name"), "orgNo": o.get("organizationNo")} for o in (acr.get("organizations") or [])],
                "species": acr.get("species"), "productionTypes": acr.get("productionTypes"), "purposes": acr.get("purposes"),
                "licenses": len(acr.get("licenses") or []) if isinstance(acr.get("licenses"), list) else acr.get("licenses"),
                "hasGreenLicense": acr.get("hasGreenLicense"), "pdZoneId": det.get("pdZoneId"),
                "placement": acr.get("placement"),
                "escapes": [{"date": e.get("date"), "species": e.get("species"), "count": e.get("count"), "estimated": e.get("estimatedCount"), "size": e.get("size")} for e in (det.get("farmedFishEscapes") or [])][:20],
                "fetched_at": now_iso(),
            }
            details_state[str(no)] = now_iso()
            fetched_details += 1
        except Exception as exc:  # noqa: BLE001
            log(f"detalj {no}: FEIL {exc}")
            break
    log(f"detaljer: hentet {fetched_details}, {len(cand)} kandidater")

    # ---- lagre dokumenter som er endret
    for no, doc in docs.items():
        save_loc(doc)

    # ---- 3) indeks og aggregat (leser alle filer)
    build_index_and_agg(wanted)
    state["last_run"] = now_iso()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    write_status("ok", weeks_fetched_now=fetched_weeks, weeks_total=len(done), details_fetched_now=fetched_details,
                 details_pending=max(0, len(cand) - fetched_details), localities=len(all_nos))
    return 0


def build_index_and_agg(wanted: list[tuple[int, int]]) -> None:
    latest = wanted[0]
    index = []
    agg: dict[str, dict] = {}       # uke -> nasjonalt + per PO
    cutoff_12w = wanted[min(11, len(wanted) - 1)]
    for p in sorted(LOC_DIR.glob("*.json")):
        doc = json.loads(p.read_text(encoding="utf-8"))
        series = doc.get("series") or []
        if not series:
            continue
        det = doc.get("detail") or {}
        po = det.get("po")
        last = series[-1]
        # siste uke med rapport
        last_rep = next((s for s in reversed(series) if s[7]), None)
        treat12 = sum(1 for s in series if (s[0], s[1]) >= cutoff_12w and any(s[8:14]))
        active_names = doc.get("dis_latest")
        if active_names is None:
            active_names = sorted({d.get("name") for d in doc.get("diseases") or [] if d.get("name") and not d.get("closureDate")})
        # trend: siste rapporterte AFL mot 4 uker før
        prev = None
        if last_rep:
            idx = series.index(last_rep)
            prev = series[idx - 4] if idx >= 4 else None
        afl = last_rep[2] if last_rep else None
        index.append({
            "no": doc["no"], "name": doc.get("name"), "mun": doc.get("municipality"), "lat": doc.get("lat"), "lon": doc.get("lon"),
            "land": doc.get("isOnLand", False), "slaughter": doc.get("isSlaughterHoldingCage", False), "salmonoid": doc.get("hasSalmonoidLicense", False),
            "po": po, "org": (det.get("organizations") or [{}])[0].get("name") if det.get("organizations") else None,
            "cap": det.get("capacity"), "species": det.get("species"),
            "week": wk(last[0], last[1]), "reported": bool(last[7]), "fallow": bool(last[6]),
            "afl": afl, "afl_prev4": prev[2] if prev else None, "mobile": last_rep[3] if last_rep else None, "temp": last_rep[5] if last_rep else None,
            "afl_week": wk(last_rep[0], last_rep[1]) if last_rep else None,
            "treat12": treat12, "dis": list(active_names or []),
            "escapes": len(det.get("escapes") or []),
        })
        # aggregat per uke (bare sjølokaliteter med laksefisk)
        if doc.get("isOnLand") or not doc.get("hasSalmonoidLicense", True):
            continue
        for s in series:
            key = wk(s[0], s[1])
            a = agg.setdefault(key, {"n": 0, "rep": 0, "afl_sum": 0.0, "afl_n": 0, "over": 0, "over_spring": 0, "fallow": 0, "treat_med": 0, "treat_nonmed": 0, "treat_any": 0, "temp_sum": 0.0, "temp_n": 0, "po": {}})
            pa = a["po"].setdefault(str(po or 0), {"n": 0, "rep": 0, "afl_sum": 0.0, "afl_n": 0, "over": 0, "fallow": 0, "treat_any": 0})
            for b in (a, pa):
                b["n"] += 1
                if s[7]:
                    b["rep"] += 1
                if s[6]:
                    b["fallow"] += 1
                if s[2] is not None:
                    b["afl_sum"] += float(s[2]); b["afl_n"] += 1
                    if float(s[2]) > LIMIT:
                        b["over"] += 1
                if any(s[8:14]):
                    b["treat_any"] += 1
            if s[2] is not None and spring_limit_applies(po, s[1]) and SPRING_LIMIT < float(s[2]) <= LIMIT:
                a["over_spring"] += 1
            if s[8]:
                a["treat_med"] += 1
            if s[9] or s[11]:
                a["treat_nonmed"] += 1
            if s[5] is not None:
                a["temp_sum"] += float(s[5]); a["temp_n"] += 1
    # ferdigstill aggregat
    weeks_out = []
    for key in sorted(agg):
        a = agg[key]
        row = {"week": key, "n": a["n"], "reported": a["rep"], "avg_afl": round(a["afl_sum"] / a["afl_n"], 3) if a["afl_n"] else None,
               "over": a["over"], "over_spring": a["over_spring"], "fallow": a["fallow"], "treat_med": a["treat_med"], "treat_nonmed": a["treat_nonmed"], "treat_any": a["treat_any"],
               "avg_temp": round(a["temp_sum"] / a["temp_n"], 1) if a["temp_n"] else None,
               "po": {k: {"n": v["n"], "reported": v["rep"], "avg_afl": round(v["afl_sum"] / v["afl_n"], 3) if v["afl_n"] else None, "over": v["over"], "fallow": v["fallow"], "treat_any": v["treat_any"]} for k, v in a["po"].items()}}
        weeks_out.append(row)
    INDEX_PATH.write_text(json.dumps({"updated_at": now_iso(), "latest_week": wk(*latest), "localities": index}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    AGG_PATH.write_text(json.dumps({"updated_at": now_iso(), "limit": LIMIT, "spring_limit": SPRING_LIMIT, "weeks": weeks_out}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"indeks: {len(index)} lokaliteter, aggregat: {len(weeks_out)} uker")


if __name__ == "__main__":
    sys.exit(main())
