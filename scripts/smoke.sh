#!/bin/sh
# Wave V18 / lane L5 — scripts/smoke.sh: ONE-COMMAND, GET-ONLY acceptance for a running stack.
#
# WHY THIS EXISTS: the release checklist was a list of curl commands and eyeballs.  This script is
# the same acceptance as a single command: it proves the stack answers, that the key gate really
# refuses and really admits, that the capability manifest is the registry-derived sorted set, that
# the phone surface is honest (real store, or a NAMED refusal — never a raw 500, never invented
# rows), that the key-only codes surface answers a COUNT without ever echoing a code, that the
# optional host send bridge is either healthy or named OFF, that docs visibility matches
# `SANDBOX_MODE`, that the proxy does not disclose its version, and that backups exist.  It never
# writes: every request is GET or HEAD.
#
# USAGE
#   scripts/smoke.sh                                  # base http://127.0.0.1, key from $API_KEY
#   scripts/smoke.sh --base http://127.0.0.1          # explicit base (nginx entry point)
#   scripts/smoke.sh --api-base http://127.0.0.1:8000 # check the API's own /docs directly
#   scripts/smoke.sh --key-from-env MY_KEY            # read the key from another env var
#   scripts/smoke.sh --dry-run                        # print the plan; no request is made
#
# EXIT CODES
#   0  every check passed (WARN and SKIP lines allowed — they are named)
#   1  at least one check FAILED (each FAIL line says what and why)
#   2  usage refusal (bad/missing argument, no base) — nothing was requested
#
# ENV (never printed; never read from .env)
#   API_KEY (or the --key-from-env name)   the owner API key the protected checks present
#   PHONE_SEND_BRIDGE_URL                  optional host send bridge (e.g. http://127.0.0.1:9227);
#                                          unset = bridge OFF (the default) -> the check is a NAMED SKIP
#   PHONE_SEND_BRIDGE_TOKEN                bridge token (>=24 chars); presence is reported, never the value
#   POLSIA_ROOT                            repo root for .env + backups/ (default: script's parent)
#   TMPDIR                                 temp dir for response bodies
#
# NO jq: JSON is parsed with python3 (or `python`) when present; otherwise the structural checks
# fall back to grep and print one named SKIP json-tool line.  NO container commands, NO traps,
# NO writes.
set -u

BASE=http://127.0.0.1
API_BASE=""
KEY_VAR=API_KEY
ROOT=${POLSIA_ROOT:-}
TIMEOUT=8
DRY_RUN=0

usage() {
    printf 'usage: smoke.sh [--base URL] [--api-base URL] [--key-from-env NAME] [--root DIR]\n'
    printf '                [--timeout SECONDS] [--dry-run] [--help]\n'
    printf '  GET/HEAD only.  Exit 0 = all checks pass, 1 = a check failed, 2 = usage refusal.\n'
    printf '  The API key is read from the environment ($API_KEY by default) and never printed.\n'
}

refuse() {
    printf 'smoke: REFUSED: %s\n' "$*"
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --base)
            [ $# -ge 2 ] || refuse "--base needs a URL"
            BASE=$2
            shift 2
            ;;
        --base=*)
            BASE=${1#--base=}
            shift
            ;;
        --api-base)
            [ $# -ge 2 ] || refuse "--api-base needs a URL"
            API_BASE=$2
            shift 2
            ;;
        --api-base=*)
            API_BASE=${1#--api-base=}
            shift
            ;;
        --key-from-env)
            [ $# -ge 2 ] || refuse "--key-from-env needs an environment variable NAME"
            KEY_VAR=$2
            shift 2
            ;;
        --key-from-env=*)
            KEY_VAR=${1#--key-from-env=}
            shift
            ;;
        --root)
            [ $# -ge 2 ] || refuse "--root needs a directory"
            ROOT=$2
            shift 2
            ;;
        --root=*)
            ROOT=${1#--root=}
            shift
            ;;
        --timeout)
            [ $# -ge 2 ] || refuse "--timeout needs a number of seconds"
            TIMEOUT=$2
            shift 2
            ;;
        --timeout=*)
            TIMEOUT=${1#--timeout=}
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf 'smoke: REFUSED: unknown argument "%s"\n' "$1"
            usage
            exit 2
            ;;
    esac
done

case "$TIMEOUT" in
    ''|*[!0-9]*) refuse "--timeout must be a whole number of seconds" ;;
