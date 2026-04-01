output "resource_group_name" {
  description = "Name of the resource group that contains all azure-penny resources."
  value       = azurerm_resource_group.main.name
}

output "storage_account_name" {
  description = "Name of the storage account that holds Cost Management exports."
  value       = azurerm_storage_account.main.name
}

output "acr_login_server" {
  description = "Login server hostname for the Azure Container Registry (e.g. myacr.azurecr.io)."
  value       = azurerm_container_registry.main.login_server
}

output "container_app_url" {
  description = "Public HTTPS URL of the deployed Container App."
  value       = "https://${azurerm_container_app.main.latest_revision_fqdn}"
}

output "managed_identity_client_id" {
  description = "Client ID of the user-assigned managed identity. Set as AZURE_CLIENT_ID env var so DefaultAzureCredential targets the correct identity."
  value       = azurerm_user_assigned_identity.main.client_id
}
