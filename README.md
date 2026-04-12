# azure-penny

A serverless Azure cost management dashboard. Reads Cost Management Parquet/CSV exports from Azure Blob Storage and serves aggregated cost data through a FastAPI web app deployed on Azure Container Apps.

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

Terraform state is stored in Azure Blob Storage (`azure-penny-tfstate-rg` / `azurepennytf3759`).

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

Authentication uses `DefaultAzureCredential`, which resolves automatically in:
- Azure Container Apps (via the attached managed identity)
- Local dev (Azure CLI or VS Code credential)
- CI/CD (service principal via environment variables)

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
| `CONTAINER_APP_NAME` | Container App resource name |
| `RESOURCE_GROUP_NAME` | Resource group name |

## Infrastructure deployment

```bash
cd terraform

# First time — initialise with the remote backend
terraform init

# Preview changes
terraform plan -var="container_image=<acr-login-server>/azure-penny:latest"

# Apply
terraform apply -var="container_image=<acr-login-server>/azure-penny:latest"
```

Key outputs after apply:

| Output | Description |
|---|---|
| `container_app_url` | Public HTTPS URL of the deployed app |
| `acr_login_server` | ACR hostname to use for image tags |
| `managed_identity_client_id` | Client ID to set as `AZURE_CLIENT_ID` in the Container App |
| `storage_account_name` | Storage account name to configure Cost Management exports against |
