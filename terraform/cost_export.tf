# azurerm_subscription_cost_management_export uses an API version not supported for
# MCA/PAYG subscriptions. Using azapi_resource at the billing profile scope instead.
resource "azapi_resource" "cost_export" {
  count     = var.enable_cost_export ? 1 : 0
  type      = "Microsoft.CostManagement/exports@2023-11-01"
  name      = "penny-${var.environment}-daily-export"
  parent_id = var.billing_profile_id

  body = {
    properties = {
      schedule = {
        status    = "Active"
        recurrence = "Daily"
        recurrencePeriod = {
          from = "2026-05-04T00:00:00Z"
          to   = "2099-12-31T00:00:00Z"
        }
      }
      format = "Csv"
      deliveryInfo = {
        destination = {
          resourceId  = azurerm_storage_account.this.id
          container   = azurerm_storage_container.this.name
          rootFolderPath = "penny"
        }
      }
      definition = {
        type      = "ActualCost"
        timeframe = "MonthToDate"
        dataSet = {
          granularity = "Daily"
        }
      }
    }
  }
}
