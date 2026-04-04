data "azurerm_subscription" "current" {}

# Daily ActualCost export → the cost-exports blob container.
# Azure Cost Management writes files under:
#   <root_folder_path>/<export-name>/<YYYYMMDD-YYYYMMDD>/<guid>/<filename>.parquet
# which matches the path pattern the application already expects.
resource "azurerm_cost_management_export_subscription" "daily" {
  name            = "${var.app_name}-daily-export"
  subscription_id = data.azurerm_subscription.current.id

  recurrence_type              = "Daily"
  recurrence_period_start_date = "2026-04-04T00:00:00Z"
  recurrence_period_end_date   = "2099-12-31T00:00:00Z"

  delivery_info {
    storage_account_id = azurerm_storage_account.main.id
    container_name     = azurerm_storage_container.cost_exports.name
    root_folder_path   = "/${var.app_name}"
  }

  query {
    type       = "ActualCost"
    time_frame = "MonthToDate"
  }
}
