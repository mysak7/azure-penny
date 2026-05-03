#!/usr/bin/env bash
# One-time bootstrap: creates the Terraform remote state backend and grants
# the GitHub Actions service principal access to read/write state.
#
# Run once from a terminal with Owner/Contributor rights before the first CI run.
# Values must match terraform/main.tf backend block.

set -euo pipefail

TFSTATE_RG="azure-penny-tfstate-rg"
TFSTATE_SA="azurepennytff04cd1"
TFSTATE_CONTAINER="tfstate"
LOCATION="eastus"

# GitHub Actions SP — object ID of azure-penny-github-actions
GH_SP_OBJECT_ID="5bcc93c5-b6e3-4a5c-91cb-3defb9254151"

echo "==> Creating resource group $TFSTATE_RG"
az group create --name "$TFSTATE_RG" --location "$LOCATION" --output none

echo "==> Creating storage account $TFSTATE_SA"
az storage account create \
  --name "$TFSTATE_SA" \
  --resource-group "$TFSTATE_RG" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --kind StorageV2 \
  --min-tls-version TLS1_2 \
  --allow-blob-public-access false \
  --output none

echo "==> Creating blob container $TFSTATE_CONTAINER"
az storage container create \
  --name "$TFSTATE_CONTAINER" \
  --account-name "$TFSTATE_SA" \
  --auth-mode login \
  --output none

SA_ID=$(az storage account show \
  --name "$TFSTATE_SA" \
  --resource-group "$TFSTATE_RG" \
  --query id -o tsv)

echo "==> Granting Storage Blob Data Contributor to GitHub Actions SP on $TFSTATE_SA"
az role assignment create \
  --assignee-object-id "$GH_SP_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" \
  --scope "$SA_ID" \
  --output none

echo ""
echo "Done. Terraform backend is ready for CI."
echo "Storage account: $TFSTATE_SA"
echo "Container: $TFSTATE_CONTAINER"
