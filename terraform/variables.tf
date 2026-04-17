variable "subscription_id" {
  description = "Azure Subscription ID. If null, falls back to the ARM_SUBSCRIPTION_ID environment variable (required by azurerm 4.x)."
  type        = string
  default     = null
}

variable "location" {
  description = "Azure region where all resources will be deployed."
  type        = string
  default     = "eastus"
}

variable "app_name" {
  description = "Base name used to derive all resource names."
  type        = string
  default     = "azure-penny"
}

variable "environment" {
  description = "Deployment environment label (e.g. prod, staging, dev)."
  type        = string
  default     = "prod"
}

variable "container_image" {
  description = "Fully-qualified container image to deploy to the Container App (e.g. myacr.azurecr.io/azure-penny:latest)."
  type        = string
  default     = "nginx:latest"
}

variable "budget_monthly_amount" {
  description = "Monthly budget cap in USD. Email alerts fire at every $10 increment up to this amount."
  type        = number
  default     = 100
}

variable "tags" {
  description = "Map of tags applied to every resource in this deployment."
  type        = map(string)
  default = {
    project     = "azure-penny"
    managed_by  = "terraform"
  }
}
