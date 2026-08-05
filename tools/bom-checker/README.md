# Azure Region BOM Checker

> **Disclaimer:** This tool was created with the assistance of GitHub Copilot as a personal project. It is not an official Microsoft tool and is not endorsed or supported by Microsoft. Review all code before running it in your environment.

A Python tool that checks which Azure regions support all the services in your Bill of Materials (BOM). It queries the Azure Resource Manager API live at runtime — no cached data.

## How it works

1. You fill in `bom_template.xlsx` with the Azure services your deployment requires
2. The script checks each specified region against the Azure provider APIs
3. Results are written to a color-coded Excel file — green (supported) or red (unsupported)

## Prerequisites

- Python 3.9+
- Azure CLI installed and authenticated (`az login`)
- openpyxl:
  ```
  pip install -r requirements.txt
  ```

## Usage

```bash
# Check specific regions (comma-separated on the command line)
python check_azure_regions.py --regions eastus,westeurope

# Specify a custom BOM file and output path
python check_azure_regions.py --bom bom_template.xlsx --regions eastus,mexicocentral --output results.xlsx

# Verbose mode (shows raw az CLI commands)
python check_azure_regions.py --regions eastus --verbose

# Maintain your regions list in a text file (see regions.txt for the template)
python check_azure_regions.py --regions-file regions.txt

# Combine a file with extra ad-hoc regions (deduped, file order first)
python check_azure_regions.py --regions-file regions.txt --regions eastus,westus3

# Check every Azure region your account can see (slow — dozens of API calls per region)
python check_azure_regions.py --regions all
```

### Regions file format (`--regions-file`)

`regions.txt` ships as a working sample. The format is:

- One Azure region name per line (e.g. `eastus`)
- Lines starting with `#` are comments; inline `#` strips the trailing comment
- Blank lines are ignored
- Multiple regions on a single line separated by commas also works
- A UTF-8 BOM at the start of the file is handled

`--regions-file` and `--regions` can be combined; results are deduped, preserving order from the file first then the command line.

## The BOM template

Open `bom_template.xlsx` and go to the **BOM** sheet. Select services from the dropdown in column A. Every service listed will be checked as a requirement.

The full list of supported services is in the **Catalog** sheet. To add a service not already in the catalog, add a new row to the Catalog sheet with:

| Column | Value |
|--------|-------|
| Service Name | Any label you choose |
| Provider | Azure resource provider (e.g. `Microsoft.Network`) |
| Resource Type | Resource type (e.g. `virtualNetworks`) |
| Zone Check | `Yes` if the service requires zone-level availability, otherwise `No` |

Then type or select that name in the BOM sheet.

### Required SKUs sheet (optional, consumed by the Azure BOM Region Support Dashboard)

`bom_template.xlsx` also ships with a **Required SKUs** sheet that declares which Azure VM families your build needs (e.g. AKS pool sizes). The [Azure BOM Region Support Dashboard](../../README.md) reads this sheet from the output `region_results_*.xlsx` to drive its ARM queries.

| Column | Required | Example |
|--------|----------|---------|
| Primary Family | yes | `standardDav6Family` |
| Primary Label | no (auto-derived) | `Dav6` |
| Alt Family | no | `standardDASv5Family` |
| Alt Label | no (auto-derived) | `Dasv5` |

The script passes this sheet through unchanged into the output workbook, so peers/customers only need to share one file. A blank row ends the data block — anything below is treated as inline notes. If the sheet is absent, the dashboard falls back to its built-in defaults.

## Output

The results Excel file contains one row per region with an overall status and a per-service breakdown:

| Color | Meaning |
|-------|---------|
| 🟢 Green | All services available in this region |
| 🔴 Red | One or more services not available in this region |

## Supported services (built-in catalog)

| Service | Notes |
|---------|-------|
| Azure Automation | Not available in all regions |
| Premium SSD v2 | Zone check — requires 3 availability zones |
| Azure Firewall | |
| Application Gateway (WAF v2) | |
| Azure Bastion | |
| Azure Private Link | |
| Azure Load Balancer (Standard) | |
| Virtual Network | |
| Azure DNS | |
| Public IP Addresses | |
| Azure NAT Gateway | |
| Azure Kubernetes Service (AKS) | |
| Azure Virtual Machines | |
| Azure Container Registry | |
| Azure Database for MySQL | Flexible Server |
| Azure Database for PostgreSQL | Flexible Server |
| Azure SQL Database | |
| Azure Cosmos DB | |
| Azure Data Lake Storage Gen2 | |
| Azure Blob Storage | |
| Managed Disks (Premium SSD) | |
| Azure Monitor | |
| Azure Key Vault | |
| Azure Service Bus | |
| Azure Event Hub | |
| Azure API Management | |
| Azure App Service | |
| Azure Functions | |
| Azure Cache for Redis | |
| Azure Cognitive Services | |

## Notes

- All availability data is queried **live** from Azure Resource Manager at runtime using your authenticated Azure CLI session
- VM SKU availability (e.g. Standard_D8as_v6) is not checked — use `az vm list-skus --location <region>` for that
- For the latest region/service availability: https://azure.microsoft.com/en-us/explore/global-infrastructure/products-by-region/
