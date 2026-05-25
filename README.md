# azure-penny

[![CI](https://github.com/mysak7/azure-penny/actions/workflows/ci.yml/badge.svg)](https://github.com/mysak7/azure-penny/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A serverless Azure cost management dashboard. Reads Cost Management Parquet/CSV exports from Azure Blob Storage and serves aggregated cost data through a FastAPI web app deployed on Azure Container Apps.

> **Screenshots:** Dashboard · Live Resources · Forecast
>
> *(Add your own screenshots here — e.g. `docs/screenshots/dashboard.png`)*

## Features

- **Cost dashboard** — daily spend by resource group and service category, current-month MTD total
- **Forecast** — linear extrapolation of MTD spend to end-of-month estimate, scoped per resource group
- **Live Resources tab** — real-time inventory of all subscription resources with cost correlation and spot-price savings hints
- **Scale to zero** — Container App idles at 0 replicas; you pay nothing when nobody is looking at it
- **No secrets** — authenticates entirely via User-Assigned Managed Identity; no connection strings, SAS tokens, or service principal secrets anywhere
- **Easy Auth** — Entra ID login gate on the Container App; `penny-admin` app role controls delete access

## Prerequisites

| Requirement | Notes |
|---|---|
| Azure subscription | Pay-As-You-Go, EA, or MCA — **not** a free trial (Cost Management exports require a paid subscription) |
| Azure Entra ID (AAD) | Required for Easy Auth and the Entra app registration provisioned by Terraform |
| Terraform ≥ 1.12 | State is stored in Azure Blob Storage (bootstrapped separately) |
| Docker | For building the container image locally or in CI |
| Azure CLI | For `az login` during local development and for the bootstrap scripts |
| GitHub repository | CI/CD workflows use GitHub Actions OIDC — no stored Azure credentials |

> **Entra ID note:** The Terraform `auth.tf` creates an app registration and Easy Auth config. This requires an account with permission to register applications in your Entra tenant (Application Administrator or Global Administrator role, or a delegated permission).

## Architecture

```
Azure Cost Management
        │  (daily scheduled export)
        ▼
Azure Blob Storage ──── Storage Blob Data Reader ────┐
                                                      │ (Managed Identity)
Azure Container Registry                              ▼
        │  (docker image)                  Azure Container App
        └──────────────────────────────────► (azure-penny, scales 0→1)
```

**Infrastructure (Terraform-managed):**

| Resource | Purpose |
|---|---|
| Resource Group | Isolates all azure-penny resources |
| Storage Account + Blob Container | Holds Cost Management Parquet/CSV exports |
| Azure Container Registry (ACR) | Stores the Docker image |
| User-Assigned Managed Identity | Passwordless auth for Blob + ACR access |
| RBAC: Storage Blob Data Reader | Grants the identity read access to the storage account |
| RBAC: AcrPull | Grants the identity pull access to the registry |
| Container App Environment | Shared ACA hosting environment (with Log Analytics) |
| Container App | The running app, scales to zero when idle |

Terraform state is stored in Azure Blob Storage (`azure-penny-tfstate-rg` / `azurepennytff04cd1`).

## Repository layout

```
.
├── main.py                  # FastAPI application
├── Dockerfile               # Container build
├── requirements.txt         # Python dependencies
├── .env.example             # Local dev environment variables
├── templates/               # Jinja2 HTML templates
├── terraform/
│   ├── main.tf              # Provider config + Terraform backend
│   ├── variables.tf
│   ├── outputs.tf
│   ├── resource_group.tf
│   ├── storage.tf
│   ├── acr.tf
│   ├── identity.tf
│   ├── container_app.tf
│   └── cost_export.tf       # Azure Cost Management export schedule
└── .github/workflows/
    ├── ci.yml               # PR: lint (ruff), terraform validate, docker build
    ├── cd.yml               # Push to main: build + push image → deploy to ACA
    └── terraform.yml        # Push to main (terraform/**): plan/apply
```

## Prerequisites — data pipeline

> **This is the most common reason the dashboard shows no data.** The Cost Management export must exist in Blob Storage before the app can load anything.

### Step 1 — Create a Cost Management export (manual — Terraform cannot do this for MCA accounts)

> ⚠️ `terraform/cost_export.tf` was removed from state because Azure does not permit billing-scope RBAC assignments via REST API for MSA-owned MCA accounts. The export must be created once manually in the Azure Portal or via CLI.

**Azure Portal:**

1. Open **Azure Portal → Cost Management + Billing → Cost Management → Exports**
2. Click **+ Add**
3. Set the following:

   | Field | Value |
   |---|---|
   | Export name | any name (e.g. `penny-daily-export`) |
   | Export type | Daily export of month-to-date costs |
   | Start date | today |
   | Format | **Parquet** |
   | Compression | Snappy (default) |
   | Storage account | the one Terraform created (see `terraform output storage_account_name`) |
   | Container | `cost-exports` (or whatever `STORAGE_CONTAINER_NAME` is set to) |
   | Directory | leave empty or set to `exports` |

4. Click **Create**, then **Run now** to trigger the first immediate export (otherwise it runs the next day).

**Azure CLI alternative:**

```bash
STORAGE_ACCOUNT=$(terraform -chdir=terraform output -raw storage_account_name)
SUBSCRIPTION_ID=$(az account show --query id -o tsv)

az costmanagement export create \
  --name "penny-daily-export" \
  --scope "subscriptions/$SUBSCRIPTION_ID" \
  --type "ActualCost" \
  --timeframe "MonthToDate" \
  --recurrence "Daily" \
  --recurrence-period from="$(date -u +%Y-%m-%dT00:00:00Z)" to="2099-12-31T00:00:00Z" \
  --storage-account-id "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$(terraform -chdir=terraform output -raw resource_group_name)/providers/Microsoft.Storage/storageAccounts/$STORAGE_ACCOUNT" \
  --storage-container "cost-exports" \
  --storage-directory "" \
  --format Parquet
```

### Step 2 — Trigger the first export run

Exports run once daily by default. To get data immediately after creation:

```bash
az costmanagement export run \
  --name "penny-daily-export" \
  --scope "subscriptions/$(az account show --query id -o tsv)"
```

Files appear in Blob Storage within a few minutes.

### Step 3 — Verify the Managed Identity can read the blobs

The Terraform-managed identity (`id-{env}-{location_short}-penny`) already has `Storage Blob Data Reader` assigned on the Storage Account. Verify:

```bash
az role assignment list \
  --assignee "$(terraform -chdir=terraform output -raw managed_identity_client_id)" \
  --query "[].{role:roleDefinitionName, scope:scope}" \
  -o table
```

Expected output includes `Storage Blob Data Reader` and `Contributor`.

### What the app expects in Blob Storage

Azure Cost Management writes exports using this path convention:

```
{export-name}/{YYYYMMDD-YYYYMMDD}/{guid}/{filename}.parquet
```

Example:
```
penny-daily-export/20250501-20250601/a1b2c3d4-e5f6.../penny-daily-export_20250525.parquet
```

The app automatically discovers the folder with the **most recent date range** and loads all Parquet (or CSV/CSV.GZ) files from it.

### Troubleshooting — no data

1. Check blobs exist: `az storage blob list --account-name $STORAGE_ACCOUNT --container-name cost-exports --query "[].name" -o tsv`
2. If empty → export hasn't run yet. Trigger it manually (see Step 2 above).
3. Check Container App logs: Azure Portal → Container App → Log stream, or:
   ```bash
   az containerapp logs show --name <app-name> --resource-group <rg> --follow
   ```
   Look for `Cache miss` log line and any errors below it.
4. Confirm `STORAGE_ACCOUNT_NAME` env var matches the actual storage account name.

---

## Application

The app discovers the latest export folder in Blob Storage using the path convention written by Azure Cost Management:

```
{export-name}/{YYYYMMDD-YYYYMMDD}/{guid}/{filename}.parquet
```

It merges all Parquet (or CSV/CSV.GZ) files from the most-recent date folder into a single Pandas DataFrame, caches the result in memory, and serves it through FastAPI endpoints.

**Key environment variables:**

| Variable | Required | Default | Description |
|---|---|---|---|
| `STORAGE_ACCOUNT_NAME` | Yes | — | Storage account holding exports |
| `STORAGE_CONTAINER_NAME` | No | `cost-exports` | Blob container name |
| `AZURE_CLIENT_ID` | No | — | Client ID of user-assigned managed identity |
| `AZURE_SUBSCRIPTION_ID` | No | — | Azure subscription ID |
| `CACHE_TTL_SECONDS` | No | `3600` | How long to cache the loaded DataFrame |
| `LIVE_CACHE_TTL_SECONDS` | No | `900` | Live data cache TTL (15 min default) |
| `PROTECTED_RGS` | No | — | Comma-separated RG names the app refuses to delete (e.g. `rg-prd-eus-penny`) |

Authentication uses `DefaultAzureCredential`, which resolves automatically in:
- Azure Container Apps (via the attached managed identity)
- Local dev (Azure CLI or VS Code credential)
- CI/CD (service principal via environment variables)

## IAM / Permissions

The managed identity holds **`Contributor` at subscription scope**. This is intentional:

- **Read** — lists all resources in the subscription for the Live Resources tab
- **Write** — allows the optional delete action on the Live Resources tab (gated behind the `penny-admin` Entra app role)

If you don't need delete functionality, change `role_definition_name` in `terraform/identity.tf` from `Contributor` to `Reader`. The azure-penny resource group is always in `PROTECTED_RGS` so the app cannot delete itself.

See [SECURITY.md](SECURITY.md) for a full security design summary.

## Local development

```bash
# 1. Copy env template and fill in your values
cp .env.example .env

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the app
uvicorn main:app --reload
```

The app will be available at `http://localhost:8000`.

## CI/CD

Three GitHub Actions workflows handle the full lifecycle:

**CI** (pull requests to `main`):
- Python linting and formatting with `ruff`
- `terraform validate` + format check
- Docker build (no push)

**CD** (push to `main`):
- Builds and pushes the Docker image to ACR (tagged with commit SHA + `latest`)
- Updates the Container App to the new image revision
- Requires the `production` environment (manual approval gate if configured)

**Terraform** (push to `main` when `terraform/**` changes, or manual dispatch):
- Runs `terraform plan` on PRs (posts plan output as a PR comment)
- Runs `terraform apply` on merge to `main`
- Requires the `production` environment for apply

All workflows authenticate to Azure via OIDC (no stored secrets).

**Required GitHub repository variables:**

| Variable | Description |
|---|---|
| `AZURE_CLIENT_ID` | Service principal / federated identity client ID |
| `AZURE_TENANT_ID` | Azure tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Azure subscription ID |
| `ACR_LOGIN_SERVER` | ACR login server (e.g. `myacr.azurecr.io`) |
| `ACR_NAME` | ACR resource name |
| `CONTAINER_APP_NAME` | Container App resource name (prd) |
| `RESOURCE_GROUP_NAME` | Resource group name (prd) |
| `DEV_CONTAINER_APP_NAME` | Container App resource name (dev branch) |
| `DEV_RESOURCE_GROUP_NAME` | Resource group name (dev branch) |
| `BILLING_PROFILE_ID` | MCA billing profile resource ID (for cost export) |

## Infrastructure deployment

```bash
# 1. Copy and fill in the variable values
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
# Edit terraform.tfvars — set subscription_id, cicd_principal_object_id, owner_email

cd terraform

# 2. Initialise with the remote backend (see scripts/bootstrap-tfstate.sh for first-time setup)
terraform init

# 3. Preview changes
terraform plan -var="container_image=<acr-login-server>/azure-penny:latest"

# 4. Apply
terraform apply -var="container_image=<acr-login-server>/azure-penny:latest"
```

**Bootstrap sequence (first deploy):**

1. Run `terraform apply` — deploys all infrastructure. The Container App starts with `nginx:latest` as a placeholder.
2. Push to `main` — the CD workflow builds the real image and updates the Container App.
3. The app is live after step 2. Step 1 alone will show nginx behind the Easy Auth login.

**Budget alerts** are not managed by Terraform. Run once to create per-$10 subscription alerts:

```bash
./scripts/set-budget-alerts.sh your@email.com 200
```

Key outputs after apply:

| Output | Description |
|---|---|
| `container_app_url` | Public HTTPS URL of the deployed app |
| `acr_login_server` | ACR hostname to use for image tags |
| `managed_identity_client_id` | Client ID to set as `AZURE_CLIENT_ID` in the Container App |
| `storage_account_name` | Storage account name to configure Cost Management exports against |
