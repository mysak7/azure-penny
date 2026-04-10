terraform {
  required_version = ">= 1.3.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.67"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.5"
    }
  }

  backend "azurerm" {
    resource_group_name  = "azure-penny-tfstate-rg"
    storage_account_name = "azurepennytf3759"
    container_name       = "tfstate"
    key                  = "azure-penny.tfstate"
  }
}

provider "azurerm" {
  subscription_id = var.subscription_id

  features {
    resource_group {
      prevent_deletion_if_contains_resources = false
    }
    key_vault {
      purge_soft_delete_on_destroy    = true
      recover_soft_deleted_key_vaults = true
    }
  }
}

# Get current Azure client context for subscription_id
data "azurerm_client_config" "current" {}

# Register required resource providers
resource "azurerm_resource_provider_registration" "app" {
  name = "Microsoft.App"
}

