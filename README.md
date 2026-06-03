# Helix Workflow Diff

## Version 28.13

- Miljö-URL och cache-scope ligger i ConfigMap.
- Användarnamn och lösenord ligger i Secret och exponeras som `UM_USERNAME`, `UM_PASSWORD`, `UTB_USERNAME`, `UTB_PASSWORD` osv.
- Jämförelse/sökning använder ett lokalt sökindex (`*.index.json`) bredvid cachefilerna på PVC. Indexet byggs vid sync och byggs lazy vid första sökning om äldre cache saknar index.
- Miljöstatus i GUI är vänsterjusterad och uppdateras utan att hela sidan laddas om.


Helix Workflow Diff är ett lättviktsverktyg för att jämföra workflow-metadata mellan BMC Helix Innovation Suite / AR System-miljöer via REST API.

Verktyget är byggt för att fungera ungefär som en enkel Migrator-lik jämförelsevy:

- välj två miljöer
- jämför metadata och workflow
- se vilka objekt som är lika, olika eller saknas
- expandera objekt och se fältdifferenser
- använd lokal JSON-cache på PVC för snabbare jämförelser

Ingen databas används.

## Nytt i denna version

Den här versionen bygger vidare på v28.10 och flyttar runtime-konfigurationen till en Kubernetes/Podman ConfigMap.

### Ändringar

- Sync körs en miljö i taget.
- Standardvärdet för objekttyps-parallellism är sänkt till `1`.
- Progressinformationen visar tydligare fas, aktuellt objekt och räknare.
- Parent-id:n i relaterade metadatafrågor citeras säkrare, särskilt för `filterId`, `actlinkId` och `containerId`.
- Vid aktiv `cache_scope` djupverifieras scoped forms/workflow som standard vid incremental sync, så fält-, permission- och actionändringar inte missas.
- Containerfilen sätter explicita läsrättigheter så att appen kan importeras när containern körs som icke-root.
- `hlx-diff.yaml` läses nu primärt från ConfigMap-mounten `/etc/hlx-workflow-diff/config.yaml`.

## Arkitektur

```text
Browser
  ↓
FastAPI / Jinja2 / HTMX-lik enkel frontend
  ↓
Helix REST API
  ↓
AR System Metadata forms
```

Applikationen kör API-anrop från containern, inte från browsern.

## Cache

Cache lagras som JSON-filer i `HELIX_CACHE_DIR`, normalt `/data/cache` på PVC.

Exempel:

```text
/data/cache/
├── um__form__deep.json
├── um__filter__deep.json
├── utb__form__deep.json
└── sync-state.json
```

### Startup-beteende

Vid pod-start:

- saknas cache för en miljö/objekttyp → cache byggs
- finns cache → incremental kontroll körs

Ingen intervallsync körs automatiskt.

Manuell sync sker via:

- knappen **Kontrollera ändringar** per miljö i GUI:t
- API-endpoint `/api/sync/{environment}`

## Cache scope

Du kan begränsa vad som läses in via `cache_scope` i `hlx-diff.yaml`.

Exempel:

```yaml
# Object types, fält och relaterade metadata-former ligger som default i appen.
# Vill du override:a dem kan du lägga till object_types här, men låt annars bli så att
# den djupa jämförelsen använder standardstödet för actions, mappings, guide references, fält och menyer.

cache_scope:
  include_form_prefixes: [HLX*,AR*]
  exclude_form_prefixes: []
```

När scope är aktivt försöker verktyget:

1. hitta formulär som matchar scope
2. identifiera workflow som är mappat till dessa formulär
3. hämta djupmetadata för scoped objekt

Globala objekttyper som Applications, Packing Lists, Web Services och Menus hoppas över vid aktiv scope om du inte sätter:

```yaml
- name: HELIX_SCOPE_INCLUDE_GLOBAL_TYPES
  value: "true"
```

## Viktiga miljövariabler

