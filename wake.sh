#!/usr/bin/env bash
set -euo pipefail

APP_NAME="azure-penny-prod"
RG="azure-penny-prod-rg"
URL="https://azure-penny-prod.ambitiouswave-074f51bf.eastus.azurecontainerapps.io"

echo "Waking up azure-penny..."

az containerapp update \
  --name "$APP_NAME" \
  --resource-group "$RG" \
  --min-replicas 1 \
  --output none 2>/dev/null

echo "Waiting for app to respond at $URL"

until curl -sf --max-time 5 "$URL" > /dev/null 2>&1; do
  echo -n "."
  sleep 3
done

echo ""
echo "App is up! Open: $URL"
echo "       Docs:     $URL/docs"

# Optionally open in browser (Linux)
if command -v xdg-open &>/dev/null; then
  xdg-open "$URL"
fi
