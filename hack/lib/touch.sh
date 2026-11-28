# shellcheck shell=bash
# touch_restdefinitions -- force a reconcile of RestDefinitions with an annotation,
# and REPORT HOW MANY actually took.
#
# WHY THIS EXISTS. Two scripts wrote the key `probe-rbac//touch` and
# `bearer-ctl//touch`. A double slash is an empty middle segment, which is not a
# valid Kubernetes annotation key, so BOTH annotates failed on every run -- and
# one silenced stderr while the next line printed "touched N RestDefinition(s)"
# unconditionally. A silenced failure under an unconditional success message. The
# 300s wait was therefore doing the whole job alone, which is a fact worth knowing
# before anyone shortens it.
#
#   source hack/lib/touch.sh
#   valid_annotation_key <key>                      rc 0 valid, 1 not
#   touch_restdefinitions <runner> <ns> <key> <rd>...
#       <runner> is a command/function taking kubectl's arguments (the script's K).
#       Sets TOUCHED (successes) and TOUCH_ERR (last stderr). Returns 0 only when
#       every annotate succeeded. A key that fails validation touches NOTHING and
#       says why, rather than letting the API server's refusal be discarded.
#
# Key grammar (apimachinery validation): optional DNS-subdomain prefix (<=253),
# a slash, then a name of <=63 chars matching [A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?.
valid_annotation_key() {
  local key="$1" prefix="" name="$1"
  [ -n "$key" ] || return 1
  case "$key" in
    */*/*) return 1 ;;
    */*) prefix="${key%%/*}"; name="${key#*/}" ;;
  esac
  [ -n "$name" ] && [ "${#name}" -le 63 ] || return 1
  printf '%s' "$name" | grep -Eq '^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$' || return 1
  case "$key" in
    */*)
      [ -n "$prefix" ] && [ "${#prefix}" -le 253 ] || return 1
      printf '%s' "$prefix" | grep -Eq '^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$' || return 1 ;;
  esac
  return 0
}

touch_restdefinitions() {
  local runner="$1" ns="$2" key="$3" rd err
  shift 3
  # shellcheck disable=SC2034  # read by the sourcing script
  TOUCHED=0; TOUCH_ERR=""
  if ! valid_annotation_key "$key"; then
    # shellcheck disable=SC2034  # read by the sourcing script
    TOUCH_ERR="annotation key '$key' is not a valid Kubernetes key, so nothing was touched"
    return 1
  fi
  for rd in "$@"; do
    if err=$("$runner" -n "$ns" annotate restdefinitions.ogen.krateo.io "$rd" "$key=$(date +%s)" --overwrite 2>&1 >/dev/null); then
      TOUCHED=$((TOUCHED+1))
    else
      # shellcheck disable=SC2034  # read by the sourcing script
      TOUCH_ERR="$err"
    fi
  done
  [ "$TOUCHED" -eq "$#" ]
}
