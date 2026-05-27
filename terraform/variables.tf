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

variable "always_on" {
  description = "Set to true to keep 1 replica running at all times (no cold start). Set to false to scale to zero when idle (saves ~$30-35/mo)."
  type        = bool
  default     = true
}

variable "custom_domain" {
  description = "Optional custom hostname to bind to the Container App (e.g. penny.example.com). Requires a pre-existing CNAME pointing to the ACA environment. Leave empty to use the default *.azurecontainerapps.io hostname."
  type        = string
  default     = ""
}

variable "vertex_proxy_url" {
  description = "Base URL of the OpenAI-compatible LLM proxy used by the AI chat widget (e.g. https://vertex.mysak.fun or http://litellm:4000)."
  type        = string
  default     = ""
}

variable "vertex_proxy_api_key" {
  description = "API key / Bearer token for the LLM proxy. Stored as a Container App secret."
  type        = string
  default     = ""
  sensitive   = true
}

variable "cf_access_client_id" {
  description = "Cloudflare Access service token Client-ID (required when vertex_proxy_url is behind Cloudflare Access)."
  type        = string
  default     = ""
}

variable "cf_access_client_secret" {
  description = "Cloudflare Access service token Client-Secret. Stored as a Container App secret."
  type        = string
  default     = ""
  sensitive   = true
}

variable "telegram_bot_token" {
  description = "Telegram Bot API token for Shield cost alerts. Stored as a Container App secret."
  type        = string
  default     = ""
  sensitive   = true
}

variable "telegram_chat_id" {
  description = "Telegram chat ID to send Shield alerts to (numeric, e.g. 1116730879)."
  type        = string
  default     = ""
}

variable "telegram_webhook_secret" {
  description = "Secret token sent by Telegram in X-Telegram-Bot-Api-Secret-Token on every webhook call. Generate with: python -c \"import secrets; print(secrets.token_hex(32))\""
  type        = string
  default     = ""
  sensitive   = true
}

variable "app_url" {
  description = "Public HTTPS URL of the Container App used to register the Telegram webhook (e.g. https://az-penny.mysak.fun). Falls back to custom_domain if set."
  type        = string
  default     = ""
}