esac

BASE=${BASE%/}
[ -n "$BASE" ] || refuse "--base must not be empty"
case "$BASE" in
    http://*|https://*) : ;;
    *) refuse "--base must start with http:// or https:// (got \"$BASE\")" ;;
esac
if [ -n "$API_BASE" ]; then
    API_BASE=${API_BASE%/}
    case "$API_BASE" in
        http://*|https://*) : ;;
        *) refuse "--api-base must start with http:// or https:// (got \"$API_BASE\")" ;;
    esac
fi

# The key comes from the ENVIRONMENT only.  A `.env` value is deliberately NOT read: a smoke that
# silently picked up the deployment's key would "pass" against a key the operator never supplied.
case "$KEY_VAR" in
    ''|*[!A-Za-z0-9_]*) refuse "--key-from-env must be a plain environment-variable name" ;;
esac
eval "KEY=\${$KEY_VAR:-}"

# The optional host send bridge (V19).  UNSET means the bridge is OFF — the default, and the
# direct subprocess path is in use.  The URL/token are read from the environment only and never
# printed; an unset bridge is a NAMED SKIP, never a silent pass.
BRIDGE_URL=${PHONE_SEND_BRIDGE_URL:-}
BRIDGE_TOKEN=${PHONE_SEND_BRIDGE_TOKEN:-}

if [ -z "$ROOT" ]; then
    SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
    ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
else
    [ -d "$ROOT" ] || refuse "--root \"$ROOT\" is not a directory"
    ROOT=$(cd "$ROOT" && pwd)
fi
# Mixed `C:/...` form so the printed paths (and any native tool) agree with the shell's view.
if command -v cygpath >/dev/null 2>&1; then
    ROOT=$(cygpath -m "$ROOT")
fi

PY=""
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
fi

# ── the plan (shared by --dry-run and the real run) ──────────────────────────────────────────
print_plan() {
    printf '  %-2s %-15s %-5s %s\n' '#' 'check' 'verb' 'request -> expectation'
    printf '  %-2s %-15s %-5s %s\n' 1 health GET "$BASE/api/v1/health -> 200 {\"status\":\"ok\"}"
    printf '  %-2s %-15s %-5s %s\n' 2 headers GET "$BASE/ -> CSP + X-Frame-Options + nosniff"
    printf '  %-2s %-15s %-5s %s\n' 3 auth-401 GET "$BASE/api/v1/notifications without a key -> 401 E_AUTH"
    printf '  %-2s %-15s %-5s %s\n' 4 auth-200 GET "$BASE/api/v1/notifications with a key -> 200 envelope"
    printf '  %-2s %-15s %-5s %s\n' 5 capabilities GET "$BASE/api/v1/ops/capabilities -> agents>0, sorted"
    printf '  %-2s %-15s %-5s %s\n' 6 phone-summary GET "$BASE/api/v1/phone/summary -> store_status=ok or named 503"
    printf '  %-2s %-15s %-5s %s\n' 7 phone-media GET "$BASE/api/v1/phone/media?thread_id=<first summary thread> -> parts[] or named refusal"
    printf '  %-2s %-15s %-5s %s\n' 8 phone-codes GET "$BASE/api/v1/phone/codes?limit=1 -> codes[N] (count only; values never printed)"
    printf '  %-2s %-15s %-5s %s\n' 9 docs GET "${API_BASE:-$BASE}/docs -> matches SANDBOX_MODE in $ROOT/.env"
    printf '  %-2s %-15s %-5s %s\n' 10 server-header GET "$BASE/ -> Server absent or version-masked"
    printf '  %-2s %-15s %-5s %s\n' 11 send-bridge GET "<\$PHONE_SEND_BRIDGE_URL>/health -> 200 {status:ok}; unset = named SKIP"
    printf '  %-2s %-15s %-5s %s\n' 12 backups LOCAL "$ROOT/backups/polsia-*.sql + 4 volume archives (warn-only)"
}

