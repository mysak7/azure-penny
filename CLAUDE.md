Act as an expert Azure Cloud Architect and Python/FastAPI Developer. Name it azure-penny

I have an existing Python FastAPI application (currently tailored for AWS) that reads Cost and Usage Report (CUR) Parquet files from S3 into memory using pandas and pyarrow, and serves cost dashboards. I am building a dedicated, isolated version of this application for Azure. 

My goal is to host this app on a serverless Azure infrastructure using Azure Container Apps (since I already have a Dockerfile) and to refactor the Python code to read Azure Cost Management exports from Azure Blob Storage instead of AWS S3.

Please provide the following:

### 1. Terraform Infrastructure Code
Write production-ready Terraform code (using the `hashicorp/azurerm` provider) to provision the following serverless architecture:
- **Resource Group:** A dedicated resource group for the cost management app.
- **Storage Account & Blob Container:** A secure storage account where Azure Cost Management will export the daily Parquet files.
- **Azure Container Registry (ACR):** With Admin user disabled (Standard sku is fine) to store the Docker image.
- **User Assigned Managed Identity:** An identity that will be assigned to the Container App.
- **Role-Based Access Control (RBAC):** Assign the "Storage Blob Data Reader" role to the Managed Identity scoped to the Storage Account so the app can securely read the Parquet files without connection strings.
- **Azure Container Apps (ACA) Environment & Container App:** 
  - Provision an ACA Environment.
  - Provision the Container App itself. Configure it to scale from 0 to 1 (scale to zero) to save costs.
  - Attach the Managed Identity to the Container App.
  - Inject necessary environment variables (e.g., `STORAGE_ACCOUNT_NAME`, `STORAGE_CONTAINER_NAME`).

### 2. Python Application Refactoring
Below, I will attach my existing `main.py` and `Dockerfile`. Please rewrite the storage interaction logic:
- Remove `boto3` and AWS-specific logic.
- Implement `azure-identity` (using `DefaultAzureCredential`) and `azure-storage-blob` to authenticate and interact with Azure Blob Storage.
- Write a function that discovers the latest exported Parquet files in the configured Azure Blob container (Azure Cost Management exports have a specific folder structure, often organized by date/month).
- Read the discovered Parquet files from Blob Storage into a Pandas DataFrame using `io.BytesIO` (similar to how I do it now, but via the Azure SDK).
- Keep the FastAPI routes, caching mechanism (`_lock`, `_cache`), and logic intact, but ensure the column mapping (like `C_COST`, `C_NAME`, etc.) is adaptable or explicitly noted that it will need adjusting to Azure's specific Cost Management schema (e.g., `CostInBillingCurrency`, `MeterCategory`, `ResourceGroup`).

### 3. Dockerfile Updates
- Update the `requirements.txt` / Dockerfile dependencies to drop `boto3` and include `azure-storage-blob` and `azure-identity`.

Here is my current codebase:

~\GitHub\SEIP\seip-money
