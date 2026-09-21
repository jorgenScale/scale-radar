#!/usr/bin/env python3
"""
Scale Radar – e-postvarsler for lokalitetssøknader.

Leser content/subscriptions.json og data/changes.json, og sender én e-post per
abonnement med hendelsene som er nye siden forrige utsending. Kjøres av GitHub
Actions etter fetch_data.py – men BARE når disse secrets er satt i repoet:

    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM

Uten secrets gjør skriptet ingenting (RSS-feedene i data/feeds/ fungerer uansett).
Tilstand (hva som er sendt) lagres i data/notify_state.json.
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUBS = ROOT / "content" / "subscriptions.json"
CHANGES = ROOT / "data" / "changes.json"
STATE = ROOT / "data" / "notify_state.json"


def log(msg: str) -> None:
    print(f"[notify] {msg}", file=sys.stderr)


def main() -> int:
    host = os.environ.get("SMTP_HOST")
    if not host:
        log("SMTP_HOST ikke satt – hopper over e-post (RSS-feedene er oppdatert uansett)")
        return 0
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASS", "")
    sender = os.environ.get("MAIL_FROM", user)

    try:
        subs = json.loads(SUBS.read_text(encoding="utf-8")).get("subscriptions", [])
    except Exception as exc:  # noqa: BLE001
        log(f"kunne ikke lese {SUBS}: {exc}")
        return 0
    try:
        events = json.loads(CHANGES.read_text(encoding="utf-8")).get("events", [])
    except Exception:  # noqa: BLE001
        events = []
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        state = {}

    if not events or not subs:
        log("ingen hendelser eller ingen abonnement")
        return 0

    site = json.loads((ROOT / "content" / "config.json").read_text(encoding="utf-8")).get("site", {}).get("url", "")
    sent_any = False
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for sub in subs:
        name = sub.get("name") or ",".join(sub.get("match", []))
        matches = [m.lower() for m in sub.get("match", [])]
        types = set(sub.get("types") or [])
        emails = [e for e in sub.get("emails", []) if "@" in e]
        if not matches or not emails:
            continue
        last = state.get(name, {}).get("last_sent_at", "")
        mine = [
            e for e in events
            if (e.get("at") or "") > last
            and any(m in (e.get("applicant") or "").lower() for m in matches)
            and (not types or e.get("type_key") in types)
        ]
        if not mine:
            continue
        mine.sort(key=lambda e: e.get("at") or "", reverse=True)

        lines = []
        for e in mine:
            where = ", ".join(filter(None, [e.get("municipality"), e.get("county")]))
            mtb = f" · MTB {e['mtb_tonn']} tonn" if e.get("mtb_tonn") else ""
            lines.append(f"• {e.get('applicant')} – {e.get('title')} ({e.get('type')}, {where})\n  {e.get('text')}{mtb}\n  {e.get('url') or ''}")
        body = (
            f"Scale Radar: {len(mine)} endring(er) for {name} i Fiskeridirektoratets lokalitetssøknader\n\n"
            + "\n\n".join(lines)
            + f"\n\nSe kart og liste: {site}\n"
            "Du får denne fordi du står i content/subscriptions.json i Scale Radar-repoet."
        )
        msg = EmailMessage()
        msg["Subject"] = f"Scale Radar: {len(mine)} endring(er) – {name}"
        msg["From"] = sender
        msg["To"] = ", ".join(emails)
        msg.set_content(body)
        try:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.starttls()
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
            state[name] = {"last_sent_at": mine[0]["at"], "sent_at": now_iso, "count": len(mine)}
            sent_any = True
            log(f"sendte {len(mine)} hendelser for {name} til {len(emails)} mottakere")
        except Exception as exc:  # noqa: BLE001
            log(f"sending for {name} feilet: {exc}")

    if sent_any:
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
