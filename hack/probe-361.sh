#!/usr/bin/env bash
#
# What leaves the controller? An access-log probe for rest-dynamic-controller.
#
# Points this chart at a mock Nova inside kind and reads the MOCK'S ACCESS LOG,
# because that records what actually went on the wire rather than what the
# controller reports having done. A CR's status tells you what the controller
# concluded; the access log tells you what it did. Those differed here.
#
# WHAT IT FOUND. oasgen-provider#361: rest-dynamic-controller implemented none
# of OpenAPI's path-level parameter inheritance -- `pathItem` was fetched and
# discarded (clienttools.go:203) -- so a `required` parameter declared on a path
# item was never validated, and a request whose placeholder never got
# substituted was SENT ANYWAY. Fixed in their #363, released in 0.48.0.
#
# The before/after, one unmodified instrument, oasgen version the only variable:
#
#   0.43.0   GET /v2.1/flavors/%7Bid%7D             200    <- literal {id}
#            GET /v2.1/flavors/probe-id-0123456789  200
#            ...and NO POST, ever
#
#   0.48.0   POST /v2.1/flavors                     200    <- create proceeds
#            GET  /v2.1/flavors/probe-id-0123456789 200
#            ...and NO literal, ever
#
# The literal disappearing is the fix and is server-independent. The create
# appearing is NOT a general property of the fix: this mock answers any
# /flavors/<x> with 200, so on 0.43.0 the literal came back affirmative, the
# controller concluded the flavor existed, and never created it. Nova returns
# 404 itemNotFound, so against real Nova the create would most likely have
# happened anyway. Stated here because the distinction is easy to lose.
#
# WHY THIS FILE IS KEPT. Nine runs, and the first eight failed on a defect of
# the INSTRUMENT rather than of the subject. Each fix is still in the code
# below, because the next probe anyone writes will face the same temptations:
#
#   1. THE MOCK ANSWERED THE QUESTION IT WAS ASKING. Returning 200 for the
#      literal /flavors/{id} told the controller the resource existed, which
#      suppressed the create for three runs -- and made the mock the source of
#      the very status.flavor.id whose absence was under test. The instrument
#      supplied the value it was measuring. Correct design: LOG the literal and
#      REFUSE it (404), so the create still proceeds.
#
#   2. THE COUNT INCLUDED THE MEASURER. The pre-flight curl checks hit the same
#      mock, so "substituted: 2" was one controller request and one of mine --
#      and from that I drew the OPPOSITE conclusion from the truth. Every count
#      now filters on the subject's user-agent and reports how many lines it
#      excluded, so the filter's effect is visible rather than silent.
#
#   3. AN EMPTY FILTER READS AS A CLEAN PASS. If the user-agent matches nothing,
#      all three counts are 0 -- indistinguishable from "the controller sent
#      nothing wrong". So the script refuses to report instead: a zero from an
#      unread source is the most plausible-looking number there is, because 0 is
#      exactly what a correct negative looks like.
#
#   4. A RUN THAT NEVER HAPPENED IS NOT A PASS. The load gate exits non-zero and
#      says NEVER RAN, because an empty result that exits 0 gets filed as
#      success by whoever reads only the exit code.
#
#   5. ORDERING WAS THE FINDING, AND A SUMMARY DESTROYED IT. `sort | uniq -c`
#      discarded the sequence that distinguished "sent before an id existed"
#      from "sent regardless". The raw ordered log is printed first and the
#      counts are labelled "for convenience only".
#
# Usage:
#   hack/probe-361.sh                         # against the repo's pinned version
#   OASGEN_VERSION=0.43.0 hack/probe-361.sh   # the "before" side of the above
#   CLUSTER=my-name hack/probe-361.sh         # a kind cluster of your choosing
#
# Needs kind, kubectl, helm. Takes ~10 minutes and creates a kind cluster it
# deletes on exit. NO CREDENTIALS: nova.url is a cluster Service, so nothing
# here contacts OpenStack or any external endpoint.
set -euo pipefail

