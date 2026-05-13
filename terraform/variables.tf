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

variable "tags" {
  description = "Map of tags applied to every resource."
  type        = map(string)
  default = {
    project    = "penny"
    managed_by = "terraform"
  }
}

variable "enable_cost_export" {
  description = "Whether to create the Cost Management export (requires EA/MCA/Pay-As-You-Go subscription; set false for trial/dev subs)."
  type        = bool
  default     = true
}

variable "billing_profile_id" {
  description = "Full resource ID of the MCA billing profile scope for Cost Management exports (e.g. providers/Microsoft.Billing/billingAccounts/.../billingProfiles/...)."
  type        = string
  default     = ""
}

variable "cicd_principal_object_id" {
  description = "Object ID of the service principal used by CI/CD (GitHub Actions OIDC) to push images to ACR."
  type        = string
}

variable "owner_email" {
  description = "Email address of the user granted access via Easy Auth. Invited as a guest if not already in the tenant."
  type        = string
}

variable "custom_domain" {
  description = "Optional custom hostname to bind to the Container App (e.g. penny.example.com). Requires a pre-existing CNAME pointing to the ACA environment. Leave empty to use the default *.azurecontainerapps.io hostname."
  type        = string
  default     = ""
}
