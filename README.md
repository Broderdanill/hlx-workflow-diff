# BMC HLX Workflow Diff

En liten Python/FastAPI-container för att jämföra workflow-metadata mellan BMC Helix Innovation Suite / AR System-miljöer.

## Vad den gör

- Gränssnittet visar först hopfällda kategorier med totalsiffror. Inne i varje kategori kan du filtrera på Olika, Saknas och Lika.
- Delar upp Active Link Guides, Filter Guides, Applications, Packing Lists och Web Services från containers via `containerType`.
- Loggar in mot varje vald miljö med `/api/jwt/login`.
- Läser metadata via AR REST API: `/api/arsys/v1/entry/<metadata-form>`.
- Filtrerar med AR qualification (`q`) utifrån prefix och fritext.
- Kan jämföra `Alla` objekttyper som standard, eller en specifik typ.
- Normaliserar entries per objektnamn.
- Visar vilka objekt som är lika, olika eller saknas, och visar fältdiff sida-vid-sida för ändrade objekt.
- Sparar inget lokalt och använder ingen databas.

## Bygg och kör med Podman

```bash
podman build -t localhost/hlx-workflow-diff:latest -f Containerfile .
podman play kube podman-play-kube.yaml
```

Öppna sedan: http://localhost:8089

## Miljöer

Miljöerna konfigureras server-side, inte i browsern. GUI:t visar bara två menyval: källmiljö och målmiljö. URL, användarnamn och lösenord skickas alltså inte ut till klienten.

Redigera innan build:

```text
config/hlx-diff.yaml
```

Alternativt sätt `HELIX_CONFIG` till en annan sökväg eller använd `HELIX_ENVIRONMENTS_JSON`. Appen kräver inte `hostPath`, PVC eller databas.

## Viktigt om metadata-former

BMC dokumenterar AR System Data Dictionary som tabeller för schema, fields, filters, escalations, active links, menyer och containers. I många Helix/Remedy-installationer exponeras dessa via `AR System Metadata:*`-former, men form- och fältnamn kan variera mellan versioner, overlays och behörigheter. Standardmappningen är uppdaterad från exporterade metadata-former: `actlink`, `filter` och `escalation` är med små bokstäver och fält som inte finns, t.ex. `elseQualification`, används inte längre. Därför ligger form- och fältnamn fortsatt i YAML-filen.

Börja med läsbehörighet för servicekontot till metadata-formerna. Ett vanligt minimum är:

- `AR System Metadata: actlink`
- `AR System Metadata: filter`
- `AR System Metadata: escalation`
- eventuellt `AR System Metadata: arschema` och field-relaterade metadata-former om ni vill jämföra formulär/fält senare.

## API

Utöver GUI finns:

```bash
curl -X POST http://localhost:8089/api/compare \
  -H 'Content-Type: application/json' \
  -d '{
    "object_type":"all",
    "source_env":"DEV",
    "target_env":"TEST",
    "prefix":"CHG:",
    "ignore_fields":["timestamp"]
  }'
```

## Nästa naturliga förbättringar

- Export till JSON/CSV.
- Podman/Kubernetes secrets för credentials i stället för klartext i YAML.
- Djupare parsning av actions om ni vill jämföra action-rader separat i stället för hela metadatafält.
- Read-only credential vault via Kubernetes/Podman secret i stället för YAML-fil.
- Finjusterad mapping om er miljö använder andra `containerType`-värden för guides, applications, packing lists eller web services.
- Export till JSON/CSV.


## v7

- Lägger till en summerad översikt ovanför kategorierna så man direkt ser om varje kategori är helt lika eller har avvikelser.
- Kategoriheadern visar tydligt "Alla objekt matchar" eller antal avvikelser utan att behöva expandera.
