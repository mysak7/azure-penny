# The Cost Management export was created at MCA billing profile scope.
# Azure does not permit billing-scope RBAC assignment via REST API for
# MSA-owned MCA accounts, so the CI/CD SP cannot read this resource during
# terraform plan. Resource removed from state (destroy = false) — the live
# export in Azure is unaffected.
removed {
  from = azapi_resource.cost_export

  lifecycle {
    destroy = false
  }
}
