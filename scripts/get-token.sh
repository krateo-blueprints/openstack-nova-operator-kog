#!/usr/bin/env bash
# get-token.sh - Fetches a Keystone v3 unscoped/scoped token from an
# OpenStack cloud (OVH-by-default) and prints it on stdout, optionally
# rendering a kubectl Secret manifest with `--secret` so you can pipe
# directly to `kubectl apply -f -`.
#
# Required env vars (all overridable; defaults match OVH Public Cloud):
#   OS_AUTH_URL          default: https://auth.cloud.ovh.net/v3
#   OS_USERNAME          OpenStack user name (NOT your OVH login)
#   OS_PASSWORD          OpenStack user password
#   OS_USER_DOMAIN_NAME  default: Default
#   OS_PROJECT_ID        OVH "tenant" / project id (32-hex string)
#   OS_PROJECT_DOMAIN_NAME  default: Default
#   OS_REGION_NAME       default: GRA11
#
# Optional flags:
#   --secret  emit a kubectl Secret manifest (consumed by the
#             InstanceConfiguration CR) instead of the raw token.
#   --upstream  print the Nova endpoint URL discovered from the catalog
#               in addition to (or instead of) the token. Useful to
#               populate nova.url in values.yaml.
#
# Examples:
#   ./scripts/get-token.sh
#   ./scripts/get-token.sh --upstream
#   ./scripts/get-token.sh --secret | kubectl apply -f -

set -euo pipefail

: "${OS_AUTH_URL:=https://auth.cloud.ovh.net/v3}"
: "${OS_USER_DOMAIN_NAME:=Default}"
: "${OS_PROJECT_DOMAIN_NAME:=Default}"
: "${OS_REGION_NAME:=GRA11}"

emit_secret=0
emit_upstream=0
for arg in "$@"; do
  case "$arg" in
    --secret)   emit_secret=1 ;;
    --upstream) emit_upstream=1 ;;
    -h|--help)  sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

for v in OS_USERNAME OS_PASSWORD OS_PROJECT_ID; do
  if [[ -z "${!v:-}" ]]; then
    echo "missing required env var: $v" >&2
    exit 2
  fi
done

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required" >&2
  exit 2
fi

payload=$(jq -n \
  --arg user      "$OS_USERNAME" \
  --arg pass      "$OS_PASSWORD" \
  --arg userdom   "$OS_USER_DOMAIN_NAME" \
  --arg project   "$OS_PROJECT_ID" \
  --arg projdom   "$OS_PROJECT_DOMAIN_NAME" \
  '{auth:{
      identity:{
        methods:["password"],
        password:{user:{name:$user,password:$pass,domain:{name:$userdom}}}
      },
      scope:{project:{id:$project,domain:{name:$projdom}}}
    }}')

tmp_headers=$(mktemp)
trap 'rm -f "$tmp_headers"' EXIT

# Normalize to the Keystone v3 base. The OVH openrc exports
# OS_AUTH_URL=https://auth.cloud.ovh.net/ (no /v3) and relies on the
# openstack CLI to append the version; this raw curl needs the /v3.
auth_base="${OS_AUTH_URL%/}"
case "$auth_base" in
  */v3|*/v3.0) ;;
  *) auth_base="$auth_base/v3" ;;
esac

body=$(curl -sS -D "$tmp_headers" \
  -H "Content-Type: application/json" \
  -X POST "${auth_base}/auth/tokens" \
  -d "$payload")

token=$(awk -F': ' 'tolower($1)=="x-subject-token"{print $2}' "$tmp_headers" | tr -d '\r\n')
if [[ -z "$token" ]]; then
  echo "keystone did not return an X-Subject-Token; response body:" >&2
  echo "$body" >&2
  exit 1
fi

# Extract Nova endpoint for the requested region.
nova_url=$(printf '%s' "$body" | jq -r --arg region "$OS_REGION_NAME" '
  .token.catalog[]
  | select(.type=="compute")
  | .endpoints[]
  | select(.interface=="public" and .region==$region)
  | .url' | head -n1)

if (( emit_secret )); then
  cat <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: nova-token
  namespace: krateo-system
type: Opaque
stringData:
  token: "${token}"
EOF
  if (( emit_upstream )) && [[ -n "$nova_url" ]]; then
    echo "# Nova endpoint (set as nova.url):"
    echo "# ${nova_url}"
  fi
elif (( emit_upstream )); then
  echo "TOKEN=${token}"
  echo "NOVA_URL=${nova_url}"
else
  echo "$token"
fi