if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRY RUN — plan only: no network call is made and nothing is written outside a temp dir.\n'
    printf 'base=%s\n' "$BASE"
    printf 'api-base=%s\n' "${API_BASE:-$BASE (docs is checked at the base)}"
    printf 'root=%s\n' "$ROOT"
    if [ -n "$KEY" ]; then
        printf 'key: $%s is present (value never printed)\n' "$KEY_VAR"
    else
        printf 'key: $%s is absent — the protected checks will FAIL and say so\n' "$KEY_VAR"
    fi
    if [ -n "$BRIDGE_URL" ]; then
        printf 'bridge: $PHONE_SEND_BRIDGE_URL is set (%s; health is checked at run time, value never printed)\n' \
            "$([ -n "$BRIDGE_TOKEN" ] && echo 'token present' || echo 'token ABSENT — /send will refuse by name')"
    else
        printf 'bridge: $PHONE_SEND_BRIDGE_URL is not set — send-bridge will SKIP (bridge OFF, the default)\n'
    fi
    print_plan
    printf 'DRY RUN complete: 12 checks planned, 0 requests made.\n'
    exit 0
fi

WORK=$(mktemp -d "${TMPDIR:-/tmp}/polsia-smoke.XXXXXX") || refuse "cannot create a temp work dir"
# The documented Windows host ships a mingw curl that CANNOT open an MSYS `/tmp/...` path (measured:
# `curl: (23) client returned ERROR on write`).  The mixed `C:/...` form is understood by both curl
# and the POSIX tools, so convert when cygpath exists; on Linux this is a no-op.
if command -v cygpath >/dev/null 2>&1; then
    WORK=$(cygpath -m "$WORK")
fi
CHECKS=0
FAILS=0
WARNS=0

pass() { CHECKS=$((CHECKS + 1)); printf 'PASS %s: %s\n' "$1" "$2"; }
fail() { CHECKS=$((CHECKS + 1)); FAILS=$((FAILS + 1)); printf 'FAIL %s: %s\n' "$1" "$2"; }
skip() { CHECKS=$((CHECKS + 1)); printf 'SKIP %s: %s\n' "$1" "$2"; }
warn() { WARNS=$((WARNS + 1)); printf 'WARN %s: %s\n' "$1" "$2"; }

short() { head -c 180 "$1" 2>/dev/null | tr -d '\r' | tr '\n' ' '; }

# curl wrappers: status on stdout, body to a file, errors to a file (never mixed into stdout).
get_code() {
    _url=$1
    _out=$2
    shift 2
    curl -sS --max-time "$TIMEOUT" -o "$_out" -w '%{http_code}' "$@" "$_url" 2>"$WORK/curl.err" || true
}
head_code() {
    _url=$1
    _hdr=$2
    shift 2
    curl -sS --max-time "$TIMEOUT" -D "$_hdr" -o /dev/null -w '%{http_code}' "$@" "$_url" 2>"$WORK/curl.err" || true
}
hdr_value() {
    grep -i "^$2:" "$1" 2>/dev/null | tail -n 1 | cut -d: -f2- | tr -d '\r' | sed 's/^ *//; s/ *$//'
}
has() { grep -q "$1" "$2" 2>/dev/null; }

# JSON value by python expression over `d`.  The document arrives on STDIN (never as a path: a
# native Windows python.exe cannot open an MSYS `/tmp/...` path), and the expression is argv[1].
# Prints __ERR__ when python or the document is unusable.
json_get() {
    [ -f "$1" ] || { printf '__ERR__'; return 0; }
    "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
    ns = {"__builtins__": {}, "d": d, "len": len, "sorted": sorted, "str": str, "int": int,
          "list": list, "isinstance": isinstance, "bool": bool, "all": all, "any": any,
          "dict": dict}
    # ONE namespace for globals AND locals: a generator expression resolves its free names from
    # globals, so a separate locals mapping made `isinstance` invisible inside `all(...)`.
    v = eval(sys.argv[1], ns)
except Exception:
    print("__ERR__")
else:
    print("" if v is None else v)
' "$2" < "$1" 2>/dev/null || true
}

if [ -n "$PY" ]; then
    printf 'INFO json-tool: %s (structural checks parsed as JSON)\n' "$PY"
else
    printf 'SKIP json-tool: no python3/python on PATH — structural checks use grep (named, less strict)\n'
fi
printf 'INFO base=%s root=%s key=$%s(%s)\n' "$BASE" "$ROOT" "$KEY_VAR" \
    "$([ -n "$KEY" ] && echo present || echo absent)"
printf '\n'

# ── 1. health ───────────────────────────────────────────────────────────────────────────────
code=$(get_code "$BASE/api/v1/health" "$WORK/health.json")
if [ -f "$WORK/health.json" ]; then
    compact=$(tr -d ' \t\r\n' < "$WORK/health.json")
