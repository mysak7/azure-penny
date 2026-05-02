variable "subscription_id" {
  description = "Azure Subscription ID. Falls back to ARM_SUBSCRIPTION_ID env var if null."
  type        = string
  default     = null
}

variable "location" {
  description = "Azure region where all resources will be deployed."
  type        = string
  default     = "swedencentral"
}

variable "location_short" {
  description = "Shortened location identifier used in resource names (e.g. sc, eus)."
  type        = string
  default     = "sc"
}

variable "environment" {
  description = "Deployment environment label (dev, acc, prd)."
  type        = string
  default     = "prd"
}

variable "container_image" {
  description = "Fully-qualified container image to deploy to the Container App."
  type        = string
  default     = "nginx:latest"
}

variable "budget_monthly_amount" {
  description = "Monthly budget cap in USD."
  type        = number
  default     = 100
}

variable "tags" {
  description = "Map of tags applied to every resource."
  type        = map(string)
  default = {
    project    = "penny"
    managed_by = "terraform"
  }
}

variable "cicd_principal_object_id" {
  description = "Object ID of the service principal used by CI/CD (GitHub Actions OIDC) to push images to ACR."
  type        = string
  default     = "79b8035e-2830-4acc-b30f-03b721eae5da"
}

variable "owner_email" {
  description = "Email address of the user granted access via Easy Auth. Invited as a guest if not already in the tenant."
  type        = string
  default     = "michal.burdik@gmail.com"
}
