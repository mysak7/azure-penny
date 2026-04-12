# Architecture — azure-penny

## Overview

**azure-penny** is a serverless Azure cost-visibility dashboard. It reads Cost Management exports from Azure Blob Storage, aggregates them in-memory with pandas, and serves interactive HTML dashboards and a JSON API via FastAPI. The application runs as a container on Azure Container Apps and scales to zero when idle.

```
┌─────────────────────────────────────────────────────────────────┐
│  GitHub Actions                                                  │
│  ┌──────────┐   OIDC   ┌──────────────────────────────────────┐ │
│  │  CI      │─────────▶│  Azure                               │ │
│  │  CD      │          │  ┌──────────────────────────────────┐│ │
│  └──────────┘          │  │  Resource Group: azure-penny-rg  ││ │
│                        │  │                                  ││ │
│                        │  │  ACR ──push/pull──▶ ACA Env     ││ │
│                        │  │                       │          ││ │
│                        │  │  Storage Account      │ identity ││ │
│                        │  │  └─ cost-exports  ◀───┘  (MI)   ││ │
│                        │  │       ▲                          ││ │
│                        │  │  Cost Management Export (daily)  ││ │
│                        │  └──────────────────────────────────┘│ │
│                        └──────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

---

## Infrastructure (Terraform)

All cloud resources are declared in `terraform/` using the `hashicorp/azurerm ~> 4.67` provider. State is stored remotely in a dedicated storage account (`azurepennytf3759` / container `tfstate`).

| File | What it provisions |
|---|---|
| `main.tf` | Provider config, backend, `Microsoft.App` provider registration |
| `resource_group.tf` | Single resource group for all app resources |
| `storage.tf` | Storage Account (BlobStorage, LRS, TLS 1.2, versioning) + `cost-exports` container |
| `acr.tf` | Azure Container Registry (Standard, admin disabled) + `AcrPull` role assignment |
| `identity.tf` | User-Assigned Managed Identity + `Storage Blob Data Reader` + `Contributor` role assignments |
| `container_app.tf` | Log Analytics workspace, ACA Environment, Container App (scale 0→1) |
| `cost_export.tf` | `azurerm_subscription_cost_management_export` — daily MonthToDate CSV export |
| `variables.tf` | `subscription_id`, `location`, `app_name`, `environment`, `container_image`, `tags` |
| `outputs.tf` | Resource group name, storage account name, ACR login server, app URL, MI client ID |

### Key design decisions

**Scale to zero** — `min_replicas = 0`, `max_replicas = 1` with an HTTP scale rule (10 concurrent requests). The app pays nothing when nobody is using it. Cold-start latency is acceptable for a cost dashboard that is opened once or twice a day.

**Single revision mode** — only one active revision at a time. Blue/green is unnecessary for this use case; simplicity wins.

**LRS replication** — cost export files are re-created daily by Azure Cost Management, so geo-redundancy (GRS) would be wasted spend.

**Blob versioning + soft delete (7 days)** — guards against accidental deletion of export files without adding meaningful cost.

---

## Authentication — no secrets, ever

The application authenticates to Azure services exclusively through **User-Assigned Managed Identity** + `DefaultAzureCredential`. No connection strings, SAS tokens, or service principal secrets are stored anywhere.

```
Container App
  └─ UserAssignedIdentity (AZURE_CLIENT_ID env var)
       ├─ Storage Blob Data Reader  →  reads cost-exports blobs
       ├─ AcrPull                   →  pulls container image from ACR
       └─ Contributor (subscription) →  lists/deletes resources (Live tab)
```

`DefaultAzureCredential` resolves the identity automatically:
- **Production (ACA):** managed identity via IMDS endpoint
- **Local dev:** Azure CLI credential (`az login`)
- **CI/CD:** service principal via `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID` env vars

### Why not a service principal with a secret?

Secrets rotate, expire, and leak. Managed identity removes the entire secret-management lifecycle at zero extra cost.

---

## Application Layer (FastAPI / Python)

### Data flow

```
Azure Cost Management
  └─ writes daily CSV export
       └─ Blob Storage (cost-exports container)
            └─ _discover_latest_blobs()
                 └─ picks newest YYYYMMDD-YYYYMMDD folder
                      └─ picks single most-recently-modified file (avoids double-counting)
                           └─ _blob_to_dataframe() → pandas DataFrame
                                └─ _apply_column_map() → internal C_* columns
                                     └─ in-memory cache (TTL 1h)
                                          └─ FastAPI routes → HTML / JSON
