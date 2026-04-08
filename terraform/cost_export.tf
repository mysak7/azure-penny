data "azurerm_subscription" "current" {}
# Storage account/container are defined in storage.tf

# Daily ActualCost export → the cost-exports blob container.
# Azure Cost Management writes files under:
#   <root_folder_path>/<export-name>/<YYYYMMDD-YYYYMMDD>/<guid>/<filename>.parquet
# which matches the path pattern the application already expects.
resource "azurerm_subscription_cost_management_export" "daily" {
  name            = "${var.app_name}-daily-export"
  subscription_id = data.azurerm_subscription.current.id

  recurrence_type              = "Daily"
  recurrence_period_start_date = "2026-04-08T00:00:00Z"
  recurrence_period_end_date   = "2099-12-31T00:00:00Z"

  file_format = "Csv"

  export_data_storage_location {
    container_id     = "${azurerm_storage_account.main.id}/blobServices/default/containers/${azurerm_storage_container.cost_exports.name}"
    root_folder_path = var.app_name
  }

  export_data_options {
    type       = "ActualCost"
    time_frame = "MonthToDate"
  }
}
