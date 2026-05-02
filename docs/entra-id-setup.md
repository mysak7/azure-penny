# Entra ID Setup

How authentication and identity work in azure-penny, and how to reproduce the setup from scratch.

---

## Architecture overview

Three separate Entra ID / identity concerns exist in this project:

| Concern | What it is | Managed by |
|---|---|---|
| **GitHub Actions service principal** | OIDC identity for CI/CD; pushes images to ACR, runs Terraform | Manual bootstrap (Federated Credential in Azure Portal) |
| **User-assigned managed identity** | Runtime identity for the Container App; reads blobs and pulls from ACR | Terraform (`identity.tf`, `acr.tf`) |
| **App registration (Easy Auth)** | Entra ID app that gates access to the Container App UI | Terraform (`auth.tf`) |

---

## 1. GitHub Actions service principal (CI/CD)

This is the identity Terraform runs as in GitHub Actions. It uses **OIDC federated credentials** — no client secret.

### Required Azure RBAC roles (subscription scope)
- `Contributor` — provision/modify all ARM resources
- `User Access Administrator` — needed to create role assignments in Terraform (e.g. assigning AcrPull to the managed identity)

### Required Microsoft Graph application permissions
- `DelegatedPermissionGrant.ReadWrite.All` — needed for Terraform to manage `azuread_service_principal_delegated_permission_grant` (the admin consent pre-grant for Easy Auth)

> **This Graph permission is a bootstrap step** — it cannot be granted by Terraform itself since Terraform is the principal being granted the permission. It must be done once manually by a Global Administrator or Privileged Role Administrator.

### Bootstrap: grant Graph permission to the CICD service principal

```bash
# Look up the CICD service principal by name (or use its known object ID)
SP_OID=$(az ad sp show --display-name "azure-penny-github-actions" --query id -o tsv)

# Look up the Microsoft Graph service principal in this tenant
MSGRAPH_OID=$(az ad sp show --id 00000003-0000-0000-c000-000000000000 --query id -o tsv)

# Assign DelegatedPermissionGrant.ReadWrite.All (app role, not delegated)
az rest --method POST \
  --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_OID/appRoleAssignments" \
  --headers "Content-Type=application/json" \
  --body "{
    \"principalId\": \"$SP_OID\",
    \"resourceId\": \"$MSGRAPH_OID\",
    \"appRoleId\": \"8e8e4742-1d95-4f68-9d56-6ee75648c72a\"
  }"
```

Note: `8e8e4742-1d95-4f68-9d56-6ee75648c72a` is the stable, well-known app role ID for `DelegatedPermissionGrant.ReadWrite.All` in Microsoft Graph. Verify with:
```bash
az ad sp show --id 00000003-0000-0000-c000-000000000000 \
  --query "appRoles[?value=='DelegatedPermissionGrant.ReadWrite.All'].id" -o tsv
```

### Bootstrap: create the service principal and federated credential

