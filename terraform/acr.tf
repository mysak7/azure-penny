resource "azurerm_container_registry" "this" {
  name                = "cr${var.environment}${var.location_short}penny"
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  sku                 = "Basic"
  admin_enabled       = false

  tags = var.tags
}

resource "azurerm_role_assignment" "acr_pull" {
  name                 = "ee571f4c-2573-1f4e-47d7-84eebffd5be5"
  scope                = azurerm_container_registry.this.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.this.principal_id
}

# Allow the GitHub Actions OIDC service principal to push images
resource "azurerm_role_assignment" "acr_push_cicd" {
  name                 = "58e3ae0e-d47f-8406-3d3a-535dc487258e"
  scope                = azurerm_container_registry.this.id
  role_definition_name = "AcrPush"
  principal_id         = var.cicd_principal_object_id
}
