#!/usr/bin/env bash
# One-time: creates Terraform remote state storage for the prd environment.
# Values must match scripts/init-prd.sh and the backend-config in terraform.yml.
set -euo pipefail

TFSTATE_RG="azure-penny-tfstate-rg"
TFSTATE_SA="azurepennytff04cd1"
TFSTATE_CONTAINER="tfstate"
LOCATION="eastus"

az group create \
    --name "$TFSTATE_RG" \
    --location "$LOCATION" \
    --output none

az storage account create \
    --name "$TFSTATE_SA" \
    --resource-group "$TFSTATE_RG" \
    --location "$LOCATION" \
    --sku Standard_LRS \
    --kind StorageV2 \
    --min-tls-version TLS1_2 \
    --allow-blob-public-access false \
    --output none

az storage container create \
    --name "$TFSTATE_CONTAINER" \
    --account-name "$TFSTATE_SA" \
    --auth-mode login \
    --output none

echo "Done. State backend: ${TFSTATE_SA}/${TFSTATE_CONTAINER}"
