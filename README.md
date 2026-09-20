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
| endre kilde for lokalitetssøknader | `content/config.json` → `applications` |
| endre utseende, tekster, søknadsprosessen  | `index.html`                  |

Skriptet er tolerant: feiler én kilde, beholdes forrige verdi for den kilden og status vises på siden.
Ingen tall anslås – manglende kurser vises som strek.

## Kjente punkter etter første kjøring

- RSS-adressene for Kyst.no, Intrafish, Fiskeribladet og Fiskeridirektoratet er merket `verify: true`
  og må bekreftes (feilen vises i nyhetsfanen hvis adressen er gal).
- Yahoo-symbolene for Euronext Growth-selskaper (NOAP, GIGA) og chilenske selskaper kan avvike; skriptet
  logger hvilke som feiler.
- Lokalitetssøknadene hentes fra Fiskeridirektoratets CSV-eksport (`aqua-download-list`). Eksporten mangler
  utfall (godkjent/avslått) og produksjonsområde; utfall ligger på detaljsiden per søknad og kan hentes senere.
- Kartet bruker Kartverkets åpne bakgrunnskart og Leaflet fra cdnjs.
- Nasdaq Salmon Index og Akvakulturregisteret (nye/endrede tillatelser) er neste kilder å koble på.

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
