#!/usr/bin/env bash
# One-time setup for Entra ID Easy Auth resources.
# Run with: az login (as an account with Application Administrator + Guest Inviter roles)
# Outputs the values to set as GitHub Actions variables/secrets.
set -euo pipefail

ENVIRONMENT="${1:-prd}"
LOCATION_SHORT="${2:-eus}"
OWNER_EMAIL="michal.burdik@gmail.com"
APP_NAME="penny-${ENVIRONMENT}"
APP_FQDN="ca-${ENVIRONMENT}-${LOCATION_SHORT}-penny.$(az containerapp env show \
  --name "cae-${ENVIRONMENT}-${LOCATION_SHORT}-penny" \
  --resource-group "rg-${ENVIRONMENT}-${LOCATION_SHORT}-penny" \
  --query defaultDomain -o tsv)"

echo "==> Creating app registration: ${APP_NAME}"
APP_ID=$(az ad app create \
  --display-name "${APP_NAME}" \
  --sign-in-audience AzureADMyOrg \
  --web-redirect-uris "https://${APP_FQDN}/.auth/login/aad/callback" \
  --enable-id-token-issuance true \
  --query appId -o tsv)
echo "    client_id: ${APP_ID}"

echo "==> Creating service principal"
SP_OID=$(az ad sp create --id "${APP_ID}" --query id -o tsv)
echo "    object_id: ${SP_OID}"

echo "==> Requiring app role assignment (block all other users)"
az ad sp update --id "${SP_OID}" --set appRoleAssignmentRequired=true

echo "==> Creating client secret"
SECRET_VALUE=$(az ad app credential reset \
  --id "${APP_ID}" \
  --display-name "easy-auth" \
  --years 2 \
  --query password -o tsv)

echo "==> Inviting ${OWNER_EMAIL} as B2B guest (if not already in directory)"
USER_OID=$(az ad user list --filter "mail eq '${OWNER_EMAIL}'" --query "[0].id" -o tsv 2>/dev/null || true)
if [ -z "${USER_OID}" ] || [ "${USER_OID}" = "null" ]; then
  USER_OID=$(az rest \
    --method POST \
    --uri "https://graph.microsoft.com/v1.0/invitations" \
    --headers "Content-Type=application/json" \
    --body "{
      \"invitedUserEmailAddress\": \"${OWNER_EMAIL}\",
      \"inviteRedirectUrl\": \"https://${APP_FQDN}\",
      \"sendInvitationMessage\": true
    }" \
    --query invitedUser.id -o tsv)
  echo "    invited user object_id: ${USER_OID}"
else
  echo "    user already exists, object_id: ${USER_OID}"
fi

echo "==> Granting guest user access to the app"
az rest \
  --method POST \
  --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${SP_OID}/appRoleAssignedTo" \
  --headers "Content-Type=application/json" \
  --body "{
    \"principalId\": \"${USER_OID}\",
    \"resourceId\": \"${SP_OID}\",
    \"appRoleId\": \"00000000-0000-0000-0000-000000000000\"
  }"

echo ""
echo "======================================================"
echo "Bootstrap complete. Set these in GitHub Actions:"
echo "  Variable  AUTH_APP_CLIENT_ID = ${APP_ID}"
echo "  Secret    AUTH_CLIENT_SECRET = ${SECRET_VALUE}"
echo ""
echo "Then re-run the Terraform pipeline — auth will auto-enable."
echo "======================================================"
