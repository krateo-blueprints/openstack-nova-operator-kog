#!/usr/bin/env bash
# Watch ONE generated controller try to read an apiKey Secret, at ONE oasgen
# version, on a fresh kind cluster, and write the two witnesses' raw material to
# a work directory for hack/measure-apikey-secret-read.py to classify.
#
# WHY. Everything else this repo knows about the apiKey grant is from READING
# FILES (hack/measure-apikey-grant.py). This is the arm that watches it happen.
# See that script's docstring for the witnesses and the verdict table; this file
# only GATHERS, and a failure to gather is VOID, never a verdict.
#
# CREDENTIAL-FREE: the Secret holds a placeholder and the endpoint is
# nova.invalid (RFC 2606, unroutable), so no packet can reach a cloud. The
# question is whether the controller gets PAST the secret read, not whether any
# vendor accepts what is in it.
#
# usage: probe-apikey-secret-read.sh <subject|control> <oasgen-version> <workdir>
# exit:  0 gathered (the classifier decides), 3 could not gather -- VOID
set -uo pipefail
export LC_ALL=C

# shellcheck source=hack/lib/wait.sh
. "$(dirname "$0")/lib/wait.sh"

ROLE="${1:-}"; VERSION="${2:-}"; WORK="${3:-}"
[ -n "$ROLE" ] && [ -n "$VERSION" ] && [ -n "$WORK" ] || {
  echo "usage: probe-apikey-secret-read.sh <subject|control> <oasgen-version> <workdir>" >&2
  exit 2
}
NS="${NS:-krateo-system}"
CLUSTER="apikey-read-$ROLE"
CTX="kind-$CLUSTER"
HOST="nova.invalid"
SECRET="apikey-arm-token"
KIND="ComputeKeypair"
GROUP="nova.openstack.krateo.io"
mkdir -p "$WORK"

K() { kubectl --context "$CTX" "$@"; }
H() { helm --kube-context "$CTX" "$@"; }
void() { echo "::error::VOID ($ROLE $VERSION) -- $1"; echo "::error::The question was not asked. This is NOT a pass and NOT a finding about the provider."; exit 3; }

echo "== $ROLE: oasgen-provider $VERSION on a fresh cluster $CLUSTER"
kind create cluster --name "$CLUSTER" --image kindest/node:v1.36.1 --wait 120s \
  || void "could not create the kind cluster"

H upgrade --install oasgen-provider-crds oci://ghcr.io/krateo-platformops/charts/oasgen-provider-crds \
  --version "$VERSION" -n "$NS" --create-namespace --wait \
  || void "oasgen-provider-crds $VERSION did not install"
H upgrade --install oasgen-provider oci://ghcr.io/krateo-platformops/charts/oasgen-provider \
  --version "$VERSION" -n "$NS" --wait --timeout 10m \
  || void "oasgen-provider $VERSION did not install"

# A stamped copy, because helm refuses the chart with CHART_VERSION in it.
STAGE="$WORK/chart"
rm -rf "$STAGE"; mkdir -p "$WORK"
cp -R ./chart "$STAGE"
sed "s/CHART_VERSION/0.0.0-ci/g" ./chart/Chart.yaml > "$STAGE/Chart.yaml"
grep -q CHART_VERSION "$STAGE/Chart.yaml" && void "could not stamp the chart version"
H upgrade --install nova "$STAGE" -n "$NS" --set "nova.url=https://$HOST/v2.1/p" --wait --timeout 5m \
  || void "the chart failed to render or install at oasgen $VERSION"

RD=restdefinitions.ogen.krateo.io
wait_for "the RestDefinition CRD" 60 3 K get crd "$RD" || void "the provider never created $RD"
K -n "$NS" wait "$RD" --all --for=condition=Ready --timeout=420s \
  || { K -n "$NS" describe "$RD" | tail -60; void "the RestDefinitions never reached Ready, so no controller exists to watch"; }

# The CRDs, by kind -- derived rather than typed, because a typed plural that
# does not exist reads as "nothing happened".
crd_by_kind() { # <kind> <jsonpath-field>
  K get crd -o jsonpath="{range .items[?(@.spec.names.kind==\"$1\")]}{$2}{\" \"}{end}" 2>/dev/null
}
PLURAL=$(crd_by_kind "$KIND" .spec.names.plural); PLURAL="${PLURAL%% *}"
CFG_PLURAL=$(crd_by_kind "${KIND}Configuration" .spec.names.plural); CFG_PLURAL="${CFG_PLURAL%% *}"
RES_VER=$(crd_by_kind "$KIND" '.spec.versions[0].name'); RES_VER="${RES_VER%% *}"
CFG_VER=$(crd_by_kind "${KIND}Configuration" '.spec.versions[0].name'); CFG_VER="${CFG_VER%% *}"
[ -n "$PLURAL" ] && [ -n "$CFG_PLURAL" ] && [ -n "$RES_VER" ] && [ -n "$CFG_VER" ] \
  || void "could not derive the CRDs for $KIND and ${KIND}Configuration (plural='$PLURAL' cfg='$CFG_PLURAL')"
