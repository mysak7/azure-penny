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

  # Remote backend — required for GitHub Actions CI/CD.
  # 1. Create a storage account + container manually (or run bootstrap/init-backend.sh).
  # 2. Run: terraform init -migrate-state
  # 3. Delete the local terraform.tfstate file after migration.
  #
  # backend "azurerm" {
  #   resource_group_name  = "azure-penny-tfstate-rg"
  #   storage_account_name = "<your-tfstate-storage-account>"
  #   container_name       = "tfstate"
  #   key                  = "azure-penny.tfstate"
  #   use_oidc             = true
  # }

  backend "local" {
    path = "terraform.tfstate"
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

# Register required resource providers
resource "azurerm_resource_provider_registration" "app" {
  name = "Microsoft.App"
}

