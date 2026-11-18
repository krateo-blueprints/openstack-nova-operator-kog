#!/usr/bin/env bash
# Does ComputeQuota's PUT ever actually fire?
#
# WHY THIS EXISTS, and it is a correction of an earlier test rather than a new
# idea. `docs/configuration.md`'s 0.52.0 row records a wire test that unparked
# this resource: "PUT /v2.1/p/os-quota-sets/proj-arm23 body={...} -- template
# substituted, tenant_id absent from the body, CR Ready=True/Available". That
# test ran against a REQUEST-LOGGING STAND-IN, and it was right about everything
# it measured. It was also silent about the thing that mattered.
#
# Nova's GET /os-quota-sets/{tenant_id} HAS NO EXISTENCE CHECK. Verified at
# stable/2026.1, nova/api/openstack/compute/quota_sets.py `show` (L123-134): a
# policy check, then `identity.verify_project_id` -- which verifies the PROJECT
# exists in Keystone, NOT that a quota set was ever written -- then the
# effective quotas. So it answers 200 for any project the credential can see,
# on the first reconcile.
#
# rest-dynamic-controller therefore concludes the resource already exists. With
# create/get/delete only, there was nothing left to call: Ready=True, and the
# PUT never fired. The old stand-in did not implement that always-200 GET, so
# its existence check came back "not found", the create ran, and the test
# confirmed the resource was CALLABLE while hiding that it would never be
# CALLED.
#
# THE INSTRUMENT IS THE POINT OF THIS SCRIPT. A mock that differs from the
# vendor in precisely the way the bug depends on will certify the bug as fixed.
# So `--validate-mock` proves the mock reproduces Nova's behaviour BEFORE any
# verdict is read from it, and it needs no cluster.
#
# MODES
#   --validate-mock  stand the mock up locally, prove GET answers 200 with a
#                    quota_set body having PUT nothing, and that PUTs are
#                    recorded. No cluster, no network beyond localhost.
#   --self-test      drive the verdict table over fixtures. No mock, no cluster.
#   (no flag)        usage. A real run needs a cluster and is not implied.
set -euo pipefail
export LC_ALL=C

VOID=3
FAIL=1
OK=0

