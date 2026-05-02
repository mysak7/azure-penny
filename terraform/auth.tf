locals {
  # Construct the stable base FQDN without revision suffix, used for the OAuth2 redirect URI.
  # Container App FQDN = <app-name>.<cae-default-domain>
  app_fqdn = "ca-${var.environment}-${var.location_short}-penny.${azurerm_container_app_environment.this.default_domain}"
}

# App registration backing Easy Auth on the Container App
resource "azuread_application" "penny" {
  display_name     = "penny-${var.environment}"
  sign_in_audience = "AzureADMyOrg"

  web {
    redirect_uris = ["https://${local.app_fqdn}/.auth/login/aad/callback"]

    implicit_grant {
      id_token_issuance_enabled = true
    }
  }
}

resource "azuread_service_principal" "penny" {
  client_id                    = azuread_application.penny.client_id
  app_role_assignment_required = true
}

resource "azuread_application_password" "penny" {
  application_id = azuread_application.penny.id
  display_name   = "easy-auth"
}

# Invite michal.burdik@gmail.com as a B2B guest — they'll receive an email to accept.
# Prerequisites: the CI/CD SP needs User Administrator or Guest Inviter role in Entra ID.
resource "azuread_invitation" "owner" {
  user_email_address = "michal.burdik@gmail.com"
  redirect_url       = "https://${local.app_fqdn}"
}

# Grant the guest user access — all other users are blocked because app_role_assignment_required = true.
resource "azuread_app_role_assignment" "owner" {
  app_role_id         = "00000000-0000-0000-0000-000000000000"
  principal_object_id = azuread_invitation.owner.user_id
  resource_object_id  = azuread_service_principal.penny.object_id
}
