# Scale Radar

Internt dashbord for ScaleAQ: nyheter fra næringen, laksepris (SSB), børsnoterte oppdrettere,
konsesjoner/kapasitet, og en egen side med Fiskeridirektoratets lokalitetssøknader (sjø og land) med
status, kart og filtre – pluss en sammenleggbar beskrivelse av saksgangen.

Siden er én statisk HTML-fil (`index.html`) som leser `data/data.json`. Dataene oppdateres av
`scripts/fetch_data.py`, som GitHub Actions kjører hver time.

## Oppsett (ca. 15 minutter, gjøres én gang)

1. **Opprett et repo på GitHub** (f.eks. `scaleaq/scale-radar`). Offentlig repo gir gratis GitHub Pages;
   privat repo krever GitHub Pro/Team/Enterprise for Pages.
2. **Last opp innholdet i denne mappen** til repoet (dra og slipp i nettleseren, eller `git push`).
3. **Slå på Actions-skriving:** Settings → Actions → General → Workflow permissions → *Read and write permissions* → Save.
4. **Slå på Pages:** Settings → Pages → Source: *Deploy from a branch* → Branch `main`, folder `/ (root)` → Save.
   Adressen blir `https://<org>.github.io/scale-radar/`.
5. **Kjør første oppdatering manuelt:** Actions → *Oppdater Scale Radar-data* → *Run workflow*.
   Etter 1–2 minutter ligger ferske data i `data/data.json`, og siden viser «Hentes automatisk hver time».

Deretter kjører oppdateringen selv, 17 minutter over hver hele time (UTC).

## Vedlikehold

| Vil du …                                   | Rediger                       |
|--------------------------------------------|-------------------------------|
| legge til/fjerne nyhetskilder (RSS)        | `content/config.json` → `feeds` |
| endre hvilke aksjer som vises              | `content/config.json` → `stock_groups` (Yahoo-symbol, f.eks. `MOWI.OL`) |
| oppdatere trafikklys, landbasert-status, saker vi følger | `content/konsesjoner.json` |
| endre kilde/søknadstyper for lokalitetssøknader | `content/config.json` → `applications` (`apptypes` per fane) |
| e-postabonnement på søkere (sentral utsending) | `content/subscriptions.json` + SMTP-secrets (se under) |
| auksjoner og fastpris – eldre runder, 2026-status, kilder | `content/auksjoner.json` (skriptet legger Fiskeridirektoratets tabeller oppå) |
| justere henting av detaljsider (antall per kjøring, oppfrisking) | `content/config.json` → `applications.details` |
| justere saksgang fra eInnsyn (antall per kjøring, oppfrisking) | `content/config.json` → `applications.einnsyn` |
| endre utseende, tekster, søknadsprosessen  | `index.html`                  |

Skriptet er tolerant: feiler én kilde, beholdes forrige verdi for den kilden og status vises på siden.
Ingen tall anslås – manglende kurser vises som strek.

## Kjente punkter etter første kjøring

- RSS-adressene for Kyst.no, Intrafish, Fiskeribladet og Fiskeridirektoratet er merket `verify: true`
  og må bekreftes (feilen vises i nyhetsfanen hvis adressen er gal).
- Yahoo-symbolene for Euronext Growth-selskaper (NOAP, GIGA) og chilenske selskaper kan avvike; skriptet
  logger hvilke som feiler.
- Lokalitetssøknadene hentes fra Fiskeridirektoratets CSV-eksport (`aqua-download-list`), og detaljsiden per søknad
  (MTB, planlagt produksjon, utfall, saksbehandler) hentes med cache i `data/details.json` – nye og endrede saker
  først, inntil 60 sider per kjøring. Eksporten mangler produksjonsområde.
- Kartet bruker Kartverkets åpne bakgrunnskart og Leaflet fra cdnjs.
- Nasdaq Salmon Index og Akvakulturregisteret (nye/endrede tillatelser) er neste kilder å koble på.

## Saksgang fra eInnsyn

For hver søknad søker skriptet i eInnsyns åpne API (`api.einnsyn.no/search`) på lokalitetsnavn og søker,
avgrenset til perioden etter innsendt dato. Treff krever at lokalitetsnavnet står i tittelen; øvrige
journalposter i samme saksmappe tas også med. Resultatet lagres i `data/einnsyn.json` (cache) og én fil per
søknad i `data/einnsyn/<søknadsnummer>.json`, som siden laster når raden åpnes. Saker under behandling
friskes opp daglig, avsluttede hver 30. dag, inntil 100 spørringer per kjøring.

Journalpostene klassifiseres regelbasert til saksgangssteg (kommune, Mattilsynet, Statsforvalteren,
Kystverket, Fiskeridirektoratet, vedtak fylkeskommune, klage). Kommunene er i hovedsak ikke på eInnsyn,
så det kommunale steget ses stort sett via fylkeskommunens post.

## Kapasitetsauksjoner

Skriptet leser indeks-siden «Auksjon av produksjonskapasitet» hos Fiskeridirektoratet hver kjøring og undersidene
hver 6. time. Tabeller med selskap/tonn/vederlag og per produksjonsområde parses generisk og legges oppå
`content/auksjoner.json`, som holder totaler, fastpris og kilder for eldre runder samt status for kommende runde.
Når Fiskeridirektoratet publiserer 2026-resultatene, fanges de automatisk og runden merkes som gjennomført.
Søkere i Lokalitetssøknader som matcher en auksjonskjøper på navn får merket «kjøpte X t»; usikre treff (bare
første navneledd matcher) vises med «~».

## Følg en søker (abonnement)

Skriptet sporer endringer (ny søknad, statusendring, utfall, endret MTB) og publiserer dem som RSS:

- alle endringer: `data/feeds/alle.xml`
- per søker: `data/feeds/soker/<slug>.xml` – lenken vises på siden når du filtrerer på en søker

Tre måter å få det som e-post:

1. **Outlook:** «RSS-feeder» → «Legg til ny RSS-feed» → lim inn lenken. Personlig, ingen oppsett.
2. **Power Automate:** utløser «Når et feedelement publiseres» → «Send en e-post (V2)». Kan sende til flere.
3. **Sentralt fra repoet:** legg søkeren i `content/subscriptions.json` og sett disse secrets i
   Settings → Secrets and variables → Actions: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `MAIL_FROM`.
   `scripts/notify.py` kjører da etter hver datahenting og sender bare det som er nytt siden sist
   (tilstand i `data/notify_state.json`). Uten secrets hopper steget over.

## Lokalt

```bash
pip install -r requirements.txt
python scripts/fetch_data.py       # skriver data/data.json
python -m http.server 8000         # åpne http://localhost:8000
```

## Bytte til Azure senere

Alt er statiske filer + ett Python-skript. Flytt `index.html`/`data/` til en Azure Static Web App
(med Entra ID-pålogging hvis innholdet blir sensitivt) og kjør skriptet som en Timer-triggered
Azure Function eller i Azure DevOps Pipelines. Ingen kode må endres.

## Design

Farger og typografi følger ScaleAQ Style Guide (Scale Deep, Scale Surface, Scale Silver, Scale Fire som
sparsom aksent). Logoen er hentet som vektor fra style guiden (`assets/`). Proxima Nova brukes der den er
installert, ellers Arial – slik style guiden angir.
