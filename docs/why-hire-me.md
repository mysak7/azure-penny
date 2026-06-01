# Proč mě najmout na FinOps / Cloud Cost Engineering

> Tento dokument není o Penny. Je o konkrétních schopnostech, které demonstruji tím, že Penny existuje.  
> Penny je vedlejší produkt — hlavní produkt jsem já a způsob, jakým přemýšlím o cloudových nákladech.

---

## Co většina BI/FinOps lidí dělá vs. co dělám já

Typický přístup ke cloudovým nákladům vypadá takto:
1. Nastavit Cost Management export do Storage
2. Připojit Power BI, naklikat dashboard
3. Čekat na měsíční report

To funguje. Ale řeší jen **co** se stalo, nikdy **proč** — a vždy se zpožděním.

Můj přístup: **rozumím billing datům na úrovni jednotlivých řádků**, vím kde jsou jejich limity, a dokážu postavit nástroje, které odhalí problémy dřív než přijde faktura.

---

## Konkrétní věci, které jsou v Power BI buď nemožné, nebo bolestivé

### 1. Fúze live ARM stavu s billing historií v reálném čase

**Co tím získáš:** Vidíš každý Azure zdroj s jeho *skutečným* billing stavem — ne odhadnutým z ceníku.

Příklad: VM je ve stavu `Stopped` (ne `Deallocated`) → billing stále běží → tato informace vyžaduje **dvě data sources najednou**: ARM Management API (live) a billing export (historická data). Spojení na `resource_id` s normalizací (ARM ID je case-insensitive, obsahuje verze API, aliasy).

**Proč je to v Power BI bolestivé:**
- Power BI refreshuje data na schedule (min. 30 min, u Standard plánu 1× hodinu)
- ARM API vyžaduje custom connector v Power Query s ručním řešením paginace
- Normalizace `resource_id` (lowercase, strip `/providers/...` suffix, aliasy) je netriviální M transformace
- Auth přes Managed Identity Power BI nativně nepodporuje — nutný service principal + secret

**Jak to řeším v kódu:**
```python
# live_resources.py — _get_live_data()
# ARM inventory + billing export → join na normalizovaném resource_id
# cost_source odráží kde cena pochází: "export_hours" = reálný billing, "price_table" = fallback ceník
```

---

### 2. Detekce "ghost costs" — zdroj smazán, billing stále teče

**Co tím získáš:** Okamžitý přehled o zdrojích, které v ARM neexistují, ale stále generují náklady.

Stane se to běžně: někdo smaže VM, ale zapomene na přidruženou Reserved IP nebo managed disk. Nebo se smaže resource group, ale billing se zpozdí o 2–3 dny.

**Proč je to v Power BI nemožné** bez custom řešení:
- Cost Management export neví nic o tom, jestli zdroj dnes existuje
- ARM inventory API neví nic o historických nákladech
- Power BI nemá způsob jak tyto dvě sady dat spojit a říct „tenhle resource_id je v billingu ale chybí v ARM"
- Bez live ARM dat je to vždy analýza minulosti, nikoli upozornění dnes

---

### 3. Unit price drift — detekce zdražení za jednotku

**Co tím získáš:** Upozornění kdy Azure zdraží za jednotku (storage transakce, data transfer, atd.) — odlišení od zdražení způsobeného větším objemem.

Billing export obsahuje `Quantity` (C_QUANTITY) i `Cost` (C_COST) na každém řádku. Efektivní cena za jednotku:

```
unit_price = C_COST / C_QUANTITY  per resource_id, per MeterSubCategory, per týden
```

Pokud se tato hodnota změní o >10 %, je to signál — buď jsi překročil pricing tier, vyprší ti Reserved Instance, nebo Azure tiše změnil cenu.

**Proč je to v Power BI bolestivé:**
- `Quantity` sloupec musí být v exportu (není vždy — záleží na nastavení exportu)
- DAX time intelligence pro week-over-week porovnání unit price per resource per meter je komplexní
- Žádný built-in alert na *změnu unit price* — Power BI alertuje jen na threshold hodnot, ne na relativní změnu
- Výsledek je statická tabulka — vyžaduje, aby si uživatel aktivně šel podívat

