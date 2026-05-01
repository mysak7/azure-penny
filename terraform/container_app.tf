resource "azurerm_log_analytics_workspace" "this" {
  name                = "log-${var.environment}-${var.location_short}-penny"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  sku                 = "PerGB2018"
  retention_in_days   = 31
  daily_quota_gb      = 1
  tags                = var.tags
}

resource "azurerm_container_app_environment" "this" {
  name                       = "cae-${var.environment}-${var.location_short}-penny"
  resource_group_name        = azurerm_resource_group.this.name
  location                   = azurerm_resource_group.this.location
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id
  tags                       = var.tags

  depends_on = [azurerm_resource_provider_registration.app]
}

resource "azurerm_container_app" "this" {
  name                         = "ca-${var.environment}-${var.location_short}-penny"
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = azurerm_resource_group.this.name
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }

  template {
    min_replicas = 1
    max_replicas = 1

    http_scale_rule {
      name                = "http"
      concurrent_requests = 10
    }

    container {
      name   = "penny"
      image  = var.container_image
      cpu    = 0.5
      memory = "1Gi"

      env {
        name  = "STORAGE_ACCOUNT_NAME"
        value = azurerm_storage_account.this.name
      }

      env {
        name  = "STORAGE_CONTAINER_NAME"
        value = azurerm_storage_container.this.name
      }

      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.this.client_id
      }

      env {
        name  = "AZURE_SUBSCRIPTION_ID"
        value = data.azurerm_client_config.current.subscription_id
      }

      env {
        name  = "LIVE_CACHE_TTL_SECONDS"
        value = "900"
      }

      liveness_probe {
        transport               = "HTTP"
        path                    = "/health"
        port                    = 8000
        initial_delay           = 10
        interval_seconds        = 30
        timeout                 = 5
        failure_count_threshold = 3
      }
    }
  }

  registry {
    server   = azurerm_container_registry.this.login_server
    identity = azurerm_user_assigned_identity.this.id
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    transport        = "http" # force HTTP/1.1 — avoids HTTP/2 framing issues with streaming responses

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  tags = var.tags

  depends_on = [
    azurerm_role_assignment.storage_blob_reader,
    azurerm_role_assignment.acr_pull,
  ]
}