else
    compact=""
fi
if [ "$code" = 200 ] && [ "$compact" = '{"status":"ok"}' ]; then
    pass health "GET /api/v1/health -> 200 {\"status\":\"ok\"}"
else
    fail health "GET /api/v1/health -> ${code:-000}, body: $(short "$WORK/health.json")"
fi

# ── 2. security headers on / ────────────────────────────────────────────────────────────────
code=$(head_code "$BASE/" "$WORK/root.hdr")
if [ "${code:-000}" = 000 ]; then
    fail headers "GET $BASE/ -> no response (curl: $(short "$WORK/curl.err"))"
else
    csp=$(hdr_value "$WORK/root.hdr" 'Content-Security-Policy')
    xfo=$(hdr_value "$WORK/root.hdr" 'X-Frame-Options')
    nosniff=$(hdr_value "$WORK/root.hdr" 'X-Content-Type-Options')
    missing=""
    case "$csp" in *default-src*) : ;; *) missing="$missing CSP" ;; esac
    case "$xfo" in SAMEORIGIN|DENY) : ;; *) missing="$missing X-Frame-Options" ;; esac
    case "$nosniff" in *nosniff*) : ;; *) missing="$missing nosniff" ;; esac
    if [ -z "$missing" ]; then
        pass headers "status=$code CSP=yes frame=$xfo nosniff=yes"
    else
        fail headers "missing:$missing (status=$code)"
    fi
fi

# ── 3. the key gate refuses an unauthenticated caller, by name ──────────────────────────────
code=$(get_code "$BASE/api/v1/notifications" "$WORK/noauth.json")
if [ "$code" = 401 ] && has '"E_AUTH"' "$WORK/noauth.json"; then
    pass auth-401 "GET /api/v1/notifications without a key -> 401 E_AUTH"
else
    fail auth-401 "expected 401 E_AUTH, got ${code:-000}: $(short "$WORK/noauth.json")"
fi

# ── 4. ...and admits the owner's key ────────────────────────────────────────────────────────
if [ -z "$KEY" ]; then
    fail auth-200 "no key in \$$KEY_VAR — export it (never on the command line) and re-run"
else
    code=$(get_code "$BASE/api/v1/notifications" "$WORK/auth.json" -H "X-API-Key: $KEY")
    if [ "$code" = 200 ] && has '"items"' "$WORK/auth.json"; then
        if [ -n "$PY" ]; then
            n=$(json_get "$WORK/auth.json" 'len(d.get("items") or [])')
        else
            n=$(grep -o '"id"' "$WORK/auth.json" 2>/dev/null | wc -l | tr -d ' ')
        fi
        pass auth-200 "GET /api/v1/notifications with the key -> 200 envelope (items=${n:-?})"
    else
        fail auth-200 "expected 200 with items[], got ${code:-000}: $(short "$WORK/auth.json")"
    fi
fi

# ── 5. capabilities: real, non-empty, sorted ────────────────────────────────────────────────
if [ -z "$KEY" ]; then
    fail capabilities "no key in \$$KEY_VAR"
else
    code=$(get_code "$BASE/api/v1/ops/capabilities" "$WORK/caps.json" -H "X-API-Key: $KEY")
    if [ "$code" != 200 ]; then
        fail capabilities "expected 200, got ${code:-000}: $(short "$WORK/caps.json")"
    elif [ -n "$PY" ]; then
        detail=$(json_get "$WORK/caps.json" '"agents=%d sorted=%s registry_tools=%d" % (len(d.get("agents") or []), str([a["agent_type"] for a in d.get("agents") or []] == sorted(a["agent_type"] for a in d.get("agents") or [])), len(d.get("registry_tools") or []))')
        case "$detail" in
            __ERR__*) fail capabilities "unparseable capabilities body: $(short "$WORK/caps.json")" ;;
            "agents=0"*) fail capabilities "agents list is empty — a manifest with no agents is not a capability surface" ;;
            *"sorted=False"*) fail capabilities "$detail (agent_type is not sorted)" ;;
            *) pass capabilities "$detail" ;;
        esac
    else
        n=$(grep -o '"agent_type"' "$WORK/caps.json" 2>/dev/null | wc -l | tr -d ' ')
        types=$(grep -o '"agent_type":"[^"]*"' "$WORK/caps.json" 2>/dev/null | sed 's/.*:"//; s/"$//')
        if [ "${n:-0}" -eq 0 ]; then
            fail capabilities "agents list is empty"
        elif printf '%s\n' "$types" | sort -c 2>/dev/null; then
            pass capabilities "agents=$n sorted=yes (grep-based)"
        else
            fail capabilities "agents=$n sorted=no (grep-based)"
        fi
    fi
