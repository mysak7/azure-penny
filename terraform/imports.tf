import {
  to = azurerm_resource_group.this
  id = "/subscriptions/9b7fe8b8-ffba-451a-a77f-46d8552f3922/resourceGroups/rg-prd-eus-penny"
}

import {
  to = azurerm_user_assigned_identity.this
  id = "/subscriptions/9b7fe8b8-ffba-451a-a77f-46d8552f3922/resourceGroups/rg-prd-eus-penny/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-prd-eus-penny"
}

import {
  to = azurerm_storage_account.this
  id = "/subscriptions/9b7fe8b8-ffba-451a-a77f-46d8552f3922/resourceGroups/rg-prd-eus-penny/providers/Microsoft.Storage/storageAccounts/stprdeus8wvzzi"
}

import {
  to = azurerm_storage_container.this
  id = "https://stprdeus8wvzzi.blob.core.windows.net/cost-exports"
}

import {
  to = azurerm_container_registry.this
  id = "/subscriptions/9b7fe8b8-ffba-451a-a77f-46d8552f3922/resourceGroups/rg-prd-eus-penny/providers/Microsoft.ContainerRegistry/registries/crprdeuspenny"
}

import {
  to = azurerm_log_analytics_workspace.this
  id = "/subscriptions/9b7fe8b8-ffba-451a-a77f-46d8552f3922/resourceGroups/rg-prd-eus-penny/providers/Microsoft.OperationalInsights/workspaces/log-prd-eus-penny"
}

import {
  to = azurerm_container_app_environment.this
  id = "/subscriptions/9b7fe8b8-ffba-451a-a77f-46d8552f3922/resourceGroups/rg-prd-eus-penny/providers/Microsoft.App/managedEnvironments/cae-prd-eus-penny"
}

import {
  to = azurerm_container_app.this
  id = "/subscriptions/9b7fe8b8-ffba-451a-a77f-46d8552f3922/resourceGroups/rg-prd-eus-penny/providers/Microsoft.App/containerApps/ca-prd-eus-penny"
}

import {
  to = azurerm_role_assignment.acr_pull
  id = "/subscriptions/9b7fe8b8-ffba-451a-a77f-46d8552f3922/resourceGroups/rg-prd-eus-penny/providers/Microsoft.ContainerRegistry/registries/crprdeuspenny/providers/Microsoft.Authorization/roleAssignments/ee571f4c-2573-1f4e-47d7-84eebffd5be5"
}

import {
  to = azurerm_role_assignment.subscription_contributor
  id = "/subscriptions/9b7fe8b8-ffba-451a-a77f-46d8552f3922/providers/Microsoft.Authorization/roleAssignments/868e0917-9b91-2748-7a5e-d4e977216487"
}

import {
  to = azurerm_role_assignment.storage_blob_reader
  id = "/subscriptions/9b7fe8b8-ffba-451a-a77f-46d8552f3922/resourceGroups/rg-prd-eus-penny/providers/Microsoft.Storage/storageAccounts/stprdeus8wvzzi/providers/Microsoft.Authorization/roleAssignments/d53ab878-c87c-3c55-768e-dcf9ecf4604e"
}

import {
  to = azurerm_role_assignment.acr_push_cicd
  id = "/subscriptions/9b7fe8b8-ffba-451a-a77f-46d8552f3922/resourceGroups/rg-prd-eus-penny/providers/Microsoft.ContainerRegistry/registries/crprdeuspenny/providers/Microsoft.Authorization/roleAssignments/58e3ae0e-d47f-8406-3d3a-535dc487258e"
}
