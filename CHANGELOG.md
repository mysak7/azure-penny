# Changelog

All notable changes to **azure-penny** are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)  
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

---

## [Unreleased]

---

## [1.3.0] – 2026-05-25

### Added
- **Application filter** (`project` tag) — `App:` dropdown in the header of all three views
  (Manager, Technician, Live), independent AND-combined with the existing RG filter
- **`GET /api/apps`** endpoint — lists distinct `project` tag values with their cost for the
  selected period; used to dynamically populate the App dropdown
- **`C_APP` column** derived at load time from `Tags` JSON (`project` key) or a dedicated
  `tag_project` export column; falls back to `"Untagged"` when the tag is absent
- `app: str = ""` query parameter added to all cost endpoints:
  `/api/compute`, `/api/storage`, `/api/storage/breakdown`, `/api/network`, `/api/database`,
  `/api/monitoring`, `/api/other`, `/api/services`, `/api/resource-groups`, `/api/anomalies`,
  `/api/breakdown`, `/api/daily`, `/api/year`, `/api/compute/machines`
- Live view: `app` field added to each resource object (`project` tag from ARM); App dropdown
  filters client-side (resources already loaded in-memory)

### Changed
- `_filter_app()` helper follows the same pattern as `_filter_rg()` — clean AND semantics
- `_extract_project_tag()` handles JSON Tags strings, empty/null/`{}`/`nan` values gracefully

---

## [1.2.0] – 2026-05-24

### Fixed
- **Category coverage gap** – services missing from `CAT_*` sets appeared in **Total** but not in any
  category card. Root cause: Azure billing uses `"Azure Container Apps"` (with prefix) but the code
  matched only `"Container Apps"`. Verified from live billing data (`/api/debug`).
  - `CAT_COMPUTE`: added `Azure Container Apps`, `Container Registry`, `Azure App Service`,
    `NAT Gateway` and other `Azure …`-prefixed billing name variants
  - `CAT_STORAGE`, `CAT_NETWORK`, `CAT_DATABASE`: expanded with both bare and prefixed forms
  - `CAT_MONITORING`: added `Key Vault` / `Azure Key Vault` (secrets management / security)

### Added
- **`/api/other` endpoint** – catch-all for spend not covered by any standard category
- **Other / Uncategorized** KPI card (amber) and category detail card in dashboard
- **"Other" pill** added to Cost Breakdown category selector
- `ALL_KNOWN_SERVICES` frozenset (union of all `CAT_*`) used for exclusion logic
- Breakdown chart `"all"` view now exhaustive – stacked bars sum to 100 % of total spend
- **"Monitoring & Security"** rename (was "Monitoring") reflecting Key Vault inclusion

### Changed
- KPI grid changed from `repeat(5, 1fr)` to `repeat(4, 1fr)`; Total card spans 2 columns —
  eliminates the 3-empty-column second row

---

## [1.1.0] – 2026-05-24

### Added
- **Forecast chart** – stacked bar chart visible on **all period tabs** (Day / Week / Month / Year),
  replaces the former Year-only breakdown card.
  - **Category selector** (amber pills): All / Compute / Storage / Network / Database / Monitoring
  - **Granularity selector** (green pills): Days / Weeks / Months
    - Auto-restricted per period (e.g. Day only allows Days)
    - Last selection remembered per period in `localStorage`
  - `All` mode → stacked by service category per time bucket
  - Single-category mode → stacked by top-8 sub-services within that category (sorted by total cost)
  - End-of-month projected cost displayed as text badge in card header
  - ISO week bucketing (`%G-W%V`), month (`YYYY-MM`) and day (`YYYY-MM-DD`) granularities
- **`GET /api/breakdown`** endpoint: `period × granularity × category × rg`
  - Returns `buckets[]` (time-bucketed stacked cost), `stack_keys[]`, `forecast_text`
- App **version badge** shown in header on all pages (`v{{ version }}` via Jinja2)
- **`version`** field added to `GET /api/status` response

### Removed
- Year-only breakdown card (`#year-card`) and `loadYearChart()` JS function —
  superseded by the new Forecast chart

---

## [1.0.0] – 2026-04-01

### Added
- **Manager dashboard**
  - KPI row: Compute / Storage / Network / Database / Monitoring / Total
  - Period pills: Day / Week / Month / Year
  - Resource-group filter with ghost-filter options (All RGs / No zero cost / No empty)
- **Monthly Forecast card**
  - End-of-month projection with actual vs. projected daily bar chart
  - Top-3 resource groups breakdown
  - Uses live ARM pricing when available, linear extrapolation as fallback
  - Billing-lag gap filled with zero-cost bars; today moved to projected section
- **Year period** – 12-month cost history bar chart with current-year projection for remaining months
- **Technician view**
  - All-services tab (billing CSV analysis)
  - Anomaly detection: week-over-week cost spikes (>50%), new/vanished services
  - Untagged resources report
  - Resource-group cost breakdown
- **Live view**
  - Real-time ARM resource inventory with per-resource monthly cost estimates
  - Cost source tag breakdown (export / spot_rate / price_table / …)
  - Admin-only resource deletion and resource-group deletion via streaming log
- **Azure Blob Storage integration**
  - Auto-discovers latest Parquet exports from Cost Management
  - In-memory cache with manual reload via `POST /api/reload`
  - `DefaultAzureCredential` (Managed Identity in ACA, `az login` locally)
- **Live ARM inventory** – enriched with spot/retail pricing from Azure Retail Prices API
- **Scale-to-zero** – Azure Container Apps with Managed Identity, ACR, Storage Account (Terraform)
- **Cloudflare Access** – all URLs protected via Entra ID authentication
- **API endpoints**: `GET /api/status`, `GET /api/forecast`, `GET /api/resource-groups-list`,
  `GET /api/resource-groups`, `GET /api/anomalies`, `GET /api/daily`, `GET /api/year`,
  `GET /api/live-resources`, `GET /api/services`, `GET /api/compute`, `GET /api/storage`,
  `GET /api/network`, `GET /api/database`, `GET /api/monitoring`,
  `DELETE /api/resource`, `DELETE /api/resource-group`, `DELETE /api/resource-groups/all`

### Fixed
- Forecast: today moved from zero actual bar into projected section (billing lag)
- Ghost filters use ARM membership rather than cost presence
- Live tab RG list restored to cost API
- `localStorage` ghost-filter state replaced with plain JS variable
- Always fetch ARM RG names for `resource-groups-list` endpoint
- Warm live inventory cache on app startup via `lifespan` task
- Fixed/sticky header with sticky nav bar
- Cost shown in Manager RG dropdown

---

[Unreleased]: https://github.com/your-org/azure-penny/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/your-org/azure-penny/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/your-org/azure-penny/releases/tag/v1.0.0