| Variabel | Standard | Beskrivning |
|---|---:|---|
| `HELIX_CONFIG_PATH` | `/etc/hlx-workflow-diff/config.yaml` | Primär sökväg till YAML-konfiguration, normalt från ConfigMap |
| `HELIX_CONFIG` | unset | Legacy/fallback-sökväg till YAML-konfiguration |
| `HELIX_CACHE_DIR` | `/tmp/cache` | Cachekatalog |
| `LOG_LEVEL` | `INFO` | Loggnivå |
| `HELIX_SYNC_ON_START` | `true` | Kör startup-sync |
| `HELIX_OBJECT_CONCURRENCY` | `1` | Antal objekttyper som synkas parallellt per miljö |
| `HELIX_RELATED_FETCH_CONCURRENCY` | `6` | Antal relaterade metadataformer parallellt per objekt |
| `HELIX_RELATED_BATCH_CONCURRENCY` | `4` | Antal parent-id batchar parallellt per relaterad form |
| `HELIX_RELATED_PARENT_BATCH_SIZE` | `25` | Antal parent-id:n per AR qualification |
| `HELIX_HTTP_TIMEOUT` | `240` | HTTP-timeout i sekunder |
| `HELIX_INCREMENTAL_VERIFY_SCOPED_FORMS` | `true` | Djupverifiera scoped forms vid incremental sync |
| `HELIX_INCREMENTAL_VERIFY_SCOPED_WORKFLOW` | `true` | Djupverifiera scoped workflow vid incremental sync |

## Konfiguration

Konfigurationen ligger normalt i en ConfigMap och mountas som:

```text
/etc/hlx-workflow-diff/config.yaml
```

Appen letar i följande ordning:

1. `HELIX_CONFIG_PATH`
2. `HELIX_CONFIG`
3. `/etc/hlx-workflow-diff/config.yaml`
4. `/config/hlx-diff.yaml`
5. `/opt/hlx-workflow-diff/config/hlx-diff.yaml`

Exempel på innehåll:

```yaml
environments:
  - name: um
    base_url: http://ars-arserver:8008
    username: Demo
    password: P@ssw0rd
    verify_tls: false

  - name: utb
    base_url: http://ars-arserver:8008
    username: Demo
    password: P@ssw0rd
    verify_tls: false

# Object types, fält och relaterade metadata-former ligger som default i appen.
# Vill du override:a dem kan du lägga till object_types här, men låt annars bli så att
# den djupa jämförelsen använder standardstödet för actions, mappings, guide references, fält och menyer.

cache_scope:
  include_form_prefixes: [HLX*,AR*]
  exclude_form_prefixes: []
```

## Körning med Podman

Bygg imagen:

```bash
podman build -t localhost/hlx-workflow-diff:latest -f Containerfile .
```

Starta med kube-yamlen. Den innehåller både `ConfigMap`, `PersistentVolumeClaim` och `Pod`:

```bash
podman play kube podman-play-kube.yaml
```

Öppna:

```text
http://localhost:8089
```

## API

### Status

```http
GET /api/cache/status
```

### Synka en miljö

```http
POST /api/sync/um
```

Body kan vara tom eller innehålla:

```json
{
  "object_type": "all"
}
```

### Starta sync med payload

```http
POST /api/sync/start
```

Exempel:

```json
{
  "environments": ["um"],
  "object_type": "all",
  "mode": "incremental"
}
```

## Felsökning

Sätt mer loggning:

```yaml
- name: LOG_LEVEL
  value: DEBUG
```

Kontrollera pod-logg:

```bash
podman logs <container-id>
```

Viktiga loggrader:

- `workflow scope applied before deep fetch`
- `incremental scan`
- `incremental scoped deep verification`
- `fetch related start`
- `fetch related batch done`
- `cache saved`

## Om cache byggs om trots PVC

Cache invalideras om `cache_scope` ändras eller om scope-modellen i koden ändrats mellan versioner. Detta är avsiktligt för att inte återanvända en cache som byggts med bredare/buggig scope.


### v28.1 fix

- Relaterade metadataformer hämtas inte längre oscope:at när parent-listan är tom.
- Om `cache_scope` ger noll matchande workflow för en objekttyp hoppas relaterade former över istället för att läsa hela metadataformen.
- Oscope:ad relaterad hämtning är avstängd som standard. Kan endast aktiveras med `HELIX_ALLOW_UNSCOPED_RELATED_FETCH=true` för felsökning.


## v28.2 schemaId scope fix

Form-prefix scope now uses the AR System data-dictionary `schemaId` value from `AR System Metadata: arschema` when querying workflow mapping tables (`filter_mapping`, `actlink_mapping`, `escal_mapping`). Older versions could use the REST entry id such as `5008-1`, which does not match mapping rows and therefore missed workflow for scoped forms. The cache scope model was bumped, so existing scoped cache is rebuilt once.


