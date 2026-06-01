# Penny — Azure Cost Intelligence Dashboard

> Open-source FinOps tool built for engineers who need more than Azure's built-in tools offer.  
> Live at `az-penny.mysak.fun` · Stack: Python / FastAPI · Azure Container Apps (scale-to-zero)

---

## Co Penny dělá nad rámec Azure Cost Management + Azure Advisor

| Schopnost | Azure Cost Management | Azure Advisor | **Penny** |
|---|---|---|---|
| Zobrazení nákladů | ✅ | ❌ | ✅ |
| Anomaly detection (meziměsíční skoky, nové/zaniklé služby) | Alerts (threshold only) | ❌ | ✅ týden-na-týden spiky >50 % |
| Live ARM inventář s odhadovanou cenou za zdroj | ❌ | ❌ | ✅ retail pricing API |
| Filtrování podle `project` tagu (aplikace / produkt) | Omezené | ❌ | ✅ App dropdown + AND s RG filtrem |
| AI chat nad vašimi reálnými daty | ❌ | ❌ | ✅ Claude + agentic loop, SSE stream |
| Telegram bot — AI asistent v kapse | ❌ | ❌ | ✅ per-chat conversation history |
| **Shield** — aktivní hlídač rozpočtu | Pouze e-mail/Action Group | Passivní | ✅ push Telegram alert, 15min cyklus |
| Forecast (projekce end-of-month) | Základní | ❌ | ✅ ARM pricing + lineární extrapolace |
| Technician view (billing CSV analýza, untagged report) | Export only | ❌ | ✅ interaktivní |
| Resource deletion přes UI (s live streamem logu) | Portal | ❌ | ✅ admin-only, streaming log |
| Scale-to-zero + Managed Identity (zero secrets) | N/A | N/A | ✅ ACA + DefaultAzureCredential |

---

## Klíčové funkce (v1.5)

### 1. Manager dashboard
- KPI řada: Compute / Storage / Network / Database / Monitoring & Security / Total
- Periody: Den / Týden / Měsíc / Rok
- **Forecast chart** — stacked bars per kategorii nebo per top-8 sub-služeb v kategorii
  - ISO week / month / day granularita, projekce do konce měsíce jako text badge
- **Monthly Forecast karta** — actual vs. projected daily bars, billing-lag gap zvýrazněn
- RG + App filter (AND kombinace)

### 2. Technician view
- Analýza billing exportu: všechny služby, kategorie, breakdown
- **Anomaly detection** — týden-na-týden skoky >50 %, nové/zaniklé služby, untagged resources
- `/api/other` — catch-all pro neuznané služby, 100% pokrytí součtu

### 3. Live view (ARM inventory)
- Real-time seznam zdrojů s odhadovanou měsíční cenou (Azure Retail Prices API)
- Filtr: RG + App (project tag)
- Zdroj ceny viditelný per-resource (export / spot_rate / price_table / …)
- Admin: smazání zdroje nebo celé RG s live streamovaným logem

### 4. AI Chat (Claude)
- Agentic loop: model si sám volí nástroje (forecast, anomálie, kategorie, snapshot, …)
- SSE streaming přímo do prohlížeče
- `get_page_snapshot` — jeden call = celý přehled (total, kategorie, top RGs, anomálie)

### 5. Telegram bot — AI v kapse
- Stejný model + nástroje jako web UI, sdílená logika (`ai_chat.py`)
- Per-chat conversation history (posledních 20 zpráv)
- Nativní Telegram HTML formátování

### 6. Shield — aktivní hlídač rozpočtu
- Pozadí: každých 15 minut porovnává projektovaný spend vůči prahu
- Při překročení pošle Telegram HTML alert + 2h cooldown
- Konfigurovatelné přes `/shield` dashboard nebo REST API
- Persistence konfigurace do `shield_config.json`

---

## Proč to není jen jiný dashboard

Azure Cost Management + Advisor jsou **pasivní** — dávají data a doporučení, ale:
- nemají **AI** schopný odpovídat na „proč mi vzrostly náklady tento týden?"
- nepošlou ti **Telegram zprávu** když ti runtime překoná budget
- neukáží **live ARM inventář** s cenami bez portálu
- neumí **filtrovat po application (project tagu)** napříč všemi pohledy najednou
- nedetekují **nové nebo zaniklé služby** bez alertu jako anomálii samo o sobě

Penny tyto mezery překlenuje jako **open-source, self-hostovaný nástroj** s nulovou závislostí na third-party SaaS.

---

## Relevance pro Eurowag / enterprise FinOps

Eurowag hledá (dle JD):

| JD požadavek | Kde Penny ukazuje praxi |
|---|---|
| Azure billing constructs, reservations, Cost Management | Celý projekt — billing export → Parquet → FastAPI |
| Automation/scripting pro cost data extraction | `storage.py` — auto-discovery exportů, in-memory cache, reload API |
| Dashboards + forecasting | Forecast chart, Monthly Forecast karta, Year view |
| Tagging standards + showback | `C_APP` z `project` tagu, untagged report, RG breakdown |
| Cost anomaly monitoring | Anomaly detection endpoint + Shield alerting |
| Automate recurring reporting | Telegram bot = asynchronní reporting bez portálu |
| Python + Azure SDK | `DefaultAzureCredential`, `azure-storage-blob`, `azure-identity` |

---

## Architektura (jednoduchá)

```
Azure Blob Storage (Cost Management export → Parquet)
        ↓  DefaultAzureCredential (Managed Identity)
   FastAPI / Python (Azure Container Apps, scale-to-zero)
        ↓
  ┌─────────────────────────────────┐
  │  Web UI (Jinja2 + vanilla JS)   │
  │  REST API                       │
  │  AI Chat (Claude, SSE)          │
  │  Telegram Bot (webhook)         │
  │  Shield (asyncio background)    │
  └─────────────────────────────────┘
        ↓
  Cloudflare Access (Entra ID authn)
```

Zero secrets: vše přes Managed Identity. ACA scale-to-zero = náklady jen při použití.

---

## Technický stack

- **Backend**: Python 3.12, FastAPI, pandas, pyarrow
- **Azure SDK**: `azure-identity`, `azure-storage-blob`, `azure-mgmt-resource`
- **AI**: Anthropic Claude API (agentic tool use)
- **Infra**: Terraform (ACA, ACR, Storage Account, Managed Identity, RBAC)
- **CI/CD**: GitHub Actions (OIDC → ACR push → ACA deploy)
- **Auth**: Cloudflare Access + Entra ID (Easy Auth na `/telegram/webhook` excluded)

---

*Penny je živý projekt — v1.5 vydán 27. 5. 2026. Zdrojový kód: github.com/mysak7/azure-penny*
