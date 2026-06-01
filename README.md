# hlx-workflow-diff v17

Webbverktyg för att jämföra workflow-metadata mellan BMC Helix Innovation Suite / AR System-miljöer.

## Viktigt i v17

Verktyget använder nu bara en cachetyp: komplett/djup snapshot.

- Ingen separat standardcache.
- GUI:t har inte längre val för snabb/djup jämförelse.
- Jämförelser körs alltid mot komplett cache.
- Vid pod-start gör appen automatiskt `auto sync`:
  - saknas cache för en miljö/objekttyp → full djup sync för just den delen.
  - finns cache → inkrementell kontroll och djup expansion endast för nya/ändrade objekt.
- Intervallsync kontrollerar timestamps och uppdaterar objekttyper där ändringar finns.

## Rekommenderad körning

```bash
podman build -t localhost/hlx-workflow-diff:latest -f Containerfile .
podman play kube podman-play-kube.yaml
```

GUI:

```text
http://localhost:8089
```

## Performance-inställningar

```yaml
- name: HELIX_DEEP_PROFILE
  value: balanced
- name: HELIX_RELATED_FETCH_CONCURRENCY
  value: "6"
- name: HELIX_SYNC_INTERVAL_SECONDS
  value: "900"
- name: LOG_LEVEL
  value: INFO
```

`balanced` är tänkt som Migrator-lik nivå. `full` hämtar fler äldre/ovanliga relaterade metadata-former och kan ta betydligt längre tid.

För felsökning:

```yaml
- name: LOG_LEVEL
  value: DEBUG
```

## v16 - Migrator-lik sync/cache

Denna version använder bara en komplett/djup cache, men synken är mer lik gamla Migrator:

- Första körningen bygger en full djup snapshot per miljö och objekttyp.
- När cache finns körs inkrementell sync.
- Inkrementell sync läser först basmetadata för objekttypen.
- Objekt som är oförändrade återanvänder redan cachad djupmetadata.
- Nya/ändrade objekt får djupmetadata hämtad selektivt via parent-id-filter.
- Borttagna objekt tas bort från cache.
- Miljöer, objekttyper och relaterade metadata-former hämtas parallellt.

Nya prestanda-env:

```yaml
HELIX_ENV_CONCURRENCY: "2"
HELIX_OBJECT_CONCURRENCY: "2"
HELIX_RELATED_FETCH_CONCURRENCY: "3"
HELIX_RELATED_BATCH_CONCURRENCY: "3"
HELIX_RELATED_PARENT_BATCH_SIZE: "25"
HELIX_HTTP_TIMEOUT: "300"
```

Öka försiktigt om AR-servern och databasen orkar. Vid första fulla cachen är det fortfarande mycket data, men efter det ska normal sync i första hand läsa basmetadata och bara djup-expanda ändringar.


## v17 - Migrator-likare och snabbare första cache

- Tunga relaterade metadata-former hämtas inte längre helt ofiltrerat.
- Även första fulla cachen använder parent-scopade q-batcher mot relaterade metadata-former.
- Batcher hämtas parallellt men med lägre standardvärden för att inte överbelasta AR-servern.
- Startup använder `auto`: bara saknade cache-delar fullsynkas, befintliga delar körs inkrementellt.
- Statuspanelen **Miljöstatus och cache** är hopfälld som standard och visar direkt om något kör.
- Detaljlistan **Visa cache per objekttyp** är borttagen från GUI:t.

Bra startvärden:

```yaml
HELIX_ENV_CONCURRENCY: "2"
HELIX_OBJECT_CONCURRENCY: "2"
HELIX_RELATED_FETCH_CONCURRENCY: "3"
HELIX_RELATED_BATCH_CONCURRENCY: "3"
HELIX_RELATED_PARENT_BATCH_SIZE: "25"
HELIX_HTTP_TIMEOUT: "300"
```

Om AR-servern verkar må bra kan du öka `HELIX_OBJECT_CONCURRENCY` eller `HELIX_RELATED_BATCH_CONCURRENCY` ett steg i taget.
