# Agent Architecture — azure-penny vs aws-penny

This document describes the AI chat agent built into **azure-penny** and how it differs from its sibling, **aws-penny**, which runs on AWS.

---

## Overview

Both dashboards expose an identical user-facing chat widget (`🤖` floating button) that lets users ask natural-language questions about cloud costs. Under the hood, each app runs a local **agentic loop**: it calls tools (Python functions backed by the in-process data store) to gather cost data, sends results back to the LLM, and streams the final answer to the browser as Server-Sent Events.

In **azure-penny** the agent logic lives in a dedicated module — **`ai_chat.py`** — which is shared by two callers:
- `routers/ai.py` — the web `POST /api/ai/chat` SSE endpoint (typewriter streaming to the browser)
- `telegram_bot.py` — the Telegram webhook handler (full response, then Telegram message)

Both callers invoke the same `_run_ai_chat(question, history) → str` coroutine, so model, tools, and system prompt are always identical regardless of the channel.

```
User question
    │
    ├── Web (POST /api/ai/chat)          Telegram webhook (POST /telegram/webhook)
    │          │                                    │
    └──────────┼────────────────────────────────────┘
               ▼
         ai_chat.py: _run_ai_chat()
               │
               ▼
         Agentic loop (max 5 iterations):
           1. LLM decides which tool(s) to call
           2. App executes tools → JSON result
           3. Result appended to message history
           4. LLM formulates final answer
               │
    ┌──────────┴────────────────────────────────────┐
    ▼                                                ▼
SSE stream → typewriter effect in browser    _md_to_html() → Telegram sendMessage
```

The architecture of the two applications is **nearly identical at the agent level**. All meaningful differences come from the underlying cloud platform, not from the agent design itself.

---

## Shared LLM Infrastructure

