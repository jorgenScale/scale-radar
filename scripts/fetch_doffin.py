#!/usr/bin/env python3
"""
Scale Radar – offentlige anbud fra Doffin (DFØ sin kunngjøringsdatabase).

Kjøres av GitHub Actions etter fetch_data.py. Krever secret DOFFIN_API_KEY (gratis nøkkel fra
Doffins API-portal, https://dof-notices-prod-api.developer.azure-api.net/). Uten nøkkel skriver
skriptet en statusfil og avslutter uten feil.

Prinsipp:
  1. Henting: GET {api_base}/search med issueDateFrom/To, sortert nyeste først, 100 treff per side.
     Hver kjøring henter de siste `incremental_days` dagene. Første gang (og til det er ferdig) fylles
     også `backfill_days` bakover, begrenset av `max_pages_per_run` per kjøring. Vi henter ALT og
     filtrerer lokalt – da styrer vi relevansen selv, og en endring i content/anbud.json slår inn
     på alt som er lagret uten ny henting.
  2. Scoring: regelbasert, forklarbar. Område-ord (fôrflåte, merd, fôringsanlegg …), havbruksord
     (akvakultur, oppdrett, settefisk …), kjent offentlig kjøper (Havforskningsinstituttet,
     fylkeskommunene …), CPV-koder og negative ord (bilflåte, redningsflåte …) gir poeng. Terskel
     for «relevant» er `threshold`; alt over `store_min_score` lagres som kandidat så filteret
     kan justeres med fasit i hånden.
  3. Lagring: data/anbud/items.json (alle lagrede kunngjøringer med score og begrunnelse),
     data/anbud/state.json (hvor langt tilbake vi har hentet), data/anbud/status.json.
     RSS: data/feeds/anbud.xml (alle relevante) og data/feeds/anbud/<område>.xml.

Lokal test uten nøkkel: DOFFIN_FIXTURE=sti/til/svar.json python scripts/fetch_doffin.py
(fila er ett API-svar: {"numHitsTotal": n, "hits": [...]}).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "content" / "anbud.json").read_text(encoding="utf-8"))
SITE = json.loads((ROOT / "content" / "config.json").read_text(encoding="utf-8")).get("site", {})
SITE_URL = SITE.get("url", "https://jorgenscale.github.io/scale-radar/").rstrip("/") + "/"

OUT_DIR = ROOT / "data" / "anbud"
ITEMS_PATH = OUT_DIR / "items.json"
STATE_PATH = OUT_DIR / "state.json"
STATUS_PATH = OUT_DIR / "status.json"
FEEDS_DIR = ROOT / "data" / "feeds"
AREA_FEED_DIR = FEEDS_DIR / "anbud"

API_BASES = [b.rstrip("/") for b in (CFG.get("api_bases") or ([CFG["api_base"]] if CFG.get("api_base") else ["https://api.doffin.no/public/v2", "https://betaapi.doffin.no/public/v2"]))]
NOTICE_URL = CFG.get("notice_url", "https://www.doffin.no/notices/")
PAGE_SIZE = int(CFG.get("page_size", 100))
MAX_PAGES = int(CFG.get("max_pages_per_run", 60))
PAUSE = float(CFG.get("pause_seconds", 0.4))
BACKFILL_DAYS = int(CFG.get("backfill_days", 120))
SLICE_DAYS = max(1, int(CFG.get("backfill_slice_days", 4)))
API_CAP = 1000          # Doffin returnerer aldri mer enn 1 000 treff per søk (numHitsTotal stopper på 1000)
BACKFILL_VERSION = 2    # bump når hentelogikken endres slik at historikken må hentes på nytt
INCREMENTAL_DAYS = int(CFG.get("incremental_days", 3))
THRESHOLD = int(CFG.get("threshold", 40))
STORE_MIN = int(CFG.get("store_min_score", 20))
KEEP_DAYS = int(CFG.get("keep_days", 365))
KEEP_AFTER_DEADLINE = int(CFG.get("keep_days_after_deadline", 90))
DESC_MAX = int(CFG.get("description_max_chars", 1500))
W = dict({"area_heading": 45, "area_text": 30, "weak_area_heading": 20, "weak_area_text": 12,
          "core_heading": 35, "core_text": 20, "buyer": 25, "cpv": 15, "negative": -50},
         **{k: v for k, v in CFG.get("weights", {}).items() if not k.startswith("_")})

TYPE_LABEL = {"COMPETITION": "Konkurranse", "ANNOUNCEMENT_OF_COMPETITION": "Konkurranse",
              "RESULT": "Tildeling", "ANNOUNCEMENT_OF_CONCLUSION_OF_CONTRACT": "Kontraktsinngåelse",
              "PLANNING": "Veiledende", "PRE_ANNOUNCEMENT": "Veiledende", "ADVISORY_NOTICE": "Veiledende",
              "ANNOUNCEMENT_OF_INTENT": "Intensjon", "DYNAMIC_PURCHASING_SCHEME": "Dynamisk innkjøpsordning",
              "QUALIFICATION_SCHEME": "Kvalifikasjonsordning", "CHANGE_OF_CONCLUSION_OF_CONTRACT": "Kontraktsendring",
              "CANCELLED_OR_MISSING_CONCLUSION_OF_CONTRACT": "Avlyst", "NOTICE_ON_BUYER_PROFILE": "Kjøperprofil"}
TYPE_KIND = {"COMPETITION": "competition", "ANNOUNCEMENT_OF_COMPETITION": "competition",
             "RESULT": "result", "ANNOUNCEMENT_OF_CONCLUSION_OF_CONTRACT": "result", "ANNOUNCEMENT_OF_INTENT": "result",
             "PLANNING": "planning", "PRE_ANNOUNCEMENT": "planning", "ADVISORY_NOTICE": "planning"}


def log(msg: str) -> None:
    print(f"[anbud] {msg}", file=sys.stderr)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_status(status: str, **extra) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(dict({"status": status, "updated_at": now_iso()}, **extra), ensure_ascii=False), encoding="utf-8")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


# --------------------------------------------------------------------------- tekst og scoring
def norm(s: str) -> str:
    """Småbokstaver. ô beholdes med vilje: «fôring» (fisk) og «foring» (rør) er ulike ord, og «fôrsystem» er ikke
    «forsystem» (IT). Ordlistene oppgir begge skrivemåter der det er trygt."""
    return (s or "").lower()


LETTER = "a-zæøåäöüô"


def term_rx(term: str, inflect: int = 3, compound: bool = False) -> re.Pattern:
    """Mønster for ett søkeord: må starte på ordgrense (ikke inni «bilflåte»), tillater inntil
    `inflect` bokstavers bøyning etter ordet (flåte → flåten/flåter) og krever ordslutt deretter
    («flåtestyring» treffer ikke). Med compound=True godtas sammensetninger som fortsetter ordet
    (oppdrett → oppdrettsutstyr, havbruk → havbruksnæring) – brukes for havbruksordene, som sjelden
    inngår i urelaterte ord. Mellomrom i ordet matcher også bindestrek og flere mellomrom."""
    t = norm(term).strip()
    parts = [re.escape(p) for p in re.split(r"\s+", t) if p]
    body = r"[\s\-]+".join(parts)
    tail = rf"[{LETTER}]*" if compound else rf"[{LETTER}]{{0,{inflect}}}(?![{LETTER}])"
    return re.compile(rf"(?<![{LETTER}0-9]){body}{tail}", re.IGNORECASE)


def compile_terms(terms: list[str], inflect: int = 3, compound: bool = False) -> list[tuple[str, re.Pattern]]:
    """Ett mønster per ulikt ord – like ord telles én gang."""
    out, seen = [], set()
    for t in terms:
        rx = term_rx(t, inflect, compound)
        if rx.pattern in seen:
            continue
        seen.add(rx.pattern)
        out.append((t, rx))
    return out


def compile_rules() -> dict:
    rules = {"areas": {}, "core": [], "buyers": [], "negative": [], "cpv": {}}
    for key, a in (CFG.get("areas") or {}).items():
        if key.startswith("_"):
            continue
        rules["areas"][key] = {
            "label": a.get("label", key),
            "strong": compile_terms(a.get("strong", [])),
            "weak": compile_terms(a.get("weak", [])),
        }
    rules["core"] = compile_terms((CFG.get("core") or {}).get("terms", []), compound=bool((CFG.get("core") or {}).get("compound", True)))
    rules["buyers"] = compile_terms((CFG.get("buyers") or {}).get("names", []), inflect=0)
    rules["negative"] = compile_terms((CFG.get("negative") or {}).get("terms", []))
    rules["cpv"] = {k: v for k, v in ((CFG.get("cpv") or {}).get("codes") or {}).items()}
    return rules


RULES = compile_rules()


def score_item(it: dict) -> tuple[int, list[str], list[dict]]:
    """Returnerer (score 0–100, områder, begrunnelser). Hvert søkeord teller én gang per felt."""
    heading = norm(it.get("heading") or "")
    text = norm(" ".join(filter(None, [it.get("description") or ""] + [f"{l.get('heading','')} {l.get('description','')}" for l in it.get("lots") or []])))
    buyer = " ".join(it.get("buyers") or [])
    reasons: list[dict] = []
    score = 0
    areas: list[str] = []

    for key, a in RULES["areas"].items():
        hit_area = False
        n_area = 0
        for strength, pairs in (("strong", a["strong"]), ("weak", a["weak"])):
            for term, rx in pairs:
                if n_area >= 3:
                    break
                if rx.search(heading):
                    pts = W["area_heading"] if strength == "strong" else W["weak_area_heading"]
                    reasons.append({"kind": "area", "area": key, "term": term, "where": "tittel", "pts": pts}); score += pts; hit_area = True; n_area += 1
                elif rx.search(text):
                    pts = W["area_text"] if strength == "strong" else W["weak_area_text"]
                    reasons.append({"kind": "area", "area": key, "term": term, "where": "beskrivelse", "pts": pts}); score += pts; hit_area = True; n_area += 1
        if hit_area:
            areas.append(key)

    core_hits = 0
    for term, rx in RULES["core"]:
        if core_hits >= 3:
            break
        if rx.search(heading):
            reasons.append({"kind": "core", "term": term, "where": "tittel", "pts": W["core_heading"]}); score += W["core_heading"]; core_hits += 1
        elif rx.search(text):
            reasons.append({"kind": "core", "term": term, "where": "beskrivelse", "pts": W["core_text"]}); score += W["core_text"]; core_hits += 1

    for name, rx in RULES["buyers"]:
        if rx.search(norm(buyer)):
            reasons.append({"kind": "buyer", "term": name, "where": "oppdragsgiver", "pts": W["buyer"]}); score += W["buyer"]
            break

    for code in it.get("cpv") or []:
        c = str(code).split("-")[0].strip()
        label = RULES["cpv"].get(c)
        if label:
            reasons.append({"kind": "cpv", "term": f"{c} {label}", "where": "CPV", "pts": W["cpv"]}); score += W["cpv"]
            break

    for term, rx in RULES["negative"]:
        if rx.search(heading) or rx.search(text):
            reasons.append({"kind": "negative", "term": term, "where": "tittel/beskrivelse", "pts": W["negative"]}); score += W["negative"]
            break

    return max(0, min(100, score)), areas, reasons


# --------------------------------------------------------------------------- Doffin API
class DoffinClient:
    """Doffin-klient som finner ut hvilken gateway og hvilke parametere API-et godtar.

    Doffin har flere gatewayer (api.doffin.no er prod, betaapi.doffin.no er beta). En nøkkel fra prod-portalen gir
    401 på beta. Et 400-svar betyr derimot at nøkkelen er godtatt, men at forespørselen ble avvist – da prøver vi noen
    kjente varianter (1-indeksert side, dato som tidspunkt, uten datofilter) og husker den som virker. Avvises alt,
    tas API-ets egen feilmelding med i statusfilen så den kan leses på siden."""

    def __init__(self, key: str | None, fixture: Path | None = None):
        self.key = key
        self.fixture = fixture
        self.calls = 0
        self.base: str | None = None
        self.page_offset = 0          # 0 = sider telles fra 0, 1 = fra 1
        self.date_mode = "date"       # "date" = YYYY-MM-DD, "datetime" = ...T00:00:00Z, "none" = ingen datofilter
        self.page_size = PAGE_SIZE
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json", "User-Agent": "ScaleRadar/1.0 (+github.com/jorgenScale/scale-radar)"})
        if key:
            self.s.headers["Ocp-Apim-Subscription-Key"] = key

    def _params(self, date_from: date, date_to: date, page: int, offset: int, date_mode: str, size: int) -> dict:
        p = {"numHitsPerPage": size, "page": page + offset, "sortBy": "PUBLICATION_DATE_DESC"}
        if date_mode == "date":
            p["issueDateFrom"], p["issueDateTo"] = date_from.isoformat(), date_to.isoformat()
        elif date_mode == "datetime":
            p["issueDateFrom"], p["issueDateTo"] = date_from.isoformat() + "T00:00:00Z", date_to.isoformat() + "T23:59:59Z"
        return p

    def _get(self, base: str, params: dict):
        self.calls += 1
        return self.s.get(f"{base}/search", params=params, timeout=40)

    @staticmethod
    def _host(base: str) -> str:
        return base.split("//", 1)[-1].split("/", 1)[0]

    def search(self, date_from: date, date_to: date, page: int) -> dict:
        if self.fixture:
            d = load_json(self.fixture, {"numHitsTotal": 0, "hits": []})
            if page > 0:
                return {"numHitsTotal": d.get("numHitsTotal", 0), "hits": []}
            return d
        bases = [self.base] if self.base else API_BASES
        denied: list[str] = []
        for base in bases:
            host = self._host(base)
            params = self._params(date_from, date_to, page, self.page_offset, self.date_mode, self.page_size)
            r = None
            for attempt in range(4):
                try:
                    r = self._get(base, params)
                except requests.RequestException as exc:
                    denied.append(f"{host}: {type(exc).__name__}"); r = None; break
                if r.status_code == 429 or r.status_code >= 500:
                    wait = min(30, 2 ** attempt * 2)
                    log(f"HTTP {r.status_code} fra {host} – venter {wait}s"); time.sleep(wait); continue
                break
            if r is None:
                continue
            if r.status_code in (401, 403):
                denied.append(f"{host}: HTTP {r.status_code}")
                time.sleep(PAUSE)
                continue
            if r.status_code == 200:
                if not self.base:
                    log(f"bruker {base}")
                self.base = base
                time.sleep(PAUSE)
                return r.json()
            if r.status_code == 400 and not self.base:
                # nøkkelen er godtatt – finn parametervarianten API-et vil ha
                first_msg = r.text.strip()[:220]
                variants = [(1, "date", PAGE_SIZE), (0, "datetime", PAGE_SIZE), (1, "datetime", PAGE_SIZE),
                            (0, "none", PAGE_SIZE), (1, "none", PAGE_SIZE), (0, "date", 50), (0, "date", 20)]
                for offset, mode, size in variants:
                    time.sleep(PAUSE)
                    r2 = self._get(base, self._params(date_from, date_to, page, offset, mode, size))
                    if r2.status_code == 200:
                        self.base, self.page_offset, self.date_mode, self.page_size = base, offset, mode, size
                        log(f"bruker {base} (sider fra {offset}, dato={mode}, {size} per side)")
                        return r2.json()
                raise RuntimeError(f"HTTP 400 fra {host}: {first_msg}")
            raise RuntimeError(f"HTTP {r.status_code} fra {host}: {r.text.strip()[:200]}")
        raise PermissionError("; ".join(denied) or "ingen gateway svarte")


def normalize_hit(h: dict) -> dict:
    buyers = [b.get("name", "") for b in (h.get("buyer") or []) if isinstance(b, dict) and b.get("name")]
    ev = h.get("estimatedValue") or {}
    winners = sorted({w.get("name", "") for l in (h.get("lots") or []) for w in (l.get("winner") or []) if isinstance(w, dict) and w.get("name")})
    desc = (h.get("description") or "").strip()
    typ = (h.get("type") or (h.get("allTypes") or ["?"])[0] or "?").upper()
    return {
        "id": h.get("id"),
        "url": NOTICE_URL + str(h.get("id", "")),
        "heading": (h.get("heading") or "").strip(),
        "buyers": buyers,
        "type": typ,
        "kind": TYPE_KIND.get(typ, "other"),
        "type_label": TYPE_LABEL.get(typ, typ.replace("_", " ").title()),
        "all_types": h.get("allTypes") or [],
        "status": h.get("status"),
        "issue_date": (h.get("issueDate") or "")[:10] or None,
        "publication_date": (h.get("publicationDate") or "")[:10] or None,
        "deadline": h.get("deadline"),
        "value": ev.get("amount"),
        "currency": ev.get("currencyCode"),
        "cpv": h.get("cpvCodes") or [],
        "location": h.get("locationId") or [],
        "received_tenders": h.get("receivedTenders"),
        "winners": winners,
        "lots": [{"heading": l.get("heading", ""), "description": (l.get("description") or "")[:400]} for l in (h.get("lots") or [])][:10],
        "description": desc[:DESC_MAX] + ("…" if len(desc) > DESC_MAX else ""),
    }


def sweep(client: DoffinClient, date_from: date, date_to: date, budget: int, label: str, start_page: int = 0) -> tuple[list[dict], bool, date | None, int, int | None]:
    """Henter sider for et datointervall til det er tomt eller budsjettet er brukt.
    Returnerer (treff, ferdig, eldste issueDate sett, neste side, numHitsTotal). Når API-et ikke tar datofilter
    (client.date_mode == "none") leses det nyeste først og stoppes når vi har passert date_from."""
    hits: list[dict] = []
    oldest: date | None = None
    page = start_page
    total = None
    fetched = 0
    while page < start_page + budget:
        d = client.search(date_from, date_to, page)
        page_hits = d.get("hits") or []
        if total is None and client.date_mode != "none":
            total = d.get("numHitsAccessible") or d.get("numHitsTotal")
        fetched += len(page_hits)
        for h in page_hits:
            it = normalize_hit(h)
            if not it["id"]:
                continue
            hits.append(it)
            try:
                dd = date.fromisoformat(it["issue_date"] or it["publication_date"])
                oldest = dd if oldest is None or dd < oldest else oldest
            except Exception:  # noqa: BLE001
                pass
        # stopp på tom side eller når vi har hentet alt API-et sier finnes – ikke på sidestørrelse, i tilfelle API-et
        # gir færre per side enn vi ba om
        if client.date_mode == "none" and oldest is not None and oldest < date_from:
            log(f"{label}: {len(hits)} treff ned til {oldest} (datokutt lokalt, side {start_page}–{page})")
            return hits, True, oldest, page + 1, total
        if not page_hits or (total is not None and fetched >= int(total)):
            log(f"{label}: {len(hits)} treff på {page - start_page + 1} side(r), {date_from}–{date_to}, totalt {total}")
            return hits, True, oldest, page + 1, total
        page += 1
    log(f"{label}: budsjett brukt ({budget} sider), {len(hits)} treff, eldste {oldest} – fortsetter neste kjøring")
    return hits, False, oldest, page, total


# --------------------------------------------------------------------------- RSS
def rfc822(iso: str | None) -> str:
    try:
        dt = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        dt = datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def x(v) -> str:
    from xml.sax.saxutils import escape
    return escape(str(v if v is not None else ""))


def fmt_value(it: dict) -> str:
    v = it.get("value")
    if not v:
        return ""
    cur = it.get("currency") or "NOK"
    if v >= 1e6:
        return f"{v / 1e6:,.1f} mill. {cur}".replace(",", " ").replace(".", ",")
    return f"{v:,.0f} {cur}".replace(",", " ")


def rss(title: str, desc: str, link: str, items: list[dict], area_labels: dict) -> str:
    out = []
    for it in items[:100]:
        areas = ", ".join(area_labels.get(a, a) for a in it.get("areas") or []) or "Havbruk generelt"
        why = "; ".join(f"{r['term']} ({r['where']})" for r in it.get("reasons") or [] if r["kind"] != "negative")
        body = (f"<b>{x(it.get('type_label'))}</b> fra {x(', '.join(it.get('buyers') or []) or 'ukjent oppdragsgiver')}<br>"
                f"Område: {x(areas)} · Relevans {it.get('score')}/100<br>")
        if it.get("deadline"):
            body += f"Frist: {x(it['deadline'][:16].replace('T', ' '))}<br>"
        if fmt_value(it):
            body += f"Anslått verdi: {x(fmt_value(it))}<br>"
        if it.get("winners"):
            body += f"Vinner: {x(', '.join(it['winners']))}<br>"
        if why:
            body += f"Treff på: {x(why)}<br>"
        if it.get("description"):
            body += "<br>" + x(it["description"][:600])
        out.append(
            "<item>"
            f"<title>{x(it.get('type_label'))}: {x(it.get('heading'))} – {x((it.get('buyers') or ['?'])[0])}</title>"
            f"<link>{x(it.get('url'))}</link>"
            f"<guid isPermaLink=\"false\">doffin-{x(it['id'])}</guid>"
            f"<pubDate>{rfc822(it.get('publication_date') or it.get('issue_date'))}</pubDate>"
            f"<description><![CDATA[{body}]]></description>"
            "</item>"
        )
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
            f"<title>{x(title)}</title><link>{x(link)}</link><description>{x(desc)}</description>"
            f"<language>nb</language><lastBuildDate>{rfc822(now_iso())}</lastBuildDate>"
            + "".join(out) + "</channel></rss>\n")


def write_feeds(items: list[dict], area_labels: dict) -> dict:
    FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    AREA_FEED_DIR.mkdir(parents=True, exist_ok=True)
    relevant = [i for i in items if i["score"] >= THRESHOLD]
    relevant.sort(key=lambda i: (i.get("publication_date") or i.get("issue_date") or ""), reverse=True)
    link = SITE_URL + "#anbud"
    (FEEDS_DIR / "anbud.xml").write_text(
        rss("Scale Radar – offentlige anbud (Doffin)", "Kunngjøringer på Doffin som er relevante for ScaleAQ: konkurranser, tildelinger og veiledende kunngjøringer innen havbruk",
            link, relevant, area_labels), encoding="utf-8")
    for key, label in area_labels.items():
        mine = [i for i in relevant if key in (i.get("areas") or [])]
        (AREA_FEED_DIR / f"{key}.xml").write_text(
            rss(f"Scale Radar – anbud: {label}", f"Doffin-kunngjøringer innen {label.lower()}", link, mine, area_labels), encoding="utf-8")
    log(f"rss: anbud.xml ({len(relevant)}) + {len(area_labels)} områdefeeder")
    return {"all": SITE_URL + "data/feeds/anbud.xml", "area_base": SITE_URL + "data/feeds/anbud/"}


# --------------------------------------------------------------------------- main
def main() -> int:
    if not CFG.get("enabled", True):
        write_status("disabled"); return 0
    key = os.environ.get("DOFFIN_API_KEY", "").strip()
    fixture = Path(os.environ["DOFFIN_FIXTURE"]) if os.environ.get("DOFFIN_FIXTURE") else None
    if not key and not fixture:
        log("DOFFIN_API_KEY mangler – hopper over")
        write_status("missing_secrets", message="Secret DOFFIN_API_KEY er ikke satt i GitHub Actions")
        return 0

    today = date.today()
    prev = load_json(ITEMS_PATH, {})
    state = load_json(STATE_PATH, {})
    stored: dict[str, dict] = {i["id"]: i for i in prev.get("items", []) if i.get("id")}
    client = DoffinClient(key, fixture)
    area_labels = {k: a["label"] for k, a in RULES["areas"].items()}

    new_hits: list[dict] = []
    pages_left = MAX_PAGES

    def fetch_range(d_from: date, d_to: date, label: str) -> bool:
        """Henter et datointervall komplett. Treffer intervallet API-ets tak på 1 000, deles det i to.
        Returnerer False hvis sidebudsjettet tok slutt før intervallet var ferdig."""
        nonlocal pages_left
        if pages_left <= 0:
            return False
        hits, done, oldest, nxt, total = sweep(client, d_from, d_to, pages_left, label)
        pages_left -= max(1, nxt)
        capped = total is not None and int(total) >= API_CAP
        if capped and (d_to - d_from).days >= 1:
            mid = d_from + (d_to - d_from) // 2
            log(f"{label}: {d_from}–{d_to} traff taket på {API_CAP} – deler i to")
            return fetch_range(mid + timedelta(days=1), d_to, label) and fetch_range(d_from, mid, label)
        new_hits.extend(hits)
        if capped:
            log(f"{label}: {d_from} alene har over {API_CAP} kunngjøringer – tar de nyeste {len(hits)}")
        return done

    try:
        # 1) siste dager – alltid
        inc_from = today - timedelta(days=INCREMENTAL_DAYS)
        if not fetch_range(inc_from, today, "siste dager"):
            log("siste dager: budsjettet tok slutt – resten tas neste time")

        # 2) bakoverfylling i biter til vi har dekket backfill_days
        target_from = today - timedelta(days=BACKFILL_DAYS)
        if state.get("backfill_days") != BACKFILL_DAYS or state.get("backfill_version") != BACKFILL_VERSION:
            state = {"backfill_days": BACKFILL_DAYS, "backfill_version": BACKFILL_VERSION, "backfill_complete": False}
        if client.date_mode == "none":
            # API-et tok ikke datofilter: les nyeste først, side for side, til vi har passert target_from
            if not state.get("backfill_complete"):
                start = int(state.get("resume_page") or 0)
                hits, done, oldest, nxt, _ = sweep(client, target_from, today, max(1, pages_left), "bakoverfylling", start_page=start)
                new_hits.extend(hits)
                if done:
                    state.update({"backfill_complete": True, "covered_from": target_from.isoformat()}); state.pop("resume_page", None)
                else:
                    state.update({"resume_page": nxt, "covered_from": (oldest or today).isoformat()})
        elif not state.get("backfill_complete"):
            resume_to = date.fromisoformat(state["resume_to"]) if state.get("resume_to") else inc_from - timedelta(days=1)
            while resume_to >= target_from and pages_left > 0:
                slice_from = max(target_from, resume_to - timedelta(days=SLICE_DAYS - 1))
                if not fetch_range(slice_from, resume_to, "bakoverfylling"):
                    break
                resume_to = slice_from - timedelta(days=1)
                state.update({"resume_to": resume_to.isoformat(), "covered_from": slice_from.isoformat()})
            if resume_to < target_from:
                state.update({"backfill_complete": True, "covered_from": target_from.isoformat()}); state.pop("resume_to", None)
            else:
                log(f"bakoverfylling: dekket til {state.get('covered_from')} – fortsetter neste kjøring")
    except PermissionError as exc:
        log(f"autentisering feilet: {exc}")
        write_status("auth_failed", message=str(exc)[:240], hint="401 på alle adresser betyr som regel at abonnementet i Doffins API-portal ikke er aktivt ennå (står som Submitted i stedet for Active).")
        return 0
    except Exception as exc:  # noqa: BLE001
        log(f"henting feilet: {exc}")
        write_status("error", message=str(exc)[:200])
        # vi fortsetter med det vi har lagret, slik at nye regler fortsatt scorer gamle treff
        if not stored:
            return 0

    # 3) slå sammen, score, beskjær
    for it in new_hits:
        old = stored.get(it["id"])
        if old:
            it["first_seen"] = old.get("first_seen") or now_iso()
        else:
            it["first_seen"] = now_iso()
        stored[it["id"]] = it

    items: list[dict] = []
    keep_cut = (today - timedelta(days=KEEP_DAYS)).isoformat()
    dl_cut = (today - timedelta(days=KEEP_AFTER_DEADLINE)).isoformat()
    for it in stored.values():
        it["url"] = NOTICE_URL + str(it["id"])   # bygges på nytt hver gang, så en rettet adresse slår inn på alt lagret
        score, areas, reasons = score_item(it)
        it["score"], it["areas"], it["reasons"] = score, areas, reasons
        pub = it.get("publication_date") or it.get("issue_date") or ""
        if pub and pub < keep_cut:
            continue
        if it.get("deadline") and it["deadline"][:10] < dl_cut and it.get("kind") != "result":
            continue
        if score >= STORE_MIN:
            items.append(it)
    items.sort(key=lambda i: (i.get("publication_date") or i.get("issue_date") or ""), reverse=True)

    relevant = [i for i in items if i["score"] >= THRESHOLD]
    active = [i for i in relevant if i.get("kind") == "competition" and i.get("deadline") and i["deadline"][:10] >= today.isoformat()]
    feeds = write_feeds(items, area_labels)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ITEMS_PATH.write_text(json.dumps({
        "updated_at": now_iso(),
        "source": "Doffin – Database for offentlige anskaffelser (DFØ), Public API v2",
        "source_url": "https://www.doffin.no/",
        "window": {"from": state.get("covered_from"), "to": today.isoformat(), "backfill_complete": bool(state.get("backfill_complete"))},
        "threshold": THRESHOLD,
        "store_min_score": STORE_MIN,
        "areas": area_labels,
        "feeds": feeds,
        "counts": {"stored": len(items), "relevant": len(relevant), "active": len(active),
                   "candidates": len(items) - len(relevant), "fetched_this_run": len(new_hits), "api_calls": client.calls},
        "items": items,
    }, ensure_ascii=False, indent=0), encoding="utf-8")
    STATE_PATH.write_text(json.dumps(dict(state, last_run=now_iso()), ensure_ascii=False), encoding="utf-8")
    write_status("ok", relevant=len(relevant), active=len(active), fetched=len(new_hits), api_calls=client.calls,
                 backfill_complete=bool(state.get("backfill_complete")))
    log(f"ferdig: {len(new_hits)} hentet, {len(items)} lagret, {len(relevant)} relevante, {len(active)} aktive konkurranser, {client.calls} API-kall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