fi

# ── 6. phone summary: real store, or a NAMED refusal (never a raw 500) ─────────────────────
THREAD_ID=""
if [ -z "$KEY" ]; then
    fail phone-summary "no key in \$$KEY_VAR"
else
    code=$(get_code "$BASE/api/v1/phone/summary" "$WORK/phone.json" -H "X-API-Key: $KEY")
    case "$code" in
        200)
            if [ -n "$PY" ]; then
                status=$(json_get "$WORK/phone.json" 'd.get("data",{}).get("store_status","")')
                unread=$(json_get "$WORK/phone.json" 'd.get("data",{}).get("unread_total","")')
                nthr=$(json_get "$WORK/phone.json" 'len(d.get("data",{}).get("threads") or [])')
                THREAD_ID=$(json_get "$WORK/phone.json" 'str((d.get("data",{}).get("threads") or [{}])[0].get("thread_id",""))')
            else
                status=$(has '"store_status":"ok"' "$WORK/phone.json" && echo ok)
                unread="?"
                nthr="?"
                THREAD_ID=$(grep -o '"thread_id":[0-9]*' "$WORK/phone.json" 2>/dev/null | head -n 1 | cut -d: -f2)
            fi
            if [ "$status" = ok ]; then
                pass phone-summary "200 store_status=ok unread=${unread:-?} threads=${nthr:-?}"
            else
                fail phone-summary "200 but store_status=${status:-?} — the store is not readable"
            fi
            ;;
        503)
            if has '"E_PHONE_UNAVAILABLE"' "$WORK/phone.json"; then
                pass phone-summary "503 E_PHONE_UNAVAILABLE (named refusal — store not mounted)"
            else
                fail phone-summary "503 without a named code: $(short "$WORK/phone.json")"
            fi
            ;;
        500) fail phone-summary "raw 500 — the phone surface must refuse by name: $(short "$WORK/phone.json")" ;;
        *) fail phone-summary "expected 200 or named 503, got ${code:-000}: $(short "$WORK/phone.json")" ;;
    esac
fi

# ── 7. phone media: frozen shape, or a named refusal ───────────────────────────────────────
case "$THREAD_ID" in ''|None) THREAD_ID="" ;; esac
if [ -z "$KEY" ]; then
    skip phone-media "no key in \$$KEY_VAR — the shape check needs the owner's key"
elif [ -z "$THREAD_ID" ]; then
    skip phone-media "no thread in /phone/summary — nothing to ask media for"
else
    code=$(get_code "$BASE/api/v1/phone/media?thread_id=$THREAD_ID" "$WORK/media.json" -H "X-API-Key: $KEY")
    case "$code" in
        200)
            if [ -n "$PY" ]; then
                parts=$(json_get "$WORK/media.json" 'len(d.get("data",{}).get("parts") or [])')
                trunc=$(json_get "$WORK/media.json" 'd.get("data",{}).get("truncated","")')
                if [ "${parts:-__ERR__}" = "__ERR__" ]; then
                    fail phone-media "200 but no data.parts[]: $(short "$WORK/media.json")"
                else
                    pass phone-media "200 parts=$parts truncated=${trunc:-false} thread=$THREAD_ID"
                fi
            elif has '"parts"' "$WORK/media.json"; then
                pass phone-media "200 data.parts[] present (grep-based) thread=$THREAD_ID"
            else
                fail phone-media "200 but no data.parts[]: $(short "$WORK/media.json")"
            fi
            ;;
        404)
            if has '"E_NOT_FOUND"' "$WORK/media.json" || has '"E_PHONE_UNAVAILABLE"' "$WORK/media.json"; then
                pass phone-media "404 named refusal: $(short "$WORK/media.json")"
            else
                fail phone-media "plain 404 — the media route is absent from this build (no E_* code)"
            fi
            ;;
        503)
            if has '"E_PHONE_UNAVAILABLE"' "$WORK/media.json"; then
                pass phone-media "503 E_PHONE_UNAVAILABLE (named refusal — store not mounted)"
            else
                fail phone-media "503 without a named code: $(short "$WORK/media.json")"
            fi
            ;;
        500) fail phone-media "raw 500 — media must refuse by name: $(short "$WORK/media.json")" ;;
        *) fail phone-media "expected 200 or named refusal, got ${code:-000}: $(short "$WORK/media.json")" ;;
    esac