echo "  $KIND -> $PLURAL ($GROUP/$RES_VER), configuration $CFG_PLURAL ($GROUP/$CFG_VER)"
echo "$PLURAL" > "$WORK/plural"; echo "$SECRET" > "$WORK/secret"; echo "$HOST" > "$WORK/host"

K -n "$NS" create secret generic "$SECRET" --from-literal=token=placeholder-not-a-credential \
  || void "could not create the fixture Secret"
K apply -f - <<YAML || void "the ${KIND}Configuration was refused"
apiVersion: $GROUP/$CFG_VER
kind: ${KIND}Configuration
metadata:
  name: apikey-arm-cfg
  namespace: $NS
spec:
  authentication:
    apiKey:
      tokenRef:
        name: $SECRET
        namespace: $NS
        key: token
      header: X-Auth-Token
YAML

# oasgen watches RestDefinitions, not Configurations, so a Configuration does
# not wake it: touch to force the reconcile (an annotation is a main-resource
# write; it cannot fake the outcome, only oasgen writes the Role).
for rd in $(K -n "$NS" get "$RD" -o name); do
  K -n "$NS" annotate "$rd" "apikey-arm//touch=$(date +%s)" --overwrite >/dev/null || true
done

# WITNESS A -- the Role, read from the cluster. Bounded: a timeout here is the
# EXPECTED outcome for the negative control (no Role is ever written for apiKey)
# and is classified by the script as ABSENT rather than treated as an error.
# shellcheck disable=SC2329  # invoked by name through wait_for
roles_have_secret() {
  K get role -A -o json > "$WORK/roles.json" 2>"$WORK/roles.err" || return 1
  python3 hack/measure-apikey-secret-read.py probe-a --roles "$WORK/roles.json" --plural "$PLURAL" --secret "$SECRET" >/dev/null
}
echo "  waiting up to 240s for a Role granting secrets/$SECRET (expected to time out for the control)"
wait_for "a Role granting secrets/$SECRET" 80 3 roles_have_secret >/dev/null 2>&1 || echo "  no such Role appeared within 240s"
K get role -A -o json > "$WORK/roles.json" || { rm -f "$WORK/roles.json"; echo "  roles unreadable"; }
echo "--- the authsecrets Roles, as the cluster holds them ---"
K get role -A 2>&1 | grep -E 'NAME|authsecrets' || echo "(none)"
K get role -A -o yaml 2>/dev/null | python3 -c '
import sys,yaml
for r in yaml.safe_load(sys.stdin.read() or "{}").get("items",[]):
    if r["metadata"]["name"].endswith("-authsecrets"):
        print(yaml.safe_dump({"name":r["metadata"]["name"],"namespace":r["metadata"]["namespace"],"rules":r.get("rules")}))' || true

# WITNESS B -- the behaviour. Create the resource and gather what the
# controller logged and what the CR says about itself.
K apply -f - <<YAML || void "the $KIND CR was refused"
apiVersion: $GROUP/$RES_VER
kind: $KIND
metadata:
  name: apikey-arm-key
  namespace: $NS
spec:
  configurationRef: { name: apikey-arm-cfg, namespace: $NS }
  keypair:
    name: apikey-arm-key
YAML

gather() {
  : > "$WORK/evidence.txt"
  local d
  for d in $(K -n "$NS" get deploy -o name 2>/dev/null | grep -E "/.*${PLURAL}" | grep -vE 'oasgen|core-provider'); do
    { echo "### logs $d"; K -n "$NS" logs "$d" --all-containers --tail=400 2>&1; \
      K -n "$NS" logs "$d" --all-containers --previous --tail=400 2>&1 || true; } >> "$WORK/evidence.txt"
  done
  { echo "### conditions"; K -n "$NS" get "$PLURAL" apikey-arm-key -o json 2>&1 \
      | python3 -c '
import sys,json
try: d=json.load(sys.stdin)
except Exception as e: print("unparseable CR:", e); sys.exit(0)
for c in (d.get("status") or {}).get("conditions") or []: print(c.get("type"),c.get("status"),c.get("reason"),c.get("message"))'
    echo "### events"; K -n "$NS" get events -o custom-columns='REASON:.reason,OBJ:.involvedObject.name,MSG:.message' 2>&1; } >> "$WORK/evidence.txt"
}
# shellcheck disable=SC2329  # invoked by name through wait_for
probe_b() { gather; python3 hack/measure-apikey-secret-read.py probe-b --evidence "$WORK/evidence.txt" --host "$HOST" >/dev/null; }
echo "  waiting up to 300s for the controller to reach EITHER the endpoint OR a secrets Forbidden"
wait_for "the controller to reach the endpoint or be Forbidden" 60 5 probe_b >/dev/null 2>&1 || echo "  neither was seen within 300s"
gather
echo "--- evidence tail ---"; tail -40 "$WORK/evidence.txt"
exit 0
