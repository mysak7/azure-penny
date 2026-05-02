# Entra ID Easy Auth — configured via ARM API (azapi) which only needs Contributor RBAC.
# The app registration, service principal, client secret, guest invitation, and role assignment
# are managed out-of-band by scripts/bootstrap-auth.sh (requires Application.ReadWrite.OwnedBy
# and User.Invite.All Graph permissions that are not granted to the CI/CD SP).
# Set enable_easy_auth=true and supply auth_app_client_id + auth_client_secret once bootstrap is done.
resource "azapi_resource" "penny_auth" {
  count     = var.enable_easy_auth ? 1 : 0
  type      = "Microsoft.App/containerApps/authConfigs@2024-03-01"
  name      = "current"
  parent_id = azurerm_container_app.this.id

  body = {
    properties = {
      platform = {
        enabled = true
      }
      globalValidation = {
        redirectToProvider          = "azureactivedirectory"
        unauthenticatedClientAction = "RedirectToLoginPage"
      }
      identityProviders = {
        azureActiveDirectory = {
          enabled = true
          registration = {
            clientId                = var.auth_app_client_id
            clientSecretSettingName = "auth-client-secret"
            openIdIssuer            = "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}/v2.0"
          }
          validation = {
            allowedAudiences = [
              "api://${var.auth_app_client_id}",
              var.auth_app_client_id,
            ]
          }
        }
      }
      login = {
        tokenStore = {
          enabled = true
        }
      }
    }
  }
}