```bash
# Create the app registration
APP_ID=$(az ad app create --display-name "azure-penny-github-actions" --query appId -o tsv)

# Create the service principal
az ad sp create --id "$APP_ID"
SP_OID=$(az ad sp show --id "$APP_ID" --query id -o tsv)

# Grant Azure RBAC roles
SUB_ID=$(az account show --query id -o tsv)
az role assignment create --assignee "$SP_OID" --role Contributor --scope "/subscriptions/$SUB_ID"
az role assignment create --assignee "$SP_OID" --role "User Access Administrator" --scope "/subscriptions/$SUB_ID"

# Add federated credential for GitHub Actions (adjust org/repo)
az ad app federated-credential create --id "$APP_ID" --parameters '{
  "name": "github-actions-main",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:mysak7/azure-penny:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

Then set these GitHub repository variables:
- `AZURE_CLIENT_ID` → app registration client ID (`$APP_ID`)
- `AZURE_TENANT_ID` → `az account show --query tenantId -o tsv`
- `AZURE_SUBSCRIPTION_ID` → `$SUB_ID`

And update `cicd_principal_object_id` in `terraform/variables.tf` to `$SP_OID`.

---

## 2. User-assigned managed identity (Container App runtime)

Terraform creates `id-{env}-{location_short}-penny` and assigns it two roles:

| Role | Scope | Purpose |
|---|---|---|
| `Storage Blob Data Reader` | Storage account | Read cost export Parquet files |
| `Contributor` | Subscription | Live Azure cost API calls via `azure.mgmt.*` |
| `AcrPull` | Container Registry | Pull the app image at startup |

The Container App injects the identity's `client_id` as `AZURE_CLIENT_ID`, which `DefaultAzureCredential` picks up automatically — no secrets needed in the app code.

---

## 3. App registration — Easy Auth (Container App UI access)

Terraform manages an Entra ID app registration (`penny-{env}`) that is wired into Container Apps Easy Auth. The app is configured in `auth.tf` and the auth config is applied via the `azapi` provider (`Microsoft.App/containerApps/authConfigs`).

### What Terraform creates

| Resource | Purpose |
|---|---|
| `azuread_application.penny` | App registration with `openid`, `profile`, `email`, `User.Read` scopes |
| `azuread_service_principal.penny` | Service principal for the app; `app_role_assignment_required = true` restricts who can sign in |
| `azuread_service_principal_delegated_permission_grant` | Pre-grants admin consent so users never see the "needs admin approval" screen |
| `azuread_app_role_assignment.owner` | Grants the `owner_email` user access (default role `00000000-…`) |
| `azuread_application_password.penny` | Client secret injected into the Container App as `auth-client-secret` |
| `azapi_resource.penny_auth` | Enables Easy Auth on the Container App pointing at the app registration |

### Access control

Because `app_role_assignment_required = true`, only users explicitly granted a role assignment can sign in. Add more users by extending `auth.tf` with additional `azuread_app_role_assignment` resources. The default role (`00000000-0000-0000-0000-000000000000`) is sufficient — no custom app roles are needed.

### Why `use_oidc = true` in the backend / `ARM_USE_AZUREAD = true` in CI

The Terraform `azurerm` backend is configured with `use_azuread_auth = true` so it authenticates to the blob storage state backend via the OIDC token, not a storage account key. `ARM_USE_OIDC=true` and `ARM_USE_AZUREAD=true` are set as env vars in the workflow.

---

## Full bootstrap checklist (new environment)

These steps are required before `terraform apply` will succeed end-to-end.

```
[ ] 1. Create the GitHub Actions service principal and federated credential (see §1 above)
[ ] 2. Grant it Contributor + User Access Administrator on the subscription
[ ] 3. Grant it DelegatedPermissionGrant.ReadWrite.All on Microsoft Graph (see §1 above)
[ ] 4. Create the Terraform state storage account and blob container (run scripts/setup-backend.sh)
[ ] 5. Set GitHub repo variables: AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID, ACR_LOGIN_SERVER
[ ] 6. Update cicd_principal_object_id in terraform/variables.tf
[ ] 7. Update owner_email in terraform/variables.tf (or pass via tfvars)
[ ] 8. Run terraform apply — all remaining Entra ID resources are created automatically
```

---

## Troubleshooting

### `403 Insufficient privileges` on `azuread_service_principal_delegated_permission_grant`

The CICD service principal is missing the `DelegatedPermissionGrant.ReadWrite.All` Graph permission. Follow the bootstrap step in §1 — it must be done by a Global Admin or Privileged Role Admin.

This error recurs whenever the grant resource is destroyed and recreated (e.g. if the underlying service principal is replaced). The permission grant on the CICD principal itself is permanent and only needs to be done once per service principal.

### `admin-consent` CLI command returns `Consent validation failed`

The `az ad app permission admin-consent` command is unreliable for application permissions (Role type). Use the `az rest` Graph API call shown in §1 instead.

### Users see "Need admin approval" when signing in

The `azuread_service_principal_delegated_permission_grant` resource was not applied or was deleted. Re-run `terraform apply`.

### User cannot sign in (AADSTS50105 / not assigned)

The user does not have an app role assignment. Add an `azuread_app_role_assignment` block in `auth.tf` for that user's object ID, or look up their object ID with:
```bash
az ad user show --id user@example.com --query id -o tsv
```