```

### Export file discovery strategy

Azure Cost Management daily exports are **cumulative within a billing period** — each run overwrites or adds a new file in the same date-range folder. Reading multiple files from the same folder would multiply costs. The discovery logic therefore:

1. Finds the lexicographically latest `YYYYMMDD-YYYYMMDD` folder.
2. Within that folder, keeps only the file with the latest `last_modified` timestamp.

### Column mapping (`COLUMN_MAP`)

Azure exports different column names depending on agreement type (EA, MCA, CSP). The map normalises them to internal `C_*` names:

| Azure column | Internal name | Notes |
|---|---|---|
| `CostInBillingCurrency` / `Cost` / `PreTaxCost` | `C_COST` | Agreement-specific |
| `MeterCategory` / `ServiceName` | `C_SERVICE` | |
| `ResourceGroup` / `ResourceGroupName` | `C_NAME` | |
| `SubscriptionName` / `SubscriptionId` | `C_ACCOUNT` | |
| `Date` / `UsageDate` | `C_DATE` | Normalised to `YYYY-MM-DD` |
| `ResourceId` / `InstanceId` | `C_RESOURCE_ID` | Used by Live Resources tab |

### Caching

A single `asyncio.Lock` guards the shared in-memory cache dict. Blocking pandas I/O runs in a thread pool executor so the event loop is never blocked:

```python
df = await asyncio.get_event_loop().run_in_executor(None, _load_dataframe)
```

TTL is 3 600 s (1 h) for cost data and 900 s (15 min) for live resource data. Both are configurable via environment variables.

### Live Resources tab

Uses `azure-mgmt-resource` and `azure-mgmt-compute` SDKs to list all resources in the subscription, correlate them with cost data, and surface spot-price savings opportunities. The managed identity needs `Contributor` (read is sufficient; write is used for the optional destroy action).

---

## CI/CD

### CI (pull requests → `main`)

| Job | Tool | What it checks |
|---|---|---|
| `lint-python` | `ruff check` + `ruff format --check` | Code style |
| `terraform-validate` | `terraform fmt -check` + `terraform validate` | HCL correctness |
| `docker-build` | `docker/build-push-action` (no push) | Image builds cleanly |

### CD (push to `main`)

1. **Build & Push** — builds the Docker image, tags it with `github.sha` and `latest`, pushes to ACR. Uses GitHub Actions OIDC (no stored Azure credentials).
2. **Deploy** — updates the Container App to the new image tag via `az containerapp update`.

OIDC login (`azure/login@v2`) uses `client-id` / `tenant-id` / `subscription-id` stored as GitHub Actions variables — no secrets in the repository.

---

## Alternatives Considered

### Hosting

| Option | Why rejected |
|---|---|
| **Azure App Service** | Always-on minimum instance; higher baseline cost for a low-traffic dashboard |
| **AKS** | Massive operational overhead for a single-container app |
| **Azure Functions** | HTTP trigger cold-start on a Python app with pandas + pyarrow (~500 MB) is too slow; execution time limits complicate large file reads |
| **Azure Container Apps** ✓ | Serverless, scale-to-zero, native container support, no cluster to manage |

### Storage / data access

| Option | Why rejected |
|---|---|
| **Azure SQL / Synapse** | Overkill; cost exports are append-only daily files, not a transactional workload |
| **Azure Data Lake Storage Gen2** | Adds complexity with no benefit at this data volume |
| **Direct Cost Management API** | Rate-limited, pagination-heavy, and returns only 1 month at a time; export files are simpler and already available |
| **Blob Storage + pandas in-memory** ✓ | Simple, fast for daily CSVs up to tens of MB, no additional services |

### Authentication

| Option | Why rejected |
|---|---|
| **Storage Account connection string** | Secret rotation burden; leaks give full account access |
| **SAS token** | Expiry management; hard-coded tokens are a security risk |
| **Service principal + secret** | Secret lifecycle (rotation, expiry, storage in Key Vault) adds complexity |
| **Managed Identity + DefaultAzureCredential** ✓ | Zero secrets, works identically in ACA and local dev, no rotation needed |

### Infrastructure as Code

| Option | Why rejected |
|---|---|
| **Bicep / ARM** | Less portable; Terraform state management and module ecosystem are more mature |
| **Pulumi** | Additional language runtime dependency; team already familiar with Terraform HCL |
| **Terraform (azurerm)** ✓ | Industry standard, remote state in Azure Blob, CI validation with `terraform validate` |

### Export format

| Option | Why rejected |
|---|---|
| **Parquet** | `azurerm_subscription_cost_management_export` does not support Parquet at the subscription scope (provider limitation as of Apr 2026) |
| **CSV** ✓ | Supported by the provider; pandas reads it with equal ease; app handles both `.csv` and `.parquet` for forward compatibility |

---

## Repository Structure

```
azure-penny/
├── main.py                  # FastAPI app — blob discovery, data loading, routes
├── Dockerfile               # python:3.11-slim, uvicorn entrypoint
├── requirements.txt         # fastapi, pandas, azure-storage-blob, azure-identity, …
├── templates/               # Jinja2 HTML templates
├── terraform/
│   ├── main.tf              # Provider + backend
│   ├── variables.tf
│   ├── outputs.tf
│   ├── resource_group.tf
│   ├── storage.tf
│   ├── acr.tf
│   ├── identity.tf
│   ├── container_app.tf
│   └── cost_export.tf
└── .github/workflows/
    ├── ci.yml               # PR checks (lint, terraform validate, docker build)
    ├── cd.yml               # Push to main → build + deploy
    └── terraform.yml        # Manual terraform plan/apply
```