## v28.3 fix

- Workflow base rows are now fetched by `Record ID` instead of querying `filterId`, `actlinkId` or `escalationId` directly on the base metadata forms.
- The mapping tables still use `filterId`, `actlinkId`, `escalationId` and `schemaId` according to the AR System data dictionary.
- Cache scope model was bumped so older failed scoped cache is rebuilt once.

## v28.4

- Fixade query mot metadata-formerna `AR System Metadata: filter`, `actlink` och `escalation` när scoped workflow hämtas.
- `Record ID`/`Request ID` är character-fält i metadata-formerna och måste därför skickas citerade även när värdet ser numeriskt ut.
- Cache scope bumpad så felaktig cache från tidigare test byggs om en gång.

## v28.6 fix

- Scoped workflow base fetch now queries both `Request ID` and `Record ID` as character values.
- This avoids PostgreSQL `citext = integer` errors when mapping tables return numeric-looking workflow ids.
- Filter/Active Link/Filter Guide scope continues to use mapping tables first and does not fall back to unscoped slow-path reads by default.
- Cache scope model bumped so incorrect v28.2-v28.4 snapshots are rebuilt once.


## v28.6

- Scoped workflow base rows are now queried via numeric display fields from the metadata XML: `Active Link ID`, `Filter ID`, `Escalation ID`, and `Container ID`.
- Form related metadata parent ids are normalized from REST entry ids like `5008-1` to schema ids like `5008`.
- Default Podman concurrency is set to one environment and one object type at a time for clearer logging and less AR server load.


## v28.8 notes

- Global object types (`menu`, `application`, `packing_list`, `web_service`) are cached even when `cache_scope` is active.
- Guide scoping now checks both `referenceId` and `referenceObjId` in `AR System Metadata: arreference`, then fetches containers by numeric `Container ID`.
- GUI prefix filtering is case-insensitive.


## v28.9 notes

- Fixar hämtning av Active Link Guides / Filter Guides mot `AR System Metadata: arcontainer`.
- Guide-containers hämtas nu via `Request ID` / `Record ID` som citerade textfält, inte via `Container ID`, eftersom vissa Helix/PostgreSQL-versioner annars genererar `citext = integer`.
- Cache-scope är bumpad så felaktig guide-cache från v28.8 byggs om.


## v28.10

- Fixar arcontainer-kvalificering: containerType citeras som text för att undvika PostgreSQL-felet `citext = integer`.
- Guide-referenser frågas med textciterade referenceId/referenceObjId.
- Behåller tidigare skydd mot oscopead slow-path.


## v28.12

- ConfigMap default scope corrected to include `HLX*` and `AR*`.
- Active Link Guides / Filter Guides now fetch `AR System Metadata: arcontainer` without a qualification and filter locally to avoid `citext = integer` errors from Helix/PostgreSQL metadata view forms.
- GUI sync status now updates with `fetch('/api/cache/status')` and only re-renders the status panel, instead of reloading the whole page while sync is running.

## v28.14 - djupare Migrator-lik diff

Denna version reviderar jämförelsemotorn så att miljöspecifika tekniska id:n inte längre skapar falska differenser.
Exempel på sådana fält är `Request ID`, `Record ID`, `schemaId`, `actlinkId`, `filterId`, `containerId`, `Active Link ID`, `Filter ID`, `Container ID` och motsvarande.

Djupmetadata jämförs nu rad-för-rad med stabila nycklar:

- Formulärfält jämförs per `fieldName`/`fieldId`.
- Field permissions jämförs per `fieldId + groupId`.
- Enum-värden jämförs per fält och enumvärde.
- VUI/vyer jämförs per `vuiName`/`vuiId`.
- View mapping jämförs per fält/extField.
- Workflow actions jämförs per `actionIndex` och action-specifika fält.
- Guide references jämförs per `referenceOrder`/referens.

Det betyder att du inte bara ser att exempelvis ett formulär skiljer sig, utan också vilket relaterat fält, vy, permission, action eller guide-medlem som saknas eller avviker.

Formulärens interna REST-id normaliseras också från exempelvis `5008-1` till dictionary-id `5008` när relaterade formulärfält, vyer och permissions hämtas.
