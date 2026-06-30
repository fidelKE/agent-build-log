#!/usr/bin/env bash
# Seed HashiCorp Vault dev mode with Conductor secrets.
# Run once after `podman compose up -d` and before starting the agent.
#
# Usage:
#   CATALOG_API_TOKEN=<your-token> bash vault_setup.sh

set -euo pipefail

VAULT_ADDR="${VAULT_ADDR:-http://localhost:8200}"
VAULT_TOKEN="${VAULT_TOKEN:-dev-root-token}"

if [[ -z "${CATALOG_API_TOKEN:-}" ]]; then
  echo "ERROR: CATALOG_API_TOKEN env var must be set"
  echo "Usage: CATALOG_API_TOKEN=<token> bash vault_setup.sh"
  exit 1
fi

echo "Enabling KV v2 secrets engine at 'secret/'..."
vault secrets enable -address="$VAULT_ADDR" -version=2 kv 2>/dev/null || echo "(already enabled)"

echo "Writing catalog-api-token to Vault..."
VAULT_ADDR="$VAULT_ADDR" VAULT_TOKEN="$VAULT_TOKEN" \
  vault kv put secret/conductor/catalog-api-token value="$CATALOG_API_TOKEN"

echo "Verifying (value is hidden)..."
VAULT_ADDR="$VAULT_ADDR" VAULT_TOKEN="$VAULT_TOKEN" \
  vault kv get -field=value secret/conductor/catalog-api-token | \
  sed 's/./*/g'

echo "Done. Vault seeded successfully."
