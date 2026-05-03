#!/usr/bin/env bash
# Run from the terraform/ directory to initialise with the prd backend.
set -euo pipefail

terraform init \
    -backend-config="resource_group_name=azure-penny-tfstate-rg" \
    -backend-config="storage_account_name=azurepennytff04cd1" \
    -backend-config="container_name=tfstate" \
    -backend-config="key=azure-penny.tfstate" \
    -backend-config="use_azuread_auth=true"
