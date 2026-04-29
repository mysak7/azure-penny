resource "azurerm_resource_group" "this" {
  name     = "rg-${var.environment}-${var.location_short}-penny"
  location = var.location
  tags     = var.tags
}
