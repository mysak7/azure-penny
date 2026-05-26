# AI Chat Widget — jak funguje a co bylo třeba opravit

Dokument popisuje architekturu AI chat widgetu v azure-penny, jeho integraci s externím LLM proxy, a sérii problémů, které bylo třeba vyřešit při prvním nasazení.

---

## Co je AI chat widget

Na každé stránce dashboardu je plovoucí tlačítko 🤖 (`gemini-3.5-flash`). Po kliknutí se otevře chat, do kterého lze psát přirozeným jazykem dotazy o Azure nákladech — česky i anglicky.

```
Uživatel → "Kde mám největší náklady tento týden?"
        ↓
  /api/ai/chat (FastAPI, SSE stream)
        ↓
  Agentic loop (max 5 iterací):
    1. volá nástroje (get_cost_summary, get_resource_groups, …)
    2. posílá výsledky zpět LLM
    3. LLM formuluje finální odpověď
        ↓
  SSE stream → typewriter efekt v prohlížeči
```

Odpovědi jsou streamovány jako [Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events) — uživatel vidí text průběžně, ne najednou po dlouhé pauze.

---

## Architektura — endpoint `/api/ai/chat`

```
Browser                  FastAPI (main.py)              LLM proxy
   │                          │                              │
   │  POST /api/ai/chat        │                              │
   │  {"question": "..."}     │                              │
   │─────────────────────────▶│                              │
   │                          │  POST /v1/chat/completions   │
   │                          │  Authorization: Bearer ...   │
   │                          │  CF-Access-Client-Id: ...    │
   │                          │─────────────────────────────▶│
   │                          │◀─────────────────────────────│
   │                          │  tool_calls: [get_cost_summary]
   │                          │                              │
   │  data: {"type":"tool"}   │  _execute_chat_tool(...)     │
   │◀─────────────────────────│  (čte z pandas DataFrame)   │
   │                          │                              │
   │                          │  POST /v1/chat/completions   │
   │                          │  (s výsledky nástrojů)       │
   │                          │─────────────────────────────▶│
   │                          │◀─────────────────────────────│
   │  data: {"type":"token"}  │                              │
   │◀─────────────────────────│                              │
   │       ...                │                              │
   │  data: {"type":"done"}   │                              │
   │◀─────────────────────────│                              │
```

### Dostupné nástroje (tool use)

| Tool | Popis |
|---|---|
| `get_cost_summary` | Celkové náklady dle služeb a resource groupů za N dní |
| `get_resource_groups` | Seznam RG s náklady za N dní |
| `get_live_resources` | Živý inventář z ARM API |
| `get_anomalies` | Anomálie v nákladech (skokový nárůst) |
| `get_daily_trend` | Denní trend nákladů za N dní |

### Konfigurace (env proměnné)

| Proměnná | Popis | Příklad |
|---|---|---|
| `VERTEX_PROXY_URL` | Base URL OpenAI-compatible LLM proxy | `https://vertex.mysak.fun` |
| `VERTEX_PROXY_API_KEY` | API klíč / Bearer token | `sk-...` |
| `CF_ACCESS_CLIENT_ID` | Cloudflare Access Service Token Client-ID | `xxx.access` |
| `CF_ACCESS_CLIENT_SECRET` | Cloudflare Access Service Token Client-Secret | `yyy` |

Pokud `VERTEX_PROXY_URL` nebo `VERTEX_PROXY_API_KEY` chybí, chat widget je **funkčně zakázán** a zobrazí chybu `Vertex proxy not configured`.

---

## LLM proxy — vertex.mysak.fun

Aplikace **nevolá Gemini/Vertex AI přímo**. Místo toho volá interní proxy (`vertex.mysak.fun`), která:

1. Je nasazena jako LiteLLM kontejner (`ghcr.io/berriai/litellm:main-stable`)
2. Je přístupná přes Cloudflare Tunnel (Cloudflare Zero Trust Access)
3. Překládá OpenAI-compatible API volání na Vertex AI / Gemini volání

```
azure-penny (ACA)
    │
    │  HTTPS + CF-Access-Client-{Id,Secret} headers
    ▼
Cloudflare Access (autentizace)
    │
    │  CF Tunnel
    ▼
LiteLLM (vertex.mysak.fun, port 4001 lokálně)
    │
    │  google-auth
    ▼
Vertex AI / Gemini 2.5 Flash
```

Pro lokální vývoj lze místo externího proxy použít lokální LiteLLM instanci přímo:
```env
VERTEX_PROXY_URL=http://127.0.0.1:4001
VERTEX_PROXY_API_KEY=<LITELLM_MASTER_KEY>
# CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET není potřeba pro lokální endpoint
```

