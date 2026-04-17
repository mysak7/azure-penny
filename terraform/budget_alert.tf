resource "azurerm_monitor_action_group" "budget_email" {
  name                = "${var.app_name}-budget-ag"
  resource_group_name = azurerm_resource_group.main.name
  short_name          = "budget-ag"
  tags                = var.tags

  email_receiver {
    name          = "michal-burdik"
    email_address = "michal.burdik@gmail.com"
  }
}

resource "azurerm_consumption_budget_subscription" "alert_per_10usd" {
  name            = "${var.app_name}-per-10usd"
  subscription_id = "/subscriptions/${data.azurerm_client_config.current.subscription_id}"

  amount     = var.budget_monthly_amount
  time_grain = "Monthly"

  time_period {
    start_date = formatdate("YYYY-MM-01'T'00:00:00'Z'", timestamp())
  }

  # Fire at every $10 increment up to the monthly budget
  dynamic "notification" {
    for_each = range(1, floor(var.budget_monthly_amount / 10) + 1)
    content {
      operator       = "GreaterThanOrEqualTo"
      threshold      = notification.value * 10
      threshold_type = "Actual"
      contact_groups = [azurerm_monitor_action_group.budget_email.id]
    }
  }
}