Both apps use the **same LLM proxy** — a shared [LiteLLM](https://github.com/BerriAI/litellm) container that translates OpenAI-compatible calls into Vertex AI / Gemini requests:

```
azure-penny (ACA)     aws-penny (ECS)
       │                      │
       │  HTTPS + CF-Access-* headers
       ▼                      ▼
Cloudflare Access   ──────────┘
       │
       │  Cloudflare Tunnel
       ▼
LiteLLM  (vertex.mysak.fun)
       │
       │  google-auth
       ▼
Vertex AI / Gemini
```

| Parameter              | Value                              |
|------------------------|------------------------------------|
| Model                  | `gemini-3.5-flash`                 |
| Proxy URL              | `https://vertex.mysak.fun`         |
| Auth to proxy          | Bearer token + Cloudflare Access Service Token |
| Streaming              | Non-streaming (full response, then word-by-word SSE typewriter) |
| Max agentic iterations | 5                                  |

Neither app calls Google / Vertex AI directly — all LLM traffic goes through the shared proxy. This means the same model, the same billing, and the same proxy outage affects both dashboards.

---

## Agent Tools

azure-penny exposes **7 tools** in OpenAI function-calling format. Six are common with aws-penny; one differs per cloud-provider concept; one is azure-penny-only:

| Tool                      | azure-penny         | aws-penny          | Purpose |
|---------------------------|---------------------|--------------------|---------|
| `get_cost_summary`        | ✅                  | ✅                 | Total spend + top services for a period |
| `get_anomalies`           | ✅                  | ✅                 | Week-over-week spikes, new/disappeared services, untagged spend |
| `get_breakdown`           | ✅                  | ✅                 | Time-series buckets (days/weeks/months) |
| `get_live_resources`      | ✅                  | ✅                 | Real-time inventory with monthly cost estimates |
| `get_daily_costs`         | ✅                  | ✅                 | Daily cost time series for the past N days |
| `get_page_snapshot`       | ✅                  | —                  | All-in-one snapshot: totals, categories, top RGs, anomalies in one call |
| **`get_resource_groups`** | ✅ (Azure-specific) | —                  | Cost by Azure Resource Group and subscription |
| **`get_accounts`**        | —                   | ✅ (AWS-specific)  | Cost by AWS account ID and project/app tag |

**`get_page_snapshot`** is a convenience super-tool: the LLM calls it when the user asks a broad question ("what's going on with my costs?"). It returns the same data as several other tools combined — total spend, breakdown by category, top services, top resource groups, application-tag breakdown, and week-over-week anomalies — in one round-trip, reducing the number of agentic iterations.

The `get_resource_groups` / `get_accounts` substitution mirrors the cloud provider's organisational model:
- **Azure** organises spend across _Resource Groups_ and _Subscriptions_.
- **AWS** organises spend across _accounts_ (billing entities) and uses tags (`user:App`, `user:Project`) for logical grouping.

All tools are **read-only** — no tool can modify or delete any cloud resource.

---

## System Prompt

The two prompts are nearly identical, with cloud-specific substitutions:

**azure-penny:**
> *"You are azure-penny, an AI assistant specialized in analyzing Microsoft Azure cloud costs. You have access to tools that provide real-time data from Azure Cost Management exports…"*

**aws-penny:**
> *"You are aws-penny, an AI assistant specialized in analyzing AWS cloud costs. You have access to tools that provide real-time data from AWS CUR (Cost and Usage Report) exports…"*

Both prompts share the same behavioral constraints:
- Always call tools before answering (no hallucinated cost figures)
- Respond in the language of the user's question (Czech ↔ English)
- Never suggest resource deletion or infrastructure changes
- Format currency in USD to 2 decimal places

---

## Cloud Platform Differences

Everything outside the agent loop diverges significantly.

### Hosting

| Dimension            | azure-penny                        | aws-penny                        |
|----------------------|------------------------------------|----------------------------------|
| Compute              | Azure Container Apps               | AWS ECS Fargate                  |
| Scaling              | Scale-to-zero (min 0 replicas)     | Always-on (min 1 task)           |
| Cold start           | Yes — first request after idle wakes the container | None (always running) |
| Container registry   | Azure Container Registry (ACR)     | Amazon ECR                       |
| CPU / Memory         | 0.5 vCPU / 1 GiB                   | 0.5 vCPU / 1 GB                  |

**Scale-to-zero impact:** azure-penny pays nothing during extended idle periods. aws-penny incurs a small but constant Fargate baseline cost even when unused. For a cost dashboard that is typically opened once or twice a day this is a non-trivial difference.

### Authentication

| Dimension            | azure-penny                        | aws-penny                        |
|----------------------|------------------------------------|----------------------------------|
| Runtime identity     | User-Assigned Managed Identity     | IAM Task Role (attached to ECS task definition) |
| Credential resolution | `DefaultAzureCredential` → IMDS   | boto3 default chain → EC2 IMDS   |
| Local dev            | Azure CLI session / `az login`     | AWS CLI session / `aws sso login` |
| CI/CD identity       | GitHub Actions OIDC → Azure SP     | GitHub Actions OIDC → AWS role   |
| Secrets stored anywhere | None                            | None                             |

Both approaches are secretless at runtime — credentials are issued dynamically by the cloud platform's metadata service.

### Cost Data Source

| Dimension            | azure-penny                            | aws-penny                              |
|----------------------|----------------------------------------|----------------------------------------|
| Export mechanism     | Azure Cost Management daily export     | AWS Billing → CUR 2.0 daily export     |
| Storage              | Azure Blob Storage                     | Amazon S3                              |
| File format          | **CSV** (Parquet not supported at subscription scope by the provider) | **Parquet** (Snappy) |
| File discovery       | Auto-discover latest `YYYYMMDD-YYYYMMDD/` folder; pick **only** the most-recently-modified file (exports are cumulative — reading multiple would double-count) | Auto-discover latest folder; reads all `.parquet` files (append-only per period) |
| Column normalisation | Azure CSV columns → `C_*` names; agreement-type aware (EA, MCA, CSP) | CUR Parquet columns → internal `C_*` names |
| First data delivery  | Next day                               | Next day after export creation         |
| Auto-discovery       | Env vars; no auto-discovery needed (Managed Identity has direct access) | `sts:GetCallerIdentity` → derives bucket name `{env}-seip-cur-{account-id}` |

### Live Resources

Both tabs make parallel SDK calls at startup and cache for 15 minutes, but the breadth differs:

**azure-penny** covers all resource types via `azure-mgmt-resource` + `azure-mgmt-compute`, limited to what the Managed Identity's `Contributor` role can see across the subscription. Includes Retail Prices API for spot-price hints and Azure Monitor for CPU/network metrics.

**aws-penny** covers 14 explicit resource types via boto3:
EC2, ECS, ECR, RDS, S3, Lambda, DynamoDB, ELBv2, Auto Scaling, ElastiCache, CloudFront, Secrets Manager, KMS, CloudWatch Logs — plus spot-price saving hints from EC2 Pricing API.

### Access Control

| Layer                | azure-penny                            | aws-penny                              |
|----------------------|----------------------------------------|----------------------------------------|
| Front door           | ACA Easy Auth (Entra ID) — all routes require login; role `penny-admin` gates destructive endpoints | Cloudflare Access (Entra ID login) — gates the ALB |
| Network path         | Internet → ACA HTTP ingress → container | Internet → ALB → CF Access → ECS task |

### Infrastructure as Code

| Resource             | azure-penny (Terraform `hashicorp/azurerm`) | aws-penny (Terraform `hashicorp/aws`) |
|----------------------|----------------------------------------------|---------------------------------------|
| Compute              | `azurerm_container_app`                      | `aws_ecs_task_definition`, `aws_ecs_service` |
| Registry             | `azurerm_container_registry`                 | `aws_ecr_repository`                  |
| Identity             | `azurerm_user_assigned_identity`             | `aws_iam_role` (task + execution)     |
| Ingress              | ACA built-in HTTPS ingress                   | `aws_alb`, listeners, target group    |
| Cost export          | `azurerm_subscription_cost_management_export` (removed from Terraform state — MCA billing scope RBAC cannot be managed via API) | CUR created manually in Billing Console |
| State backend        | Azure Blob Storage (`tfstate` container)     | (default local)                       |

### CI/CD

| Dimension            | azure-penny                            | aws-penny                              |
|----------------------|----------------------------------------|----------------------------------------|
| Workflow layout      | Separate `ci.yml` (PRs) and `cd.yml` (push to main) | Single `deploy.yml` with 3 jobs |
| Parallel jobs        | `lint-python` ‖ `terraform-validate` ‖ `docker-build` (CI only) | `quality-checks` ‖ `build-push` → `deploy` |
| Security scanning    | No Trivy scan                          | Trivy CVE scan (CRITICAL blocks deploy, results in GitHub Security tab) |
| Image tags           | `:latest` + `:<git-sha>`               | `:latest` + `:<git-sha>`, provenance attestation, SBOM |
| Deploy mechanism     | `az containerapp update --image <new-tag>` | `amazon-ecs-render-task-definition` + `amazon-ecs-deploy-task-definition` |
| OIDC                 | GitHub → Azure SP (`AZURE_CLIENT_ID` / `TENANT_ID`) | GitHub → AWS (assume `role-prd-euc1-penny-gha`) |

---

## Data Flow Comparison

### azure-penny

```
Azure Cost Management
  └─ daily CSV export (cumulative within billing period)
       └─ Blob Storage (cost-exports container)
            └─ storage.py: azure-storage-blob SDK
                 └─ _discover_latest_blobs() → single file (latest mtime)
                      └─ _apply_column_map() → C_* normalisation (EA/MCA/CSP-aware)
                           └─ pandas DataFrame
                                └─ in-memory cache (TTL 1h, asyncio.Lock)
                                     └─ FastAPI routes
                                          └─ Azure Container App (Managed Identity)
                                               └─ ACA HTTPS ingress → penny.mysak.fun
```

### aws-penny

```
AWS Billing Console
  └─ CUR 2.0 export (daily, Parquet, Snappy)
       └─ S3 Bucket (dev-seip-cur-<account-id>)
            └─ storage.py: boto3 s3.get_object
                 └─ pyarrow → pandas DataFrame
                      └─ in-memory cache (TTL 1h, asyncio.Lock)
                           └─ FastAPI routes (cost API, live resources, AI chat)
                                └─ ECS Fargate Task (IAM Task Role)
                                     └─ ALB → Cloudflare Access → aws-penny.mysak.fun
```

---

## Why Two Separate Applications?

The split exists because the two cost systems are **fundamentally different** data sources:

- Azure Cost Management and AWS CUR exports have different schemas, column semantics, file formats, and discovery logic.
- Azure resource inventory requires `azure-mgmt-*` SDKs; AWS requires boto3.
- Organisational concepts differ: Azure Resource Groups & subscriptions vs AWS accounts & tags.
- Authentication mechanisms are cloud-native and non-interchangeable (Managed Identity vs IAM Task Role).

A single unified app would require conditional code paths for every data access operation, making both harder to reason about and test. The shared agent loop pattern (same tool API shape, same LLM proxy, same SSE streaming contract) means enhancements to the agent behaviour can be ported between the two apps with minimal diff.
