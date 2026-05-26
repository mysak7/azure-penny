# Data Layer Analysis — DuckDB, CSV Scale & Column Optimisation

Analýza provedena May 2026 na základě live dat z produkčního exportu.

---

## 1. Aktuální stav dat

```
Container:  cost-exports
Folder:     penny/penny-prd-daily-export/20260501-20260531/
Soubory:    37 (Azure generuje 2× denně nový soubor místo přepsání)
Načítaný:   1 soubor — nejnovější dle last_modified (~1.6 MB)
```

Azure Cost Management export je **kumulativní v rámci billing periody** — každý soubor obsahuje
veškerá data od začátku měsíce. Discovery logika v `_discover_latest_blobs()` proto záměrně
bere pouze jediný nejaktuálnější soubor, aby nedocházelo k double-countingu.

---

## 2. Optimalizace: filtrování sloupců při parsování CSV

Azure Cost Management CSV exporty obsahují **65 sloupců**, z nichž aplikace využívá pouze **~11**.

**Před změnou** (`storage.py`, `_blob_to_dataframe`):
```python
pd.read_csv(buf)          # načte všech 65 sloupců
pd.read_csv(buf, compression="gzip")
```

**Po změně**:
```python
pd.read_csv(buf, usecols=_keep_col)          # načte jen 11 relevantních sloupců
pd.read_csv(buf, compression="gzip", usecols=_keep_col)
```

`_keep_col` je predikát který přijme sloupec pokud:
1. jeho lowercase název je v `COLUMN_MAP` (přesná shoda, case-insensitive), nebo
2. začíná prefixem `tag_` (per-tag sloupce některých export formátů)

**Zahozené sloupce** (příklady z produkčního exportu):
`invoiceId`, `billingAccountId`, `billingProfileId`, `productOrderId`, `meterId`,
`resourceLocation`, `effectivePrice`, `unitOfMeasure`, `chargeType`, `costInUsd`,
`exchangeRatePricingToBilling`, `isAzureCreditEligible`, `serviceInfo1`, `additionalInfo`,
`frequency`, `term`, `reservationId`, `benefitId`, `provider` … a dalších 35+.

### Dopad na RAM

| Fáze | Před | Po |
|---|---|---|
| BytesIO buffer (raw download) | plná velikost souboru | plná velikost souboru (nezměněno) |
| pandas parse peak RAM | ~5–8× velikost CSV | ~1–2× velikost CSV |
| Finální DataFrame v cache | 65 sloupců × N řádků | 11 sloupců × N řádků |

> **Pozn.:** `_read_blob_to_bytes()` stahuje celý soubor do paměti jako `bytes` před parsováním —
> to je dominantní náklad při velkých souborech a `usecols` ho neovlivní. Při současných
> ~1.6 MB je obojí zanedbatelné.

---

## 3. Analýza DuckDB

### Proč DuckDB, proč teď ne

DuckDB je in-process OLAP engine schopný přímo dotazovat Parquet/CSV soubory v Azure Blob Storage
přes vlastní Azure extension s predikátovým pushdownem — načítá pouze sloupce a řádky které
dotaz skutečně potřebuje.

**Proč teď ne:** při ~1.6 MB CSV je jakákoli optimalizace query vrstvy bezvýznamná.
pandas + celý file do RAM zvládá tento objem bez problémů.

### Kdy by DuckDB dával smysl

| Scénář | File size | Pandas | DuckDB |
|---|---|---|---|
| Aktuální (subscription scope, MCA) | ~1–5 MB | ✅ | zbytečné |
| EA, více subscriptions | 50–300 MB | ⚠️ pomalé | ✅ |
| EA billing account scope (celá org) | 500 MB – 5 GB | 💥 OOM | ✅ nutné |

ACA container má 0.5 vCPU / 1 GiB RAM. Po odečtení Python runtime (~150–200 MB) zbývá
zhruba **800 MB** pro data. Pandas načítá celý soubor jako BytesIO + DataFrame — praktický
strop je někde kolem **200–300 MB CSV** (s usecols optimalizací výše). Nad tím nastane OOM.

### Technická proveditelnost DuckDB v ACA

DuckDB Azure extension (verze ≥ 0.10) podporuje `CREDENTIAL_CHAIN` provider, který prochází
standardním Azure credential chainem **včetně Managed Identity** přes IMDS endpoint:

```python
import duckdb
conn = duckdb.connect()
conn.execute("INSTALL azure; LOAD azure;")
conn.execute("""
    CREATE SECRET azure_secret (
        TYPE AZURE,
        PROVIDER CREDENTIAL_CHAIN,
        ACCOUNT_NAME 'stprdeusmcucta'
    );
""")
df = conn.execute(
    "SELECT * FROM read_parquet('azure://cost-exports/path/*.parquet')"
).df()
```

`AZURE_CLIENT_ID` env var (pro User-Assigned MI) je DuckDB respektován automaticky —
stejně jako ho používá `DefaultAzureCredential` v azure-identity SDK.

**Ale** — DuckDB implementuje credential chain v C++, nezávisle na Python `azure-identity`.
Je to oddělená implementace; funguje v ACA, ale vyžaduje ověření v runtime.

### Co by se muselo přepsat

Komplikovaná logika v `_apply_column_map()` (JSON parsing tagů, regex inference app jména
z AKS managed RG, Karpenter system tags) se do SQL nepřeloží jednoduše — zůstala by v
Pythonu i při přechodu na DuckDB.

Nejrozumnější hybridní přístup:
1. DuckDB čte Parquet/CSV přímo z Blob Storage (predicate pushdown, column pruning)
2. `.df()` vrátí pandas DataFrame
3. `_apply_column_map()` a veškerá tag logika zůstanou beze změny
4. Výsledek se cachovuje stejně jako dnes

### Blocker pro enterprise (nesouvisí s DuckDB)

U MCA/EA subscriptions nelze vytvořit cost export na **billing account scope** přes Terraform/API
— RBAC na billing scope je přiřaditelný pouze ručně přes Azure Portal / EA Portal.
Viz komentář v `terraform/cost_export.tf`.

DuckDB tento problém **neřeší** — jde o permissions/IAM blocker, nikoli o výkon query vrstvy.
DuckDB by pomohl až poté, co jsou exporty dostupné v Blob Storage.

---

## 4. Kapacita po optimalizaci

S `usecols` filtrem zvládne aplikace bez změny infrastruktury přibližně:

| Export size | RAM peak (BytesIO + DF) | Zvládne ACA (1 GiB) |
|---|---|---|
| 1.6 MB (aktuálně) | ~20 MB | ✅ |
| 50 MB | ~80 MB | ✅ |
| 150 MB | ~220 MB | ✅ |
| 300 MB | ~420 MB | ✅ (těsně) |
| 500 MB | ~700 MB | ⚠️ riziko OOM |
| 1 GB+ | > 1 GB | ❌ OOM |

Bottleneck při velkých souborech je `_read_blob_to_bytes().readall()` — celý soubor se stáhne
do RAM jako `bytes` před parsováním. Toto `usecols` neovlivní; řešením by bylo streamované
čtení nebo přechod na DuckDB s přímým přístupem k Blob Storage.
