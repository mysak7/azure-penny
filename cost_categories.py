"""
cost_categories.py — Azure service category sets for azure-penny cost grouping.

These sets map Azure MeterCategory values (from Cost Management exports) to
logical categories.  Both bare names and "Azure …" prefixed variants are listed
so the mapping works regardless of which export format Azure produces.
"""

# ---------------------------------------------------------------------------
# Category sets
# ---------------------------------------------------------------------------

CAT_COMPUTE: set[str] = {
    "Virtual Machines", "Azure Virtual Machines",
    "App Service", "Azure App Service",
    "Container Apps", "Azure Container Apps",
    "Azure Functions", "Functions",
    "Container Instances", "Azure Container Instances",
    "Azure Kubernetes Service", "Kubernetes Service",
    "Container Registry", "Azure Container Registry",
    "Batch", "Azure Batch",
    "Cloud Services",
    "Azure Spring Apps", "Spring Apps",
    "Azure Red Hat OpenShift",
}

CAT_STORAGE: set[str] = {
    "Storage", "Azure Blob Storage", "Azure Files",
    "Azure Data Lake Storage", "Azure Data Lake Storage Gen2",
    "Backup", "Azure Backup",
    "StorSimple",
    "Azure NetApp Files",
    "Managed Disks", "Azure Managed Disks",
    "Azure Queue Storage",
    "Azure Table Storage",
}

CAT_NETWORK: set[str] = {
    "Virtual Network", "Azure Virtual Network",
    "Load Balancer", "Azure Load Balancer",
    "Application Gateway", "Azure Application Gateway",
    "Azure DNS", "DNS",
    "Azure Front Door", "Front Door",
    "Bandwidth",
    "Content Delivery Network", "Azure CDN",
    "VPN Gateway", "Azure VPN Gateway",
    "Azure Bastion",
    "Azure Firewall",
    "Network Watcher", "Azure Network Watcher",
    "Traffic Manager", "Azure Traffic Manager",
    "ExpressRoute", "Azure ExpressRoute",
    "NAT Gateway", "Azure NAT Gateway",
    "Private Link", "Azure Private Link",
    "Azure DDoS Protection",
    "Azure Virtual WAN",
}

CAT_DATABASE: set[str] = {
    "SQL Database", "Azure SQL Database",
    "Azure Cosmos DB", "Cosmos DB",
    "Azure Cache for Redis", "Cache for Redis",
    "Azure Database for MySQL", "Database for MySQL",
    "Azure Database for PostgreSQL", "Database for PostgreSQL",
    "Azure SQL Managed Instance", "SQL Managed Instance",
    "Azure Synapse Analytics", "Synapse Analytics",
    "Azure Database for MariaDB",
    "Azure SQL",
}

CAT_MONITORING: set[str] = {
    "Azure Grafana Service", "Grafana",
    "Log Analytics", "Azure Log Analytics",
    "Azure Monitor",
    "Application Insights", "Azure Application Insights",
    "Microsoft Sentinel", "Azure Sentinel",
    "Azure Advisor",
    "Microsoft Defender for Cloud", "Azure Security Center",
    "Key Vault", "Azure Key Vault",
}

# Union of all explicitly mapped services — used to identify "Other" spend.
ALL_KNOWN_SERVICES: frozenset[str] = frozenset(
    CAT_COMPUTE | CAT_STORAGE | CAT_NETWORK | CAT_DATABASE | CAT_MONITORING
)

# Keywords that identify data-transfer rows within storage billing.
_TRANSFER_KEYWORDS: frozenset[str] = frozenset([
    "bandwidth", "transfer", "egress", "geo-redundant replication",
    "data retrieval", "replication",
])

# Ordered display names and their corresponding category sets.
# None sentinel → "Other" (services not in any known set).
_ORDERED_CATS: list[str] = ["Compute", "Storage", "Network", "Database", "Monitoring", "Other"]

_CAT_KEY_MAP: dict[str, set[str] | None] = {
    "compute":    CAT_COMPUTE,
    "storage":    CAT_STORAGE,
    "network":    CAT_NETWORK,
    "database":   CAT_DATABASE,
    "monitoring": CAT_MONITORING,
    "other":      None,
}
