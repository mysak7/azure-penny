data "azuread_client_config" "current" {}

locals {
  # Constructed from variables to avoid a cycle:
  # azuread_application → container_app_fqdn → container_app → azuread_application_password → azuread_application
  container_app_fqdn = "ca-${var.environment}-${var.location_short}-penny.${azurerm_container_app_environment.this.default_domain}"
}

resource "azuread_application" "penny" {
  display_name     = "penny-${var.environment}"
  sign_in_audience = "AzureADMyOrg"
  owners           = [data.azuread_client_config.current.object_id]

  web {
    redirect_uris = [
      "https://${local.container_app_fqdn}/.auth/login/aad/callback"
    ]
    implicit_grant {
      id_token_issuance_enabled = true
    }
  }
}

resource "azuread_service_principal" "penny" {
  client_id                    = azuread_application.penny.client_id
  app_role_assignment_required = true
  owners                       = [data.azuread_client_config.current.object_id]
}

resource "azuread_application_password" "penny" {
  application_id = azuread_application.penny.id
  display_name   = "easy-auth"
  end_date       = "2028-01-01T00:00:00Z"
}

# Invite the owner as a guest if they are not already in the tenant.
# If the user already exists the provider imports them without sending a new invite.
resource "azuread_invitation" "owner" {
  user_email_address = var.owner_email
  redirect_url       = "https://${local.container_app_fqdn}"

  message {
    language = "en-US"
  }
}

resource "azuread_app_role_assignment" "owner" {
  app_role_id         = "00000000-0000-0000-0000-000000000000"
  principal_object_id = azuread_invitation.owner.user_object_id
  resource_object_id  = azuread_service_principal.penny.object_id
}

resource "azapi_resource" "penny_auth" {
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
            clientId                = azuread_application.penny.client_id
            clientSecretSettingName = "auth-client-secret"
            openIdIssuer            = "https://login.microsoftonline.com/${data.azurerm_client_config.current.tenant_id}/v2.0"
          }
          validation = {
            allowedAudiences = [
              "api://${azuread_application.penny.client_id}",
              azuread_application.penny.client_id,
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

  depends_on = [azuread_application_password.penny]
}
