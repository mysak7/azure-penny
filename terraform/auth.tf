data "azuread_client_config" "current" {}

data "azuread_application_published_app_ids" "well_known" {}

data "azuread_service_principal" "msgraph" {
  client_id = data.azuread_application_published_app_ids.well_known.result.MicrosoftGraph
}

locals {
  # Constructed from variables to avoid a cycle:
  # azuread_application → container_app_fqdn → container_app → azuread_application_password → azuread_application
  container_app_fqdn = "ca-${var.environment}-${var.location_short}-penny.${azurerm_container_app_environment.this.default_domain}"
}

resource "azuread_application" "penny" {
  display_name     = "penny-${var.environment}"
  sign_in_audience = "AzureADMyOrg"
  owners           = [data.azuread_client_config.current.object_id]

  # penny-admin: grants access to resource deletion endpoints.
  # Additional users should be managed in the Azure Portal:
  #   Enterprise Applications → penny-prd → Users and groups → Add user
  # Assign "Penny Admin" for delete access, or the default role for read-only.
  app_role {
    allowed_member_types = ["User"]
    description          = "Full access including resource deletion"
    display_name         = "Penny Admin"
    enabled              = true
    id                   = "f0e1d2c3-b4a5-4968-9c8d-7e6f5a4b3c2d"
    value                = "penny-admin"
  }

  web {
    redirect_uris = compact([
      "https://${local.container_app_fqdn}/.auth/login/aad/callback",
      var.custom_domain != "" ? "https://${var.custom_domain}/.auth/login/aad/callback" : "",
    ])
    implicit_grant {
      id_token_issuance_enabled = true
    }
  }

  required_resource_access {
    resource_app_id = data.azuread_application_published_app_ids.well_known.result.MicrosoftGraph

    resource_access {
      id   = "37f7f235-527c-4136-accd-4a02d197296e" # openid
      type = "Scope"
    }
    resource_access {
      id   = "14dad69e-099b-42c9-810b-d002981feec1" # profile
      type = "Scope"
    }
    resource_access {
      id   = "64a6cdd6-aab1-4aab-94b5-c79e0cbe1ab1" # email
      type = "Scope"
    }
    resource_access {
      id   = "e1fe6dd8-ba31-4d61-89e7-88639da4683d" # User.Read
      type = "Scope"
    }
  }
}

resource "azuread_service_principal" "penny" {
  client_id                    = azuread_application.penny.client_id
  app_role_assignment_required = true
  owners                       = [data.azuread_client_config.current.object_id]
}

# Pre-grant admin consent so users never see the "Need admin approval" consent screen.
resource "azuread_service_principal_delegated_permission_grant" "penny_msgraph" {
  service_principal_object_id          = azuread_service_principal.penny.object_id
  resource_service_principal_object_id = data.azuread_service_principal.msgraph.object_id
  claim_values                         = ["openid", "profile", "email", "User.Read"]
}

resource "azuread_application_password" "penny" {
  application_id = azuread_application.penny.id
  display_name   = "easy-auth"
  end_date       = "2028-01-01T00:00:00Z"
}

resource "azuread_invitation" "owner" {
  user_email_address = var.owner_email
  redirect_url       = "https://${local.container_app_fqdn}"
}

resource "azuread_app_role_assignment" "owner" {
  app_role_id         = "f0e1d2c3-b4a5-4968-9c8d-7e6f5a4b3c2d" # penny-admin
  principal_object_id = azuread_invitation.owner.user_id
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
          enabled = false
        }
      }
    }
  }

  depends_on = [azuread_application_password.penny]
}
