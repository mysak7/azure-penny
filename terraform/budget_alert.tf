resource "azurerm_monitor_action_group" "this" {
  name                = "ag-${var.environment}-${var.location_short}-budget"
  resource_group_name = azurerm_resource_group.this.name
  short_name          = "budget-ag"
  tags                = var.tags

  email_receiver {
    name          = "michal-burdik"
    email_address = "michal.burdik@gmail.com"
  }
}

resource "azurerm_consumption_budget_subscription" "this" {
  name            = "bgt-${var.environment}-${var.location_short}-penny"
  subscription_id = "/subscriptions/${data.azurerm_client_config.current.subscription_id}"

  amount     = var.budget_monthly_amount
  time_grain = "Monthly"

  time_period {
    start_date = "2026-04-01T00:00:00Z"
  }

  dynamic "notification" {
    for_each = [20, 40, 60, 80, 100]
    content {
      operator       = "GreaterThanOrEqualTo"
      threshold      = notification.value
      threshold_type = "Actual"
      contact_groups = [azurerm_monitor_action_group.this.id]
    }
  }
}