---

## Nasazení — jak se env vars dostanou do Container App

### Produkce (GitHub Actions + Terraform)

```
git push main
    │
    ├─▶ CD workflow (cd.yml)
    │     └─ docker build + push → ACR
    │         └─ az containerapp update --image <new-tag>
    │            (zachová stávající env vars)
    │
    └─▶ Terraform workflow (terraform.yml)  ← pouze při změnách v terraform/**
          └─ Write prd.tfvars z GH Actions vars/secrets
          └─ terraform apply
             (přepíše celý Container App template včetně env vars)
```

**Důležité:** Terraform workflow generuje `prd.tfvars` dynamicky ze GitHub Actions secrets a variables. Pokud proměnná v GH Actions chybí, terraform apply ji nastaví na `""` → chat widget se zakáže.

### GitHub Actions secrets/variables pro chat widget

| Název | Typ | Obsah |
|---|---|---|
| `VERTEX_PROXY_URL` | Variable (plain) | `https://vertex.mysak.fun` |
| `CF_ACCESS_CLIENT_ID` | Variable (plain) | `xxx.access` |
| `VERTEX_PROXY_API_KEY` | **Secret** | `sk-...` |
| `CF_ACCESS_CLIENT_SECRET` | **Secret** | `yyy` |

Nastavení: `GitHub repo → Settings → Secrets and variables → Actions`

### Lokální vývoj

```bash
# .env v kořenu projektu (gitignorován)
VERTEX_PROXY_URL=http://127.0.0.1:4001
VERTEX_PROXY_API_KEY=<LITELLM_MASTER_KEY z vertex-proxy/.env>

STORAGE_ACCOUNT_NAME=stprdeusmcucta
STORAGE_CONTAINER_NAME=cost-exports
AZURE_SUBSCRIPTION_ID=5ffa7688-ecf9-4b9e-8f17-ab20790b0673
```

```bash
cd ~/GitHub/azure-penny
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

`python-dotenv` načte `.env` automaticky při startu. Chat widget funguje na `http://localhost:8000`.

---

## Problémy a jejich řešení

### Problém 1 — Chybějící `return StreamingResponse(...)`

**Symptom:** `POST /api/ai/chat` vrací HTTP 200 s tělem `null` (content-type: application/json). Widget se ani neotevře správně.

**Příčina:** Funkce `api_ai_chat()` v `main.py` definovala generátor `event_stream()` ale zapomněla ho vrátit:

```python
# ŠPATNĚ (konec funkce před opravou)
async def event_stream():
    ...
    yield _sse({"type": "done"})
# ← chyběl return!
```

FastAPI interpretoval `None` jako odpověď a serializoval ho jako JSON `null`.

**Oprava** (`36bbc45`):
```python
    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

**Proč to nebylo zachyceno dříve:** Chybová větev (při `VERTEX_PROXY_URL` == `""`) správně vracela `return StreamingResponse(...)` → SSE fungoval a zobrazoval "Vertex proxy not configured". Happy-path větev neměla return → `null`. Chyba proto vypadala jako chyba konfigurace, ne kódu.

---

### Problém 2 — Env vars chyběly v Container App

**Symptom:** Po opravě kódu widget stále hlásil `❌ Vertex proxy not configured`.

**Příčina:** `VERTEX_PROXY_URL` a `VERTEX_PROXY_API_KEY` nebyly nikdy nastaveny v produkčním Container App. Terraform config je neobsahoval, takže od vzniku aplikace nikdy nebyly přítomny.

**Oprava:**
```bash
az containerapp update \
  --name ca-prd-eus-penny \
  --resource-group rg-prd-eus-penny \
  --set-env-vars \
    "VERTEX_PROXY_URL=https://vertex.mysak.fun" \
    "VERTEX_PROXY_API_KEY=..." \
    "CF_ACCESS_CLIENT_ID=..." \
    "CF_ACCESS_CLIENT_SECRET=..."
