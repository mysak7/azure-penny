#!/usr/bin/env bash
# Creates subscription-level monthly budget alerts at every $10 increment.
# Run once after az login; safe to re-run (existing budgets are skipped).
#
# Usage: ./scripts/set-budget-alerts.sh [EMAIL] [MAX_USD]
#   EMAIL   : notification recipient (default: mikezspam@gmail.com)
#   MAX_USD : upper bound in USD      (default: 200, step is always $10)

set -euo pipefail

EMAIL="${1:-mikezspam@gmail.com}"
MAX_USD="${2:-200}"
STEP=10
SUBSCRIPTION_ID=$(az account show --query id -o tsv)
START_DATE=$(date +"%Y-%m-01")

echo "Subscription : $SUBSCRIPTION_ID"
echo "Alert email  : $EMAIL"
echo "Alerts every : \$${STEP} up to \$${MAX_USD}"
echo ""

for amount in $(seq "$STEP" "$STEP" "$MAX_USD"); do
  NAME="penny-alert-${amount}usd"

  existing=$(az consumption budget list \
    --query "[?name=='$NAME'].name" -o tsv 2>/dev/null || true)
  if [[ -n "$existing" ]]; then
    echo "  skip   \$${amount} — '$NAME' already exists"
    continue
  fi

  az consumption budget create \
    --budget-name "$NAME" \
    --amount "$amount" \
    --category Cost \
    --time-grain Monthly \
    --start-date "$START_DATE" \
    --end-date "2099-12-31" \
    --notifications "{\"alert\":{\"enabled\":true,\"operator\":\"GreaterThanOrEqualTo\",\"threshold\":100,\"contactEmails\":[\"${EMAIL}\"],\"thresholdType\":\"Actual\"}}" \
    --output none

  echo "  created \$${amount} — '$NAME'"
done

echo ""
echo "Done. Verify with:"
echo "  az consumption budget list --query \"[?starts_with(name,'penny-alert-')].{name:name,amount:amount}\" -o table"
