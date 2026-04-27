resource "azurerm_user_assigned_identity" "main" {
  name                = "${var.app_name}-${var.environment}-identity"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = var.tags
}

# Grant the managed identity read access to blobs in the storage account.
# Scoped to the storage account so it can read from any container (including
# cost-exports). Narrow to the container scope if stricter isolation is needed.
resource "azurerm_role_assignment" "storage_blob_reader" {
  scope                = azurerm_storage_account.main.id
  role_definition_name = "Storage Blob Data Reader"
  principal_id         = azurerm_user_assigned_identity.main.principal_id
}

# Allow azure-penny to delete resources across the subscription.
# Scoped to the subscription so it can destroy resources in any RG.
resource "azurerm_role_assignment" "subscription_contributor" {
  scope                = "/subscriptions/${data.azurerm_client_config.current.subscription_id}"
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.main.principal_id

}
