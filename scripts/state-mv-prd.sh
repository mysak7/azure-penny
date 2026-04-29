#!/usr/bin/env bash
# Run from the terraform/ directory BEFORE the first terraform apply after the
# carlzxc71 style-guide refactor.  Renames Terraform state addresses so that
# the apply does not destroy and recreate every resource.
#
# Safe to run multiple times: each mv is skipped if the source address is gone.
set -euo pipefail

BACKEND_FLAGS=(
  -backend-config="resource_group_name=azure-penny-tfstate-rg"
  -backend-config="storage_account_name=azurepennytf3759"
  -backend-config="container_name=tfstate"
  -backend-config="key=azure-penny.tfstate"
  -backend-config="use_azuread_auth=true"
)

cd "$(dirname "$0")/../terraform"

terraform init "${BACKEND_FLAGS[@]}" -reconfigure > /dev/null

mv_if_exists() {
  local src="$1" dst="$2"
  if terraform state list | grep -qF "$src"; then
    echo "  mv $src → $dst"
    terraform state mv "$src" "$dst"
  else
    echo "  skip $src (not in state)"
  fi
}

echo "==> Renaming .main → .this"
mv_if_exists azurerm_resource_group.main                         azurerm_resource_group.this
mv_if_exists azurerm_storage_account.main                        azurerm_storage_account.this
mv_if_exists azurerm_storage_container.cost_exports              azurerm_storage_container.this
mv_if_exists azurerm_container_registry.main                     azurerm_container_registry.this
mv_if_exists azurerm_user_assigned_identity.main                 azurerm_user_assigned_identity.this
mv_if_exists azurerm_log_analytics_workspace.main                azurerm_log_analytics_workspace.this
mv_if_exists azurerm_container_app_environment.main              azurerm_container_app_environment.this
mv_if_exists azurerm_container_app.main                          azurerm_container_app.this

echo "==> Renaming random_string suffix"
mv_if_exists random_string.storage_suffix                        random_string.suffix

echo "==> Renaming budget resources"
mv_if_exists azurerm_monitor_action_group.budget_email           azurerm_monitor_action_group.this
mv_if_exists azurerm_consumption_budget_subscription.alert_per_10usd azurerm_consumption_budget_subscription.this

echo ""
echo "Done. Run 'terraform plan -var-file=variables/prd.tfvars' to verify."