fi

# ── 8. phone codes: key-only, count reported, values NEVER printed ──────────────────────────
if [ -z "$KEY" ]; then
    skip phone-codes "no key in \$$KEY_VAR — the codes route is key-only (auth-200 already names the missing key)"
else
    code=$(get_code "$BASE/api/v1/phone/codes?limit=1" "$WORK/codes.json" -H "X-API-Key: $KEY")
    case "$code" in
        200)
            if [ -n "$PY" ]; then
                detail=$(json_get "$WORK/codes.json" '(lambda x: "codes=%d shape=%s" % (len(x) if isinstance(x, list) else -1, str(isinstance(x, list) and all(isinstance(r, dict) and "when" in r and "code" in r for r in x))))((d.get("data") or {}).get("codes"))')
                case "$detail" in
                    __ERR__*) fail phone-codes "200 but no data.codes[] — body withheld (it may contain code values)" ;;
                    *"shape=False"*) fail phone-codes "$detail (a code row must carry when+code; values never printed)" ;;
                    *) pass phone-codes "GET /phone/codes?limit=1 -> 200 $detail (values never printed)" ;;
                esac
            elif has '"codes"' "$WORK/codes.json"; then
                pass phone-codes "200 data.codes[] present (grep-based; values never printed)"
            else
                fail phone-codes "200 but no data.codes[] — body withheld (it may contain code values)"
            fi
            ;;
        401)
            if has '"E_AUTH"' "$WORK/codes.json"; then
                pass phone-codes "401 E_AUTH (named refusal — the supplied key was rejected; auth-200 names it)"
            else
                fail phone-codes "401 without E_AUTH — body withheld (it may contain code values)"
            fi
            ;;
        503)
            if has '"E_PHONE_UNAVAILABLE"' "$WORK/codes.json"; then
                pass phone-codes "503 E_PHONE_UNAVAILABLE (named refusal — store not mounted)"
            else
                fail phone-codes "503 without a named code — body withheld (it may contain code values)"
            fi
            ;;
        500) fail phone-codes "raw 500 — codes must refuse by name (body withheld)" ;;
        *) fail phone-codes "expected 200 or a named refusal, got ${code:-000} (body withheld)" ;;
    esac
fi

# ── 9. docs visibility matches SANDBOX_MODE ─────────────────────────────────────────────────
SANDBOX=""
if [ -f "$ROOT/.env" ]; then
    SANDBOX=$(grep -E '^[[:space:]]*SANDBOX_MODE=' "$ROOT/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d '\r' | tr -d '"' | tr 'A-Z' 'a-z')
fi
if [ -z "$SANDBOX" ]; then
    skip docs "no SANDBOX_MODE in $ROOT/.env — nothing to compare /docs against"
else
    case "$SANDBOX" in
        true|1|yes|on) want=200 ;;
        false|0|no|off) want=404 ;;
        *) want="" ;;
    esac
    if [ -z "$want" ]; then
        skip docs "SANDBOX_MODE=\"$SANDBOX\" is not a boolean — cannot compare"
    else
        if [ -n "$API_BASE" ]; then
            docs_url="$API_BASE/docs"
            proxy_frontend=0
        else
            docs_url="$BASE/docs"
            proxy_frontend=0
            if [ "$SANDBOX" = true ] && [ "$(hdr_value "$WORK/root.hdr" 'X-Powered-By')" = Next.js ]; then
                proxy_frontend=1
            fi
        fi
        code=$(get_code "$docs_url" "$WORK/docs.json")
        if [ "$code" = "$want" ]; then
            pass docs "SANDBOX_MODE=$SANDBOX -> GET /docs = $code (expected $want)"
        elif [ "$proxy_frontend" = 1 ] && [ "${code:-000}" = 404 ]; then
            skip docs "$BASE fronts the Next app at /docs (docs is not proxied to the API); pass --api-base http://127.0.0.1:8000 to check the API itself"
        else
            fail docs "SANDBOX_MODE=$SANDBOX expects /docs = $want, got ${code:-000}: $(short "$WORK/docs.json")"
        fi
    fi
