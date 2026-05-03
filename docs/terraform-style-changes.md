# Terraform Style Changes

This document explains the changes made to align the Terraform code with the
[carlzxc71 / lindbergtech style guide](https://github.com/carlzxc71).

---

## 1. Exact provider version pins

**Before**
```hcl
version = "~> 4.67"
version = "~> 3.5"
```

**After**
```hcl
version = "4.67.0"
version = "3.8.1"
```

The style guide requires an exact pin — no range operators. Exact versions are
read directly from `.terraform.lock.hcl` so the pin matches what is already
installed. This makes provider upgrades a deliberate, auditable change rather
than a silent side-effect of `terraform init`.

---

## 2. Empty backend block + init scripts

**Before** — `main.tf` held hardcoded backend values:
```hcl
backend "azurerm" {
  resource_group_name  = "azure-penny-tfstate-rg"
  storage_account_name = "azurepennytff04cd1"
  ...
}
```

**After** — empty block in `main.tf`:
```hcl
backend "azurerm" {}
```

Backend config is now supplied at init time via `-backend-config` flags. Two
scripts were added:

| Script | Purpose |
|--------|---------|
| `scripts/prepare-prd.sh` | One-time bootstrap of the state storage account |
| `scripts/init-prd.sh` | `terraform init` with the correct backend flags |

The CI workflow (`terraform.yml`) was updated to pass the same flags in the
`Terraform Init` step.

The existing state storage account (`azurepennytff04cd1`) is unchanged — `init-prd.sh`
points to it so no state migration is needed.

---

## 3. Removed `required_version`

The style guide does not include `required_version` in the `terraform {}` block.
Removing it avoids an additional constraint that is already implicitly enforced
by exact provider pins and CI's pinned `TF_VERSION`.

---

## 4. Logical resource names: `main` → `this`

Every singleton resource was renamed from `.main` to `.this`.

| File | Old | New |
|------|-----|-----|
| `resource_group.tf` | `azurerm_resource_group.main` | `azurerm_resource_group.this` |
| `storage.tf` | `azurerm_storage_account.main` | `azurerm_storage_account.this` |
| `storage.tf` | `azurerm_storage_container.cost_exports` | `azurerm_storage_container.this` |
| `acr.tf` | `azurerm_container_registry.main` | `azurerm_container_registry.this` |
| `identity.tf` | `azurerm_user_assigned_identity.main` | `azurerm_user_assigned_identity.this` |
| `container_app.tf` | `azurerm_log_analytics_workspace.main` | `azurerm_log_analytics_workspace.this` |
| `container_app.tf` | `azurerm_container_app_environment.main` | `azurerm_container_app_environment.this` |
| `container_app.tf` | `azurerm_container_app.main` | `azurerm_container_app.this` |
| `budget_alert.tf` | `azurerm_monitor_action_group.budget_email` | `azurerm_monitor_action_group.this` |
| `budget_alert.tf` | `azurerm_consumption_budget_subscription.alert_per_10usd` | `azurerm_consumption_budget_subscription.this` |

Descriptive names are kept where there are multiple resources of the same type
in one file (e.g. `acr_pull`, `storage_blob_reader`, `subscription_contributor`).

---

## 5. Azure resource naming convention

**Before** — ad-hoc pattern using `app_name`:
```hcl
name = "${var.app_name}-${var.environment}-rg"
```

**After** — style-guide pattern `<abbr>-<env>-<location_short>-<descriptor>`:
```hcl
name = "rg-${var.environment}-${var.location_short}-penny"
```

Full mapping:

| Resource | Old name | New name |
|----------|----------|----------|
| Resource Group | `azure-penny-prd-rg` | `rg-prd-eus-penny` |
| Storage Account | `azurepennyprodsaXXXXXX` | `stprdeus<suffix>` |
| Storage Container | `cost-exports` | `cost-exports` (unchanged) |
| ACR | `azurepennyproducracr` | `crprdeusspenny` |
| User Assigned Identity | `azure-penny-prd-identity` | `id-prd-eus-penny` |
| Log Analytics Workspace | `azure-penny-prd-logs` | `log-prd-eus-penny` |
| Container App Environment | `azure-penny-prd-env` | `cae-prd-eus-penny` |
| Container App | `azure-penny-prd` | `ca-prd-eus-penny` |
| Action Group | `azure-penny-budget-ag` | `ag-prd-eus-budget` |
| Budget | `azure-penny-per-10usd` | `bgt-prd-eus-penny` |

Storage accounts follow the no-hyphen rule: `st<env><location_short><suffix>`.
ACR names are alphanumeric only: `cr<env><location_short>penny`.

### State migration (important)

Renaming Azure resources means Terraform will plan a destroy + recreate. To
rename in-place without downtime, use `terraform state mv` for each resource
before applying:

```bash
terraform state mv \
  azurerm_resource_group.main \
  azurerm_resource_group.this

terraform state mv \
  azurerm_storage_account.main \
  azurerm_storage_account.this

# ... repeat for each renamed logical name
```

After moving state addresses, verify `terraform plan` shows only name-change
diffs (where Terraform can update in place) vs forced-replace diffs (which
require destroy/recreate). Resources like ACR and Container App **can** be
renamed in place; storage accounts **cannot** (they are force-new).

---

## 6. Added `location_short` variable, removed `app_name`

`location_short` is a required component of the naming convention.
`app_name` was replaced by the hardcoded descriptor `penny` in all names.

Default values:
- `location` → `swedencentral`
- `location_short` → `sc`

The `prd.tfvars` file overrides these to `eastus` / `eus` for the existing
production deployment.

---

## 7. `variables/` directory with per-env tfvars

**New files:**
- `terraform/variables/prd.tfvars.example` — production values template (eastus)
- `terraform/variables/dev.tfvars.example` — development values template (swedencentral)

The `.gitignore` blocks `*.tfvars` to prevent committing secrets. Copy the
`.example` file, fill in `subscription_id`, and use locally:
```bash
cp terraform/variables/prd.tfvars.example terraform/variables/prd.tfvars
```

The CI plan step was updated to pass `-var-file=variables/prd.tfvars`.

---

## 8. `data` sources consolidated into `main.tf`

`data "azurerm_subscription" "current"` was previously declared in
`cost_export.tf`. Both data sources now live in `main.tf` per the style guide's
ordering rule: `terraform{}` → `provider{}` → `locals{}` → `data{}` → `resource{}`.

---

## 9. Comments style

**Before** — `#` used for all comments, including section headers:
```hcl
# Get current Azure client context for subscription_id
data "azurerm_client_config" "current" {}
```

**After** — `//` used only for non-obvious inline values; section headers removed:
```hcl
shared_access_key_enabled = true // kept for break-glass; app uses managed identity
```

---

## 10. Minimal `features {}` block

The `key_vault` features block was removed (no Key Vault resources in this
project). The `resource_group.prevent_deletion_if_contains_resources` override
was also removed to keep `features {}` empty per the style guide.

---

## GitHub Actions variable updates required

After applying the rename changes, update these GitHub Actions repository variables:

| Variable | Old value | New value (prd/eus) |
|----------|-----------|---------------------|
| `CONTAINER_APP_NAME` | `azure-penny-prod` | `ca-prd-eus-penny` |
| `RESOURCE_GROUP_NAME` | `azure-penny-prod-rg` | `rg-prd-eus-penny` |
| `ACR_NAME` | `azurepennyproducracr` | `crprdeusspenny` |
| `ACR_LOGIN_SERVER` | `azurepennyproducracr.azurecr.io` | `crprdeusspenny.azurecr.io` |