say()  { printf '\n=== %s ===\n' "$*"; }
note() { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32mOK\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------------------
# the verdict, pure, over counted requests
# ---------------------------------------------------------------------------
# verdict <get-count> <put-count>
#
# A PUT COUNT OF ZERO IS THE DEFECT, not a quiet result: it is exactly what a
# create-only ComputeQuota produces against a Nova-shaped endpoint, and it comes
# with Ready=True on the CR. A GET count of zero means the controller never
# reached the endpoint at all, which is unmeasured rather than passing -- the
# distinction the old test could not make.
verdict() {
  local gets="$1" puts="$2"
  if [ "$gets" -eq 0 ]; then
    printf 'VOID: the endpoint saw NO GET at all, so the controller never reached it.\n'
    printf '  Nothing was measured. A zero PUT count here would say nothing about\n'
    printf '  whether the quota is written -- only that this probe cannot see.\n'
    return "$VOID"
  fi
  if [ "$puts" -eq 0 ]; then
    printf 'FAIL: %s GET(s) and ZERO PUTs. The quota was never written.\n' "$gets"
    printf '  This is the create-only shape against a Nova-shaped endpoint: the GET\n'
    printf '  answers 200 with no existence check, the resource reads as existing,\n'
    # shellcheck disable=SC2016  # the backticks are prose, and the
    # self-test greps for this exact phrase -- expanding them breaks both.
    printf '  and with no `update` verb there is nothing left to call. The CR\n'
    printf '  reaches Ready=True while the quota keeps its defaults.\n'
    return "$FAIL"
  fi
  printf 'OK: %s GET(s) and %s PUT(s) -- the quota was actually written.\n' "$gets" "$puts"
  return "$OK"
}

# ---------------------------------------------------------------------------
# the mock: Nova-shaped, which is the whole correction
# ---------------------------------------------------------------------------
mock_py() {
  cat <<'PYEOF'
import json, re, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

# Requests this mock was asked for, so the probe can count them instead of
# parsing a log. `writes` records what a PUT actually carried.
SEEN = {"gets": 0, "puts": 0, "writes": []}

# What a PUT has set, per tenant. Deliberately starts EMPTY: the point is that a
# GET answers 200 anyway.
STORE = {}

DEFAULTS = {"instances": 10, "cores": 20, "ram": 51200}

# `p` is the project segment nova.url requires; the chart is pointed at
# .../v2.1/p so every path carries it.
QS = re.compile(r"^/v2\.1/p/os-quota-sets/([^/?]+)$")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/__state":
            return self._send(200, SEEN)
        m = QS.match(self.path)
        if not m:
            return self._send(404, {"itemNotFound": {"message": "not found", "code": 404}})
        SEEN["gets"] += 1
        tenant = m.group(1)
        # THE BEHAVIOUR THE OLD STAND-IN DID NOT HAVE. Nova's `show` performs no
        # existence check: it verifies the PROJECT and returns the effective
        # quotas. So 200, with defaults, even though nothing was ever PUT here.
        qs = dict(DEFAULTS)
        qs.update(STORE.get(tenant, {}))
        # Real Nova returns `id`, not `tenant_id` -- measured on 2026.1 and
        # recorded in this repo's notes. Kept faithful so a consumer reading
        # status against this mock sees what Nova sends.
        qs["id"] = tenant
        return self._send(200, {"quota_set": qs})

    def do_PUT(self):
        m = QS.match(self.path)
        if not m:
            return self._send(404, {"itemNotFound": {"message": "not found", "code": 404}})
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {"__unparseable__": raw.decode("utf-8", "replace")}
        SEEN["puts"] += 1
        SEEN["writes"].append({"path": self.path, "body": body})
        tenant = m.group(1)
        sent = (body or {}).get("quota_set") or {}
        STORE.setdefault(tenant, {}).update(
            {k: v for k, v in sent.items() if k != "id"})
        qs = dict(DEFAULTS)
        qs.update(STORE[tenant])
        qs["id"] = tenant
        return self._send(200, {"quota_set": qs})

    def do_DELETE(self):
        m = QS.match(self.path)
        if not m:
            return self._send(404, {"itemNotFound": {"message": "not found", "code": 404}})
        STORE.pop(m.group(1), None)
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()


HTTPServer(("0.0.0.0", int(sys.argv[1])), H).serve_forever()
PYEOF
}

# ---------------------------------------------------------------------------
# --validate-mock: prove the instrument before trusting any verdict from it
# ---------------------------------------------------------------------------
validate_mock() {
  local pass=0 failn=0 port=18774
  # NOT `local tmp`, and NOT `local pid`. The EXIT trap below runs after this
  # function has RETURNED, where a local is out of scope: under `set -u` the
  # trap then dies with "tmp: unbound variable" AFTER printing a clean pass, and
  # leaves its temp dir and its mock process behind. hack/render-check.sh
  # records this exact bug in its own self-test, and I reproduced it here two
  # hours later -- which is why it is written down in both places rather than
  # quietly fixed in one.
  tmp=$(mktemp -d)
  # shellcheck disable=SC2329  # invoked by the EXIT trap below
  cleanup() { [ -n "${pid:-}" ] && kill "$pid" 2>/dev/null; rm -rf "$tmp"; }
  trap cleanup EXIT
  mock_py > "$tmp/mock.py"
  python3 -I "$tmp/mock.py" "$port" & pid=$!

  local base="http://127.0.0.1:$port"
  local up=0
  for _ in $(seq 1 50); do
    if curl -s -m 1 "$base/__state" > /dev/null 2>&1; then up=1; break; fi
    sleep 0.1
  done
  if [ "$up" -ne 1 ]; then
    bad "the mock never came up on $port -- nothing below would mean anything"
    return "$VOID"
  fi
  ok "mock up on $port"

  say "THE ARM THIS SCRIPT EXISTS FOR: GET answers 200 having PUT nothing"
  local code body
  code=$(curl -s -o "$tmp/g1" -w '%{http_code}' -m 5 "$base/v2.1/p/os-quota-sets/proj-x")
  body=$(cat "$tmp/g1")
  if [ "$code" = "200" ] && printf '%s' "$body" | grep -q '"quota_set"'; then
    ok "GET on a never-written tenant -> 200 with a quota_set (Nova's shape)"; pass=$((pass+1))
  else
    bad "GET returned $code / $body -- the mock does NOT reproduce Nova, so any"
    bad "  verdict from it would certify the bug as fixed"; failn=$((failn+1))
  fi
  if printf '%s' "$body" | grep -q '"id"'; then
    ok "the body carries \`id\`, as real Nova 2026.1 sends (not tenant_id)"; pass=$((pass+1))
  else
    bad "no \`id\` in the body -- diverges from the measured Nova shape"; failn=$((failn+1))
  fi

  say "a PUT is recorded, and changes what a later GET returns"
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -X PUT \
    -H 'Content-Type: application/json' \
    -d '{"quota_set":{"instances":42}}' "$base/v2.1/p/os-quota-sets/proj-x")
  # if/else, not `A && B || C`: that form runs C when B fails too, and the
  # arithmetic increment in B is a command with a status. shellcheck's SC2015.
  if [ "$code" = "200" ]; then
    ok "PUT -> 200"; pass=$((pass+1))
  else
    bad "PUT -> $code"; failn=$((failn+1))
  fi
  # Captured, not piped: a pipefail race here would print "writes are invisible
  # to this probe" about a working mock. Fails safe, fixed anyway.
  qbody=$(curl -s -m 5 "$base/v2.1/p/os-quota-sets/proj-x")
  if printf '%s' "$qbody" | grep -q '"instances": 42'; then
    ok "the PUT took effect, so a write is distinguishable from a default"; pass=$((pass+1))
  else
    bad "the PUT did not change the GET -- writes are invisible to this probe"; failn=$((failn+1))
  fi

  say "the state endpoint counts what the probe reads its verdict from"
  local st
  st=$(curl -s -m 5 "$base/__state")
  if printf '%s' "$st" | grep -q '"puts": 1' && printf '%s' "$st" | grep -q '"gets": 2'; then
    ok "counts are exact: gets=2 puts=1"; pass=$((pass+1))
  else
    bad "counts wrong: $st"; failn=$((failn+1))
  fi

  say "NEGATIVE CONTROL: an unprefixed path must 404"
  # If the mock answered /v2.1/os-quota-sets/... too, it would not be testing
  # the chart's required project segment and a misconfigured URL would pass.
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 5 "$base/v2.1/os-quota-sets/proj-x")
  if [ "$code" = "404" ]; then
    ok "project-less path -> 404"; pass=$((pass+1))
  else
    bad "project-less path -> $code, so the mock is laxer than the chart"
    failn=$((failn+1))
  fi

  printf '\n  %s passed, %s failed\n' "$pass" "$failn"
  [ "$failn" -eq 0 ] || return "$FAIL"
  printf '  The mock reproduces the behaviour that hid this defect, so a verdict\n'
  printf '  read from it means something.\n'
  return "$OK"
}

