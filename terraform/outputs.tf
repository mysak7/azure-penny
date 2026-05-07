output "resource_group_name" {
  description = "Name of the resource group that contains all azure-penny resources."
  value       = azurerm_resource_group.this.name
}

output "storage_account_name" {
  description = "Name of the storage account that holds Cost Management exports."
  value       = azurerm_storage_account.this.name
}

output "acr_login_server" {
  description = "Login server hostname for the Azure Container Registry."
  value       = azurerm_container_registry.this.login_server
}

output "container_app_url" {
  description = "Public HTTPS URL of the deployed Container App."
  value       = "https://${azurerm_container_app.this.latest_revision_fqdn}"
}

output "container_app_fqdn" {
  description = "Stable FQDN of the Container App ingress, used as the CNAME target for penny.mysak.fun."
  value       = azurerm_container_app.this.ingress[0].fqdn
}

output "managed_identity_client_id" {
  description = "Client ID of the user-assigned managed identity."
  value       = azurerm_user_assigned_identity.this.client_id
}