---

### 4. Tag inference pro AKS managed resource groups

**Co tím získáš:** Správné přiřazení nákladů AKS node pools a Karpenter nodů ke správnému projektu — i když na nich není `project` tag.

AKS automaticky vytváří `mc_*` resource groups (managed by AKS), kde zdroje nezdědí tagy z nadřazené RG. Karpenter nody mají systémové tagy (`karpenter.azure.com_cluster`), ale ne uživatelský `project` tag.

Bez tohoto by celá AKS infrastruktura spadala do "Untagged" — a tím pádem bys nevěděl, který projekt za ní platí.

**Jak to řeším:**
```python
# storage.py — _infer_app_from_mc_rg() + _infer_app_from_aks_tags()
# Parsování cluster name z mc_{parent-rg}_{cluster-name}_{region} formátu
# Čtení karpenter.azure.com_cluster tagu pro Karpenter nody
```

**Proč je to v Power BI bolestivé:**
- Vyžaduje znalost interní struktury AKS naming conventions
- M transformace na regex parsing resource group jmen
- Nutno udržovat při každé změně cluster naming standardů

---

### 5. Proaktivní alerting s kontextem (ne jen threshold e-mail)

**Co tím získáš:** Telegram zpráva s konkrétním textem „*compute náklady vzrostly o 73 % tento týden, hlavně NAT gateway data processing*" — ne jen „cost exceeded $50".

Power BI alerting je threshold-based a posílá e-mail. Pokud chceš Telegram, webhook, nebo SMS, nutný Power Automate (= další licenční náklad, workflow, který někdo musí spravovat).

Shield v Penny:
- Každých 15 minut porovná projektovaný spend s nastavením
- Při překročení zavolá AI model s aktuálními daty → model napíše kontext
- Pošle Telegram HTML zprávu s 2h cooldownem
- Zero external dependency — vše běží v jednom ACA kontejneru

---

### 6. Billing data nad 1M řádků bez Premium

Standard Power BI Desktop má limit 1 milion řádků na tabulku. Enterprise Azure subscription s desítkami subscription a týdenním exportem tento limit překročí.

Penny načítá Parquet soubor přes `pyarrow` + `pandas` s `usecols` filtrem (načte jen relevantní sloupce), cachuje v paměti a servíruje přes FastAPI. Žádný row limit, žádná Premium licence.

---

## Co to znamená pro tým, který mě najme

- **Nebudu čekat na IT ticket** abych dostal přístup k billing datům — vím jak nastavit Cost Management export, RBAC, Managed Identity
- **Budu rozumět faktuře** na úrovni MeterCategory, MeterSubCategory, PaygCost vs. CostInBillingCurrency, reservation coverage, billing lag
- **Postavím nástroj** který odhalí plýtvání dřív než ho vidí finance — ne až na konci měsíce
- **Udržím to v provozu** — Terraform, CI/CD, scale-to-zero, zero secrets

---

## Technický základ (zkráceně)

| Oblast | Co konkrétně umím |
|---|---|
| Azure billing | Cost Management export formáty (Parquet/CSV), COLUMN_MAP normalizace, billing lag, PaygCost vs. billed cost |
| Azure SDK | `DefaultAzureCredential`, `azure-storage-blob`, `azure-mgmt-resource`, `azure-mgmt-compute`, `azure.monitor.query` |
| Data | pandas, pyarrow, in-memory cache s TTL, DuckDB (analýza exportů) |
| Infra | Terraform (ACA, ACR, Storage, Managed Identity, RBAC), GitHub Actions OIDC |
| Python | FastAPI, asyncio, SSE streaming, agentic AI loop (tool use) |
| Auth | Cloudflare Access + Entra ID, Managed Identity, zero long-lived secrets |

---

*Živá ukázka: `az-penny.mysak.fun` (přístup přes Cloudflare Access / Entra ID)*  
*Zdrojový kód: github.com/mysak7/azure-penny*