# ---------------------------------------------------------------------------
# --self-test: the verdict table, no mock
# ---------------------------------------------------------------------------
selftest() {
  local pass=0 failn=0
  arm() { # arm <label> <gets> <puts> <want>
    local out rc
    # `out=$(...)` IS a simple assignment, so under `set -e` a non-zero verdict
    # exits the script -- which killed this suite on its first FAIL arm, i.e. on
    # the arm the probe exists for. An `if` makes the failure a value again.
    if out=$(verdict "$2" "$3" 2>&1); then rc=0; else rc=$?; fi
    if [ "$rc" = "$4" ]; then
      printf '    %-46s PASS (rc=%s)\n' "$1" "$rc"; pass=$((pass+1))
    else
      printf '    %-46s FAIL (want %s got %s)\n' "$1" "$4" "$rc"; failn=$((failn+1))
      printf '%s\n' "$out" | sed 's/^/        /'
    fi
  }
  echo "hack/probe-quota-update.sh --self-test"
  echo
  echo "  the verdict table"
  arm "gets>0 puts>0 -> OK"                 3 1 "$OK"
  # THE ARM THE SCRIPT EXISTS FOR. If this returned OK, the probe would agree
  # with the create-only shape it was written to catch.
  arm "gets>0 puts=0 -> FAIL (the defect)"  3 0 "$FAIL"
  arm "gets=0 puts=0 -> VOID (unmeasured)"  0 0 "$VOID"
  # A PUT with no GET cannot happen through the controller, and reporting it as
  # OK would hide a probe reading someone else's traffic.
  arm "gets=0 puts>0 -> VOID, not OK"       0 2 "$VOID"

  echo
  echo "  the three verdicts must not share a message"
  local m1 m2 m3
  # `|| true` on each, for the same reason as the arm above: two of these three
  # verdicts are non-zero BY DESIGN, and under `set -e` a bare assignment from
  # one of them exits the suite. Second instance of this in one function, so it
  # is spelled out rather than fixed silently.
  m1=$(verdict 3 1 2>&1 | head -1) || true
  m2=$(verdict 3 0 2>&1 | head -1) || true
  m3=$(verdict 0 0 2>&1 | head -1) || true
  if [ "$m1" != "$m2" ] && [ "$m2" != "$m3" ] && [ "$m1" != "$m3" ]; then
    printf '    %-46s PASS\n' "three distinct first lines"; pass=$((pass+1))
  else
    printf '    %-46s FAIL\n' "three distinct first lines"; failn=$((failn+1))
  fi
  # And FAIL must name the mechanism, or a reader learns only that it failed.
  # THIS SCRIPT SETS `pipefail`, so `verdict | grep` returns verdict's
  # DELIBERATE non-zero even when grep matches -- and the arm reported that its
  # own FAIL message was missing while the message was right there. The first
  # comment I wrote here said "a pipeline's status is its last command", which
  # is true WITHOUT pipefail and false in this file. Capture first, then match.
  local failmsg
  failmsg=$(verdict 3 0 2>&1) || true
  if printf '%s' "$failmsg" | grep -q "no \`update\` verb"; then
    printf '    %-46s PASS\n' "FAIL names the missing update verb"; pass=$((pass+1))
  else
    printf '    %-46s FAIL\n' "FAIL names the missing update verb"; failn=$((failn+1))
  fi

  printf '\n  %s passed, %s failed\n' "$pass" "$failn"
  [ "$failn" -eq 0 ] || return 1
  return 0
}

case "${1:-}" in
  --validate-mock) validate_mock ;;
  --self-test|--selftest) selftest ;;
  *)
    cat >&2 <<'USAGE'
usage: probe-quota-update.sh --validate-mock | --self-test

  --validate-mock  prove the mock reproduces Nova's always-200 GET before any
                   verdict is read from it. No cluster.
  --self-test      drive the verdict table. No mock, no cluster.

A real run against a cluster is deliberately NOT a mode here yet: it needs a
kind cluster and the load gate, and the two modes above are what make such a run
readable when it happens.
USAGE
    exit 2 ;;
esac
