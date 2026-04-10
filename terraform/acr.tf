resource "azurerm_container_registry" "main" {
  name                = "${replace(var.app_name, "-", "")}${var.environment}acr"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "Standard"
  admin_enabled       = false

  tags = var.tags
}

# Allow the Container App's managed identity to pull images from ACR.
resource "azurerm_role_assignment" "acr_pull" {
  name                 = "ee571f4c-2573-1f4e-47d7-84eebffd5be5"
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.main.principal_id
}
