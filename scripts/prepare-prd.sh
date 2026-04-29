#!/usr/bin/env bash
# One-time: creates Terraform remote state storage for the prd environment.
# Values must match scripts/init-prd.sh.
set -euo pipefail

az group create \
    --name rg-prd-terraform-tfstate \
    --location eastus

az storage account create \
    --name stprdterraformtfstate \
    --resource-group rg-prd-terraform-tfstate \
    --location eastus \
    --sku Standard_LRS

az storage container create \
    --name tfstate-penny \
    --account-name stprdterraformtfstate \
    --auth-mode login