fi

# ── 10. the proxy must not disclose its version ─────────────────────────────────────────────
code=$(head_code "$BASE/" "$WORK/server.hdr")
server=$(hdr_value "$WORK/server.hdr" 'Server')
if [ "${code:-000}" = 000 ]; then
    fail server-header "GET $BASE/ -> no response"
elif [ -z "$server" ]; then
    pass server-header "no Server header on $BASE/ (nothing disclosed)"
else
    case "$server" in
        *uvicorn*|*Uvicorn*|*starlette*|*Starlette*|*python*|*Python*)
            fail server-header "Server: $server discloses the runtime stack"
            ;;
        *[0-9]*)
            fail server-header "Server: $server discloses a version — add 'server_tokens off;' at nginx/nginx.conf http{} scope and reload nginx"
            ;;
        *)
            pass server-header "Server: $server (masked, no version)"
            ;;
    esac
fi

# ── 11. send bridge (optional; default OFF): healthy when configured, named OFF otherwise ───
if [ -z "$BRIDGE_URL" ]; then
    skip send-bridge "PHONE_SEND_BRIDGE_URL is not set — the send bridge is OFF (the default); sends use the direct subprocess path"
else
    BRIDGE_URL=${BRIDGE_URL%/}
    case "$BRIDGE_URL" in
        http://*|https://*) BRIDGE_SCHEME_OK=1 ;;
        *) BRIDGE_SCHEME_OK=0 ;;
    esac
    if [ "$BRIDGE_SCHEME_OK" -eq 0 ]; then
        fail send-bridge "PHONE_SEND_BRIDGE_URL must start with http:// or https:// (the value is not printed)"
    else
        code=$(get_code "$BRIDGE_URL/health" "$WORK/bridge.json")
        tok=absent
        [ -n "$BRIDGE_TOKEN" ] && tok=present
        case "${code:-000}" in
            000)
                fail send-bridge "GET <PHONE_SEND_BRIDGE_URL>/health -> no response (curl: $(short "$WORK/curl.err"))"
                ;;
            200)
                if [ -n "$PY" ]; then
                    status=$(json_get "$WORK/bridge.json" 'd.get("status","")')
                elif grep -q '"status"[[:space:]]*:[[:space:]]*"ok"' "$WORK/bridge.json" 2>/dev/null; then
                    status=ok
                else
                    status=""
                fi
                case "$status" in
                    ok) pass send-bridge "GET <PHONE_SEND_BRIDGE_URL>/health -> 200 status=ok token=$tok" ;;
                    __ERR__*) fail send-bridge "200 but unparseable body: $(short "$WORK/bridge.json")" ;;
                    *) fail send-bridge "200 but status=${status:-?} (expected ok) token=$tok" ;;
                esac
                ;;
            *)
                fail send-bridge "GET <PHONE_SEND_BRIDGE_URL>/health -> ${code:-000}: $(short "$WORK/bridge.json")"
                ;;
        esac
    fi
fi

# ── 12. backup artifacts (warn-only: a fresh clone has none until `make backup`) ───────────
sql_count=$(ls -1 "$ROOT"/backups/polsia-*.sql 2>/dev/null | wc -l | tr -d ' ')
missing=""
for vol in backend_data chroma_data celery_beat_data redis_data; do
    [ -f "$ROOT/backups/$vol.tgz" ] || missing="$missing $vol.tgz"
done
if [ "${sql_count:-0}" -gt 0 ] && [ -z "$missing" ]; then
    pass backups "$sql_count database dump(s) + 4/4 volume archives under $ROOT/backups"
else
    detail=""
    [ "${sql_count:-0}" -gt 0 ] || detail="no polsia-*.sql"
    [ -z "$missing" ] || detail="$detail missing:$missing"
    warn backups "$detail (warn-only — run 'make backup' for a restorable artifact set)"
fi

# ── verdict ────────────────────────────────────────────────────────────────────────────────
printf '\n'
rm -rf "$WORK" 2>/dev/null
if [ "$FAILS" -eq 0 ]; then
    printf 'RESULT: PASS — %s checks passed, %s warning(s), base %s\n' "$CHECKS" "$WARNS" "$BASE"
    exit 0
fi
printf 'RESULT: FAIL — %s of %s checks failed (each FAIL line names it), %s warning(s)\n' \
    "$FAILS" "$CHECKS" "$WARNS"
exit 1
