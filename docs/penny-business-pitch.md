# Penny — Azure Cost Intelligence Dashboard

> Open-source FinOps tool built for engineers who need more than Azure's built-in tools offer.  
> Live at `az-penny.mysak.fun` · Stack: Python / FastAPI · Azure Container Apps (scale-to-zero)

---

## Co Penny dělá nad rámec Azure Cost Management + Azure Advisor

| Schopnost | Azure Cost Management | Azure Advisor | Power BI | **Penny** |
|---|---|---|---|---|
| Zobrazení nákladů | ✅ | ❌ | ✅ | ✅ |
| Anomaly detection (meziměsíční skoky, nové/zaniklé služby) | Alerts (threshold only) | ❌ | Manuální | ✅ týden-na-týden spiky >50 % |
| Live ARM inventář s billing historií **spojenou per resource_id** | ❌ | ❌ | Premium + ETL | ✅ automaticky, in-memory |
| Detekce "ghost costs" — zdroj smazán, billing stále běží | ❌ | ❌ | Manuálně | ✅ ARM ∩ billing join |
| Detekce stopped-not-deallocated VM (billing trvá!) | ❌ | ❌ | ❌ | ✅ ARM status + billing |
| Unit price drift — odhalení zdražení za jednotku | ❌ | ❌ | Manuálně | ✅ C_COST / C_QUANTITY per meter |
| Waste detector — placení za nulu (C_QUANTITY ≈ 0, C_COST > 0) | ❌ | Částečně | Manuálně | ✅ billing řádky |
| Filtrování podle `project` tagu (aplikace / produkt) | Omezené | ❌ | Manuálně | ✅ App dropdown + AND s RG filtrem |
| AI chat nad vašimi reálnými daty | ❌ | ❌ | ❌ | ✅ Gemini + agentic loop, SSE stream |
| Telegram bot — AI asistent v kapse | ❌ | ❌ | ❌ | ✅ per-chat conversation history |
| **Shield** — aktivní hlídač rozpočtu | Pouze e-mail/Action Group | Passivní | ❌ | ✅ push Telegram alert, 15min cyklus |
| Forecast (projekce end-of-month) | Základní | ❌ | Manuálně | ✅ skutečné billing sazby + extrapolace |
| Technician view (billing CSV analýza, untagged report s Kč) | Export only | ❌ | Manuálně | ✅ interaktivní, klikatelný |
| Resource deletion přes UI (s live streamem logu) | Portal | ❌ | ❌ | ✅ admin-only, streaming log |
| Scale-to-zero + Managed Identity (zero secrets) | N/A | N/A | N/A | ✅ ACA + DefaultAzureCredential |

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

## Dvě unikátní technické výhody

### 1. Fúze live ARM dat s billing exportem (in-memory join)

Azure portal a Power BI zobrazují live inventář a billing historii jako **dva oddělené pohledy**. Penny je spojuje automaticky v paměti na `resource_id`:

```
ARM API (real-time)          Billing export (Parquet, 30 dní)
  resource_id → stav           resource_id → C_COST, C_QUANTITY, C_DATE
  vm_size → t4g.small          10 řádků za 10 dní
  status → Deallocated         celková cena: $12.40
         ↓                              ↓
              IN-MEMORY JOIN (resource_id, normalizováno)
                        ↓
         "dev-app-host" — Deallocated (!) — $12.40/měsíc aktuálně stále teče
         cost_source: "export_hours"  ← víme že cena je ze skutečného billingu, ne z ceníku
```

Tím Penny odhaluje věci, které žádný jednotlivý pohled neumí:

| Situace | Co to znamená | Kde to vidíš v Azure portálu |
|---|---|---|
| ARM existuje, billing = $0 | Nový zdroj nebo free tier | Nigde spojeno |
| ARM neexistuje, billing > $0 | **"Ghost cost"** — smazáno, ale faktura stále chodí | Nigde |
| ARM status = Stopped (ne Deallocated), billing > $0 | **VM zastavena ale NEodlokována** — billing běží | Je nutno zkontrolovat ručně |
| `cost_source = "export_hours"` | Hodinová sazba odvozena z reálných billing dat | Ceník neodpovídá skutečnosti |
| `cost_source = "price_table"` | Nový zdroj — cena z Azure Retail Prices API (fallback) | N/A |

Power BI toto umí, ale vyžaduje **Power BI Premium licence + vlastní ETL pipeline + manuální konfiguraci joinu**. Penny to dělá ze dvou `async` cache objektů automaticky při každém načtení stránky.

---

### 2. Přístup k řádkovým billing datům (C_QUANTITY + C_COST per meter)

Azure Cost Management ukazuje vždy **agregované koruny**. Billing export (Parquet/CSV) obsahuje každý billing řádek zvlášť, včetně sloupce `Quantity` (C_QUANTITY) — tj. kolik jednotek bylo spotřebováno a za jakou cenu.

To otevírá analýzy, které Azure portal ani Power BI bez prémiového nastavení nenabídne:

**Unit price drift — je Azure zdražování?**
```python
effective_price = C_COST / C_QUANTITY  # per resource, per week
```
Pokud Azure překročíš pricing tier (např. storage transactions) nebo vyprší Reserved Instance, tato hodnota se změní. Portal ti ukáže vyšší celkovou fakturu — ale ne *proč* zdražilo za jednotku. Penny to může detekovat automaticky.

**Waste detector — platíš za nulu**
Řádky kde `C_QUANTITY ≈ 0` ale `C_COST > 0` jsou zombie resources:
- Reserved IP nepřipojená k ničemu
- Disk bez připojené VM
- Storage account s 0 transakcemi

Azure Advisor část tohoto chytí — ale pouze pro vybraný typ zdrojů a s týdenní latencí. Billing data odhalí *cokoliv* co přichází na fakturu.

**Burn rate per project s projekcí**
Protože máme `C_APP` tag na řádkové úrovni, lze spočítat burn rate per projekt (ne jen per subscription):
```
projekt "seip": posledních 14 dní → $X/den → projekce: $Y tento měsíc
projekt "penny": posledních 14 dní → $A/den → projekce: $B tento měsíc
```
Azure Cost Analysis to počítá jen na úrovni celé subscription nebo Management Group.

**Tag compliance report s přiřazenou cenou**
"Tyto konkrétní resource IDs nemají `project` tag a stojí $Y/měsíc" — klikatelný seznam s ARM ID, ne jen procento. Umožňuje přiřadit zodpovědnost konkrétnímu týmu.

---

## Proč to není jen jiný dashboard

Azure Cost Management + Advisor jsou **pasivní** — dávají data a doporučení, ale:
- nemají **AI** schopný odpovídat na „proč mi vzrostly náklady tento týden?"
- nepošlou ti **Telegram zprávu** když ti runtime překoná budget
- neukáží **live ARM inventář spojený s billing historií** — nikdy neuvidíš „tento VM je stopped-not-deallocated a stojí tě $X/měsíc"
- neumí **filtrovat po application (project tagu)** napříč všemi pohledy najednou
- nedetekují **nové nebo zaniklé služby** jako anomálii automaticky
- nevidí **unit price drift** — pouze celkovou fakturu, ne sazbu za jednotku

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
