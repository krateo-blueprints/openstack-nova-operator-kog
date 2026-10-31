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
#
# PREFERRED instead of OS_USERNAME/OS_PASSWORD/OS_PROJECT_ID -- an application
# credential, which is revocable on its own and cannot hold a role its creator
# lacks, so it does not require regenerating the user's password:
#   OS_APPLICATION_CREDENTIAL_ID
#   OS_APPLICATION_CREDENTIAL_SECRET
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

# Two auth methods. An APPLICATION CREDENTIAL is preferred and is tried first:
# it is created separately, revoked independently, and carries only a subset of
# the creating user's roles -- so verifying this chart does not require handing
# over (or regenerating) the user's own password, which would invalidate it for
# everything else using it.
#
#   OS_APPLICATION_CREDENTIAL_ID + OS_APPLICATION_CREDENTIAL_SECRET
#     -> methods:["application_credential"], and NO scope block
#   OS_USERNAME + OS_PASSWORD + OS_PROJECT_ID
#     -> methods:["password"], scoped to the project
AUTH_METHOD=""
if [[ -n "${OS_APPLICATION_CREDENTIAL_ID:-}" || -n "${OS_APPLICATION_CREDENTIAL_SECRET:-}" ]]; then
  # Half-set is a configuration error, not a reason to fall back: silently
  # using the password when an app credential was intended would hand the
  # stronger credential to Keystone without the operator realising.
  for v in OS_APPLICATION_CREDENTIAL_ID OS_APPLICATION_CREDENTIAL_SECRET; do
    if [[ -z "${!v:-}" ]]; then
      echo "application-credential auth selected but $v is unset." >&2
      echo "Set BOTH OS_APPLICATION_CREDENTIAL_ID and ..._SECRET, or neither." >&2
      exit 2
    fi
  done
  AUTH_METHOD=application_credential
else
  for v in OS_USERNAME OS_PASSWORD OS_PROJECT_ID; do
    if [[ -z "${!v:-}" ]]; then
      echo "missing required env var: $v" >&2
      echo "(or set OS_APPLICATION_CREDENTIAL_ID + ..._SECRET to use an" >&2
      echo " application credential instead, which needs neither a password" >&2
      echo " nor a project id)" >&2
      exit 2
    fi
  done
  AUTH_METHOD=password
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "jq is required" >&2
  exit 2
fi

if [[ "$AUTH_METHOD" == application_credential ]]; then
  # NO `scope` block, deliberately. The Identity API reference is explicit:
  # "You are not allowed to request a scope, as the scope is retrieved from the
  # application credential." Sending one is rejected, and the rejection does not
  # obviously point at the scope -- which is why this is a separate payload
  # rather than the password one with a field swapped.
  payload=$(jq -n \
    --arg acid    "$OS_APPLICATION_CREDENTIAL_ID" \
    --arg acsec   "$OS_APPLICATION_CREDENTIAL_SECRET" \
    '{auth:{
        identity:{
          methods:["application_credential"],
          application_credential:{id:$acid,secret:$acsec}
        }
      }}')
else
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
fi

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

# An empty nova_url has THREE indistinguishable causes, and --upstream exists
# precisely to produce this value -- so emitting an empty one with exit 0 turns a
# diagnosable misconfiguration into a silent blank that surfaces much later as a
# chart that will not install. Say which case it is. Only service types and region
# names are printed; never the body, which carries credentials.
if [[ -z "$nova_url" ]] && (( emit_upstream )); then
  types=$(printf '%s' "$body" | jq -r '[.token.catalog[].type] | sort | join(", ")' 2>/dev/null || true)
  cregions=$(printf '%s' "$body" | jq -r '
    [.token.catalog[] | select(.type=="compute") | .endpoints[] | .region] | unique | join(", ")' 2>/dev/null || true)
  cifaces=$(printf '%s' "$body" | jq -r '
    [.token.catalog[] | select(.type=="compute") | .endpoints[] | .interface] | unique | join(", ")' 2>/dev/null || true)
  {
    echo "no Nova (type=compute) endpoint found in the Keystone catalog."
    if [[ -z "$cregions" ]]; then
      echo "  cause: the catalog has NO compute service at all."
      echo "  services present: ${types:-<none>}"
      echo "  An identity-only deployment cannot serve this chart; you need a Nova."
    elif [[ "$cifaces" != *public* ]]; then
      echo "  cause: compute exists but publishes no 'public' interface."
      echo "  interfaces present for compute: ${cifaces}"
    else
      echo "  cause: compute has a public interface, but not in region '${OS_REGION_NAME}'."
      echo "  regions available for compute: ${cregions}"
      echo "  Set OS_REGION_NAME to one of those, or re-scope the token."
    fi
  } >&2
  exit 3
fi

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
