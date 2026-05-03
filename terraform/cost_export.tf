resource "azurerm_subscription_cost_management_export" "daily" {
  count           = var.enable_cost_export ? 1 : 0
  name            = "penny-${var.environment}-daily-export"
  subscription_id = data.azurerm_subscription.current.id

  recurrence_type              = "Daily"
  recurrence_period_start_date = "2026-05-01T00:00:00Z"
  recurrence_period_end_date   = "2099-12-31T00:00:00Z"

  file_format = "Csv"

  export_data_storage_location {
    container_id     = "${azurerm_storage_account.this.id}/blobServices/default/containers/${azurerm_storage_container.this.name}"
    root_folder_path = "penny"
  }

  export_data_options {
    type       = "ActualCost"
    time_frame = "MonthToDate"
  }
}