```

A zároveň přidány do Terraform (`113f9e7`):
- `variables.tf` — nové optional proměnné `vertex_proxy_url`, `vertex_proxy_api_key`, `cf_access_client_id`, `cf_access_client_secret`
- `container_app.tf` — `dynamic "env"` / `dynamic "secret"` bloky (chat widget je disabled když vars jsou prázdné)

---

### Problém 3 — Terraform apply přepsal env vars (opakující se regrese)

**Symptom:** Po každém commitu s terraform změnami se env vars ztratily a chat widget opět hlásil chybu.

**Příčina:** Terraform workflow (`terraform.yml`) generuje `prd.tfvars` dynamicky z GitHub Actions vars/secrets v kroku `Write prd.tfvars`. Nové VERTEX proměnné tam nebyly zahrnuty → terraform apply nastavil `vertex_proxy_url = ""` → chat widget zakázán.

```yaml
# terraform.yml — PŘED opravou (chyběly VERTEX řádky)
cat > variables/prd.tfvars <<EOF
environment = "prd"
...
owner_email = "${{ vars.OWNER_EMAIL }}"
# ← VERTEX vars chyběly
EOF
```

**Oprava** (`5516be6`):
```yaml
# terraform.yml — PO opravě
cat > variables/prd.tfvars <<EOF
...
vertex_proxy_url        = "${{ vars.VERTEX_PROXY_URL }}"
vertex_proxy_api_key    = "${{ secrets.VERTEX_PROXY_API_KEY }}"
cf_access_client_id     = "${{ vars.CF_ACCESS_CLIENT_ID }}"
cf_access_client_secret = "${{ secrets.CF_ACCESS_CLIENT_SECRET }}"
EOF
```

A přidány hodnoty do GH Actions:
- Secrets: `VERTEX_PROXY_API_KEY`, `CF_ACCESS_CLIENT_SECRET`
- Variables: `VERTEX_PROXY_URL`, `CF_ACCESS_CLIENT_ID`

---

### Problém 4 — Lokální app neměla `AZURE_SUBSCRIPTION_ID`

**Symptom:** Lokální spuštění selhalo s `RuntimeError: AZURE_SUBSCRIPTION_ID environment variable is not set.`

**Příčina:** Soubor `.env` obsahoval pouze Cloudflare/Vertex hodnoty, chyběly Azure storage a subscription proměnné.

**Oprava:** Doplněn `.env` o:
```env
STORAGE_ACCOUNT_NAME=stprdeusmcucta
STORAGE_CONTAINER_NAME=cost-exports
AZURE_SUBSCRIPTION_ID=5ffa7688-ecf9-4b9e-8f17-ab20790b0673
```

---

### Problém 5 — `AuthorizationPermissionMismatch` na storage account

**Symptom:** Chat widget fungoval, ale AI asistent odpovídal "nemohu zjistit vaše náklady, chyba oprávnění".

**Příčina:** Lokální az login user (`admin@MichalMichal643.onmicrosoft.com`, OID `d90c9694-...`) neměl roli `Storage Blob Data Reader` na storage accountu `stprdeusmcucta`. Roli měla pouze Managed Identity (pro ACA) a SP (pro CI/CD).

**Oprava:**
```bash
az role assignment create \
  --role "Storage Blob Data Reader" \
  --assignee d90c9694-7e3c-4e07-a8a9-4da4879ee196 \
  --scope "/subscriptions/.../storageAccounts/stprdeusmcucta"
```

Po propagaci RBAC (cca 2–5 min) se data načetla a chat widget odpovídal správně.

---

## Diagnostika — jak ověřit že widget funguje

### 1. Zkontroluj env vars na Container App

```bash
az containerapp show \
  --name ca-prd-eus-penny \
  --resource-group rg-prd-eus-penny \
  --query "properties.template.containers[0].env[].{name:name,value:value}" \
  -o table
```

Musí obsahovat `VERTEX_PROXY_URL` a `VERTEX_PROXY_API_KEY`.

### 2. Zkontroluj aktivní revizi

```bash
az containerapp revision list \
  --name ca-prd-eus-penny \
  --resource-group rg-prd-eus-penny \
  --query "[?properties.active==\`true\`].{rev:name,traffic:properties.trafficWeight,state:properties.runningState}" \
  -o table
```

### 3. Test vertex proxy přímo

```bash
curl -s -X POST https://vertex.mysak.fun/v1/chat/completions \
  -H "Authorization: Bearer $VERTEX_PROXY_API_KEY" \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-2.5-flash","messages":[{"role":"user","content":"ping"}]}'
```

### 4. Test chat endpointu (lokálně)

```bash
curl -s -N -X POST http://localhost:8000/api/ai/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "Kde mám největší náklady?"}' | head -10
```

Správná odpověď: `data: {"type": "tool", ...}` pak `data: {"type": "token", ...}`.

Chybná odpověď: HTTP 200 `null` (chybí return StreamingResponse) nebo `data: {"type": "error", "text": "Vertex proxy not configured"}`.

---

## Commit history

| Commit | Co opravuje |
|---|---|
| `36bbc45` | Přidán `return StreamingResponse(event_stream(), ...)` — hlavní bug |
| `113f9e7` | Terraform: nové proměnné pro chat widget (`vertex_proxy_url` atd.) |
| `5516be6` | CI: terraform.yml čte VERTEX vars z GH Actions secrets/variables |
