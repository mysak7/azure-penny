# Security Policy

## Supported Versions

This project is maintained on a best-effort basis. Security fixes are applied to the latest commit on `main`.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report vulnerabilities privately via [GitHub Security Advisories](../../security/advisories/new) or by emailing the maintainer directly (see the GitHub profile).

You can expect an acknowledgement within 48 hours and a fix or mitigation plan within 7 days for confirmed issues.

## Security Design Notes

**No secrets stored in the application.** Authentication to Azure Blob Storage and the Azure management APIs uses a [User-Assigned Managed Identity](https://learn.microsoft.com/en-us/azure/active-directory/managed-identities-azure-resources/overview) via `DefaultAzureCredential`. No connection strings, SAS tokens, or service principal secrets are stored in the codebase or environment variables.

**Broad IAM scope (Contributor).** The managed identity holds `Contributor` at the subscription scope. This is required for the Live Resources tab to list all resources and optionally delete them. If you do not need the delete functionality, you can change the role to `Reader` in `terraform/identity.tf`. The resource group hosting azure-penny itself is always in `PROTECTED_RGS`.

**Entra ID Easy Auth.** The Container App is protected by Azure AD authentication via ACA Easy Auth. Unauthenticated requests are redirected to the login page. The `penny-admin` app role gates the destructive delete endpoints.

**Terraform state.** The Entra application password (`auth-client-secret`) is stored in Terraform state. Ensure your Terraform state backend (Azure Blob Storage) has appropriate access controls and encryption at rest.