CLUSTER=${CLUSTER:-nova-361}
NS=krateo-system
OASGEN_VERSION=${OASGEN_VERSION:-0.48.0}
GROUP=nova.openstack.krateo.io
FAKE_ID=probe-id-0123456789
MOCK_URL="http://mocknova.${NS}.svc.cluster.local:8080/v2.1"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m  OK: %s\033[0m\n' "$*"; }
bad()  { printf '\033[31m  BAD: %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }

[ -f chart/Chart.yaml ] || fail "run me from the repo root"

# Respect my own gate. Cluster creation succeeded at 4.7 and failed at ~50.
L=$(uptime | sed 's/.*load averages*: *//' | tr ',' '.' | awk '{print $1}'); LI=${L%%.*}
if [ "${LI:-99}" -ge 15 ]; then
  echo "load is $L on $(sysctl -n hw.ncpu) cores; my own gate is 15. Not running."
  echo "NOT A RESULT -- nothing was measured."
  exit 4
fi
echo "  load $L -- under the gate, proceeding"

STAGE=$(mktemp -d)
cp -R ./chart "$STAGE/chart"
sed "s/CHART_VERSION/0.0.0-ci/g" ./chart/Chart.yaml > "$STAGE/chart/Chart.yaml"
cleanup() { rm -rf "$STAGE"; say "cleanup"; kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

say "cluster + oasgen-provider ${OASGEN_VERSION}"
kind get clusters | grep -qx "$CLUSTER" || kind create cluster --name "$CLUSTER" >/dev/null
kubectl config use-context "kind-$CLUSTER" >/dev/null
helm upgrade --install oasgen-provider-crds \
  oci://ghcr.io/krateo-platformops/charts/oasgen-provider-crds \
  --version "$OASGEN_VERSION" -n "$NS" --create-namespace --wait >/dev/null
helm upgrade --install oasgen-provider \
  oci://ghcr.io/krateo-platformops/charts/oasgen-provider \
  --version "$OASGEN_VERSION" -n "$NS" --wait --timeout 10m >/dev/null
ok "oasgen-provider installed"

say "mock Nova that ACCEPTS the create and returns id=${FAKE_ID}"
# QUOTED heredoc: nothing inside is shell-expanded, so nginx variables and
# punctuation are safe. The id is injected by sed afterwards. Escaping an
# unquoted heredoc instance by instance cost two runs: backticks became
# command substitution and an nginx variable tripped set -u. Removing the
# class beats escaping each case.
MOCK_MANIFEST=$(cat <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: mocknova-conf
data:
  default.conf: |
    server {
      listen 8080;
      access_log /dev/stdout;
      default_type application/json;

      # The collection: POST creates, and the envelope carries the identifier
      # the RestDefinition maps from (identifiers: flavor.id; the get verb's
      # {id} comes from status.flavor.id).
      # EMPTY collection on GET, the created object on POST. Attempt six
      # returned a name-matching flavor here, and 'identifiers' includes
      # flavor.name -- so the controller correctly concluded the resource
      # already existed and never issued a POST. That was my mock, not a
      # controller defect, and it cost the discriminating question.
      #
      # the shell also eats backticks and \$variables. nginx's own variables
      location = /v2.1/flavors {
        if ($request_method = POST) {
          return 200 '{"flavor":{"id":"__FAKE_ID__","name":"probe-361","ram":2048,"vcpus":1,"disk":20}}';
        }
        return 200 '{"flavors":[]}';
      }

      # Any item path. Which one the controller asks for IS the measurement, so
      # this deliberately answers both the substituted and the literal form --
      # answering only the correct one would hide the defect behind a 404.
      location ~ ^/v2\\.1/flavors/.+$ {
        return 200 '{"flavor":{"id":"__FAKE_ID__","name":"probe-361","ram":2048,"vcpus":1,"disk":20}}';
      }

      location / { return 404 '{"itemNotFound":{"message":"not found","code":404}}'; }
    }
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mocknova
spec:
  replicas: 1
  selector: { matchLabels: { app: mocknova } }
  template:
    metadata: { labels: { app: mocknova } }
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 65534
        runAsGroup: 65534
        seccompProfile: { type: RuntimeDefault }
      containers:
        - name: nginx
          image: nginxinc/nginx-unprivileged:1.27-alpine
          ports: [{ containerPort: 8080 }]
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: false
            capabilities: { drop: ["ALL"] }
          resources:
            requests: { cpu: 10m, memory: 16Mi }
            limits:   { cpu: 200m, memory: 128Mi }
          volumeMounts:
            - { name: conf, mountPath: /etc/nginx/conf.d }
      volumes:
        - name: conf
          configMap: { name: mocknova-conf }
---
apiVersion: v1
kind: Service
metadata:
  name: mocknova
spec:
  selector: { app: mocknova }
  ports: [{ port: 8080, targetPort: 8080 }]
EOF
)
printf '%s\n' "$MOCK_MANIFEST" | sed "s/__FAKE_ID__/$FAKE_ID/g" | kubectl apply -n "$NS" -f - >/dev/null
kubectl -n "$NS" rollout status deploy/mocknova --timeout=180s >/dev/null
# Prove the mock answers BOTH shapes before trusting any conclusion drawn from
# which one the controller picks.
kubectl -n "$NS" run mockprobe --rm -i --restart=Never --image=curlimages/curl:8.11.0 -- \
  -s -o /dev/null -w 'collection=%{http_code}\n' "$MOCK_URL/flavors" | sed 's/^/  /'
kubectl -n "$NS" run mockprobe2 --rm -i --restart=Never --image=curlimages/curl:8.11.0 -- \
  -s -o /dev/null -w 'item=%{http_code}\n' "$MOCK_URL/flavors/$FAKE_ID" | sed 's/^/  /'
ok "mock up at $MOCK_URL"

say "chart pointed at the mock"
helm upgrade --install nova "$STAGE/chart" -n "$NS" \
  --set nova.url="$MOCK_URL" --wait --timeout 5m >/dev/null
RD_CRD=restdefinitions.ogen.krateo.io
for _ in $(seq 1 60); do kubectl get crd "$RD_CRD" >/dev/null 2>&1 && break; sleep 3; done
kubectl wait --for=condition=Established "crd/$RD_CRD" --timeout=180s >/dev/null
kubectl -n "$NS" wait "$RD_CRD" --all --for=condition=Ready --timeout=420s >/dev/null || true
for _ in $(seq 1 60); do kubectl get crd "computeflavors.$GROUP" >/dev/null 2>&1 && break; sleep 5; done
kubectl wait --for=condition=Established "crd/computeflavors.$GROUP" --timeout=180s >/dev/null
ok "ComputeFlavor CRD Established"

say "apply a ComputeFlavor (apiVersion v2-1, per the published GVK)"
kubectl -n "$NS" create secret generic nova-token --from-literal=token=dummy-not-a-credential >/dev/null
kubectl apply -f - >/dev/null <<'EOF'
apiVersion: nova.openstack.krateo.io/v1alpha1
kind: ComputeFlavorConfiguration
metadata: { name: nova-config, namespace: krateo-system }
spec:
  authentication:
    apiKey:
      tokenRef: { name: nova-token, namespace: krateo-system, key: token }
      header: X-Auth-Token
---
apiVersion: nova.openstack.krateo.io/v2-1
kind: ComputeFlavor
metadata: { name: probe-361, namespace: krateo-system }
spec:
  configurationRef: { name: nova-config, namespace: krateo-system }
  flavor: { name: probe-361, ram: 2048, vcpus: 1, disk: 20 }
EOF
ok "applied"

echo "  observing for 240s..."
sleep 240

say "WHAT LEFT THE CONTROLLER (the instrument)"
LOG=$(kubectl -n "$NS" logs deploy/mocknova --tail=-1 2>/dev/null || true)
if [ -z "$LOG" ]; then
  bad "the mock received NOTHING. The precondition failed: the controller never"
  bad "called the endpoint, so no conclusion about substitution is available."
  bad "VOID -- not a result."
  exit 3
fi
echo "  --- RAW, IN ORDER. Never summarise this away: ORDERING is the finding ---"
printf '%s\n' "$LOG" | grep -a 'HTTP/1' | sed 's/^/    /'
echo "  --- counts, for convenience only ---"
printf '%s\n' "$LOG" | grep -aoE '"(GET|POST|PUT|DELETE) [^"]*"' | sort | uniq -c | sed 's/^/    /'

say "VERDICT on #361"
# Count ONLY the subject's own traffic. The pre-flight curl checks above hit the
# same mock, and counting them once produced the OPPOSITE conclusion from the
# truth: 2 of 4 requests were mine, and "both forms were sent" was my own probe
# showing up as the controller's behaviour.
SUBJECT=$(printf '%s\n' "$LOG" | grep -a 'Go-http-client' || true)
EXCLUDED=$(( $(printf '%s\n' "$LOG" | grep -ac 'HTTP/1' || true) - $(printf '%s\n' "$SUBJECT" | grep -ac 'HTTP/1' || true) ))
# CONTROL. If the subject's user-agent matches nothing, all three counts below
# are 0 -- which is indistinguishable from "the controller sent nothing wrong".
# A zero from an empty filter is the most plausible-looking number there is,
# because 0 is exactly what a correct negative looks like. So refuse to report.
if ! printf '%s\n' "$SUBJECT" | grep -aq 'HTTP/1'; then
  echo "  VOID: no Go-http-client requests in the log at all. Either the"
  echo "  controller never reached the mock, or the user-agent changed. Every"
  echo "  count below would be 0 and would read as a clean pass. Not a result."
  exit 3
fi
echo "  (excluded $EXCLUDED non-subject request(s) -- my own pre-flight curls)"
LITERAL=$(printf '%s\n' "$SUBJECT" | grep -ac '%7Bid%7D' || true)
SUBST=$(printf '%s\n' "$SUBJECT"  | grep -ac "flavors/$FAKE_ID" || true)
POSTS=$(printf '%s\n' "$SUBJECT"  | grep -ac '"POST ' || true)
echo "  POST (create) attempts:          $POSTS"
echo "  GET with LITERAL {id}:           $LITERAL"
echo "  GET with the SUBSTITUTED id:     $SUBST"
if [ "$SUBST" -gt 0 ] && [ "$LITERAL" -eq 0 ]; then
  ok "ONLY the substituted form was sent -- substitution works whenever a"
  ok "value exists, so the empty case is the whole defect."
elif [ "$POSTS" -eq 0 ] && [ "$SUBST" -eq 0 ] && [ "$LITERAL" -eq 0 ]; then
  bad "no POST and neither GET form: nothing to discriminate. VOID."
elif [ "$SUBST" -gt 0 ] && [ "$LITERAL" -eq 0 ]; then
  ok "substitution WORKS when the value exists. The empty case is the whole"
  ok "defect -- #361 as filed is complete."
elif [ "$SUBST" -gt 0 ] && [ "$LITERAL" -gt 0 ]; then
  bad "BOTH forms were sent. Substitution works sometimes -- ordering or a"
  bad "pre-create observe is involved; the literal form is not limited to the"
  bad "never-created case."
elif [ "$LITERAL" -gt 0 ]; then
  bad "the literal {id} was sent EVEN THOUGH the create succeeded. Substitution"
  bad "is broken independently of whether a value exists -- LARGER than #361."
else
  bad "neither form seen; the controller called something else entirely."
fi

say "#347 arm: does the CR claim Ready without an identifier?"
# Last attempt this collapsed to "unreadable" because only a JSON parse ran and
# an empty read looked identical to a broken one. Dump the raw object first.
RAW=$(kubectl -n "$NS" get computeflavors.$GROUP probe-361 -o yaml 2>&1 || true)
if [ -z "$RAW" ] || printf '%s' "$RAW" | grep -qi '^error\|not found'; then
  bad "cannot read the CR. Raw output follows -- this is VOID, not 'symptom absent':"
  printf '%s\n' "$RAW" | head -20 | sed 's/^/    /'
else
  printf '%s\n' "$RAW" | grep -aE '^\s+(type|status|reason|message|id|name):' | head -24 | sed 's/^/    /'
  kubectl -n "$NS" get computeflavors.$GROUP probe-361 -o json 2>/dev/null | python3 -c '
import json,sys
d=json.load(sys.stdin); st=d.get("status") or {}
conds={c.get("type"):(c.get("status"),c.get("reason")) for c in (st.get("conditions") or [])}
ident=(st.get("flavor") or {}).get("id") or st.get("id") or ""
r=conds.get("Ready",("<absent>",None))
print("  Ready={} reason={}  identifier={!r}".format(r[0], r[1], ident))
if r[0]=="True" and not ident:
    print("  >>> #347 SYMPTOM: Ready=True with no identifier in status")
elif r[0]=="True":
    print("  Ready=True WITH an identifier -- expected, the mock returned one")
else:
    print("  >>> the CR does not claim Ready; #347 symptom not present here")
' || bad "status present but JSON arm failed -- see the raw dump above"
fi

say "DONE -- no credentials used, nothing contacted OpenStack"
