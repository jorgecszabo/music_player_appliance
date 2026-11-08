#!/bin/bash
# Smoke test of a running speaker, from this workstation (TESTING.md section 13).
# Uses the web API (curl) and, if you give it, ssh. Installs nothing on the board.
#
#   scripts/smoke.sh 192.168.0.56                 API checks, including playing a radio station
#   SMOKE_SSH=panda@192.168.0.56 scripts/smoke.sh 192.168.0.56   also process, log and memory checks
#   scripts/smoke.sh HOST --quick                 skip everything that interrupts playback or restarts services
#   scripts/smoke.sh HOST --no-wifi-scan          skip the Wi-Fi scan (a scan can drop a board that is itself on Wi-Fi)
#   scripts/smoke.sh HOST --no-radio              skip the internet-radio check (the board has no internet)
#
# Prints PASS / FAIL / SKIP / INFO per check; exit status 1 if anything FAILed.
# It restarts the radio player and the audio stack unless --quick: use it on a speaker nobody is listening to.
# The "regression probes" send malformed requests; an old version answers them with errors (500, a
# dropped connection): a FAIL there means the board runs code from before those fixes.
set -uo pipefail

HOST="${1:-}"
[ -n "$HOST" ] && shift
[ -n "$HOST" ] || { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
QUICK="" NORADIO="" NOWIFI=""
for a in "$@"; do
    case "$a" in
        --quick) QUICK=1 ;;
        --no-radio) NORADIO=1 ;;
        --no-wifi-scan) NOWIFI=1 ;;
        *) echo "unknown option $a" >&2; exit 2 ;;
    esac
done
BASE="http://$HOST"
SSH_TARGET="${SMOKE_SSH:-}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new)
passed=0 failed=0 skipped=0

pass() { echo "PASS  $*"; passed=$((passed + 1)); }
fail() { echo "FAIL  $*"; failed=$((failed + 1)); }
skip() { echo "SKIP  $*"; skipped=$((skipped + 1)); }
info() { echo "INFO  $*"; }
section() { echo; echo "== $*"; }
check() { local what="$1"; shift; if "$@"; then pass "$what"; else fail "$what"; fi; }

get() { curl -sS -m "${2:-10}" "$BASE$1" 2>/dev/null; }
post() { curl -sS -m "${3:-15}" -o - -w '\n%{http_code}' -H 'Content-Type: application/json' -H 'X-Panda-Panel: 1' \
    -d "$2" "$BASE$1" 2>/dev/null; }
# post_code PATH BODY -> the HTTP status (000 if the connection failed)
post_code() { curl -sS -m 10 -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -H 'X-Panda-Panel: 1' \
    -d "$2" "$BASE$1" 2>/dev/null || echo 000; }
# status_py 'expression on s' -> exit 0 if it is true (s = /api/status as a dict)
status_py() {
    get /api/status | python3 -c '
import json, sys
try:
    s = json.load(sys.stdin)
    sys.exit(0 if eval(sys.argv[1]) else 1)
except Exception:
    sys.exit(2)' "$1"
}
status_val() {
    get /api/status | python3 -c '
import json, sys
try:
    s = json.load(sys.stdin)
    print(eval(sys.argv[1]))
except Exception as e:
    print("?")' "$1"
}
# wait_status 'expression' SECONDS
wait_status() {
    local end=$((SECONDS + $2))
    while [ $SECONDS -lt $end ]; do status_py "$1" && return 0; sleep 1; done
    return 1
}
remote() { ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "$@" 2>/dev/null; }

# ---------------------------------------------------------------------------------------------
section "reachability and version"
if ! get /api/status 8 | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null; then
    fail "the web page answers on $BASE"
    echo; echo "0 passed, 1 failed: $HOST is not answering"; exit 1
fi
pass "the web page answers on $BASE"
info "version $(status_val "s['ota']['running']['version']") on $(status_val "s['host']['board_name']"), kernel $(status_val "s['host']['kernel']"), up $(status_val "int(s['host']['uptime'])") s"
check "the new status format (restartable list) is there" status_py "'restartable' in s and 'speaker-radio' in s['restartable']"
check "the update slots are consistent (booted slot is the confirmed one, no trial pending)" \
    status_py "s['ota']['ok'] and s['ota']['booted'] == s['ota']['confirmed'] and not s['ota']['trial']"

# ---------------------------------------------------------------------------------------------
section "services"
bad=$(get /api/status | python3 -c '
import json, sys
s = json.load(sys.stdin)
print(" ".join(f"{k}={v}" for k, v in s["services"].items() if v not in ("started", "not in a runlevel")))')
if [ -z "$bad" ]; then pass "every service is started (none stopped or crashed)"; else fail "services not started: $bad"; fi
check "the audio sink exists" status_py "s['audio']['sink'] is not None"
check "the player (mpv) answers" status_py "s['radio']['available']"
errs=$(status_val "s['audio']['sink'].get('errors')")
info "xruns on the output so far: $errs"
mem_free=$(status_val "s['mem']['available'] * 100 // s['mem']['total']")
check "at least 20% of memory is available (${mem_free}%)" status_py "s['mem']['available'] * 5 > s['mem']['total']"
check "CPU temperature is sane ($(status_val "s['cpu'].get('temp')") C)" status_py "s['cpu'].get('temp') is None or s['cpu']['temp'] < 85"
check "the clock is synchronised" status_py "'Normal' in str(s['sync'].get('leap')) or s['sync'].get('reference')"

# ---------------------------------------------------------------------------------------------
LOGS="arbiter radio panda-web panda-bt panda-wifi pipewire wireplumber shairport"
# The logs outlive deploys and restarts, so old tracebacks stay in them: count now, compare at the end.
declare -A TB0
for name in $LOGS; do
    TB0[$name]=$(get "/api/logs?name=$name" 15 | grep -c Traceback)
    [ "${TB0[$name]}" -gt 0 ] && info "log $name already has ${TB0[$name]} traceback(s) from before this run"
done

# ---------------------------------------------------------------------------------------------
section "regression probes (malformed requests; old versions answer with 500 or drop the connection)"
check "service name that is a list -> 400" test "$(post_code /api/service '{"name": ["speaker-radio"]}')" = 400
check "volume id that is a list -> 400" test "$(post_code /api/volume '{"id": [1]}')" = 400
check "speaker name null -> 400" test "$(post_code /api/name '{"name": null}')" = 400
check "audio value 1e999 -> 400" test "$(post_code /api/audio '{"bass": 1e999, "treble": 0, "cap": 1, "usb_cap": 0, "output": "onboard"}')" = 400
check "a station search with page=² -> 200" test "$(curl -sS -m 10 -o /dev/null -w '%{http_code}' "$BASE/api/radio/search?page=%C2%B2" 2>/dev/null || echo 000)" -ne 000
check "a request with Content-Length -1 is answered, not left hanging" python3 - "$HOST" <<'PY'
import socket, sys
s = socket.create_connection((sys.argv[1], 80), timeout=4)
s.sendall(b"POST /api/service HTTP/1.1\r\nHost: " + sys.argv[1].encode() + b"\r\nX-Panda-Panel: 1\r\n"
          b"Content-Type: application/json\r\nContent-Length: -1\r\n\r\n")
s.settimeout(4)
try:
    sys.exit(0 if s.recv(100).startswith(b"HTTP/1.") else 1)
except OSError:
    sys.exit(1)
PY
check "unknown service names are refused" test "$(post_code /api/service '{"name": "sshd"}')" = 400
check "a foreign Host header is refused (DNS rebinding)" test "$(curl -sS -m 8 -o /dev/null -w '%{http_code}' -H 'Host: evil.example' "$BASE/api/status" 2>/dev/null)" = 421

# ---------------------------------------------------------------------------------------------
section "Wi-Fi and Bluetooth"
if status_py "s['wifi'].get('adapters')"; then
    pass "a Wi-Fi adapter is present ($(status_val "s['wifi']['adapters'][0]['description']"))"
    if [ -n "$NOWIFI" ]; then nets=-1; else nets=$(get /api/wifi/scan 30 | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("networks", [])))' 2>/dev/null || echo 0); fi
    if [ "$nets" -lt 0 ]; then skip "Wi-Fi scan (--no-wifi-scan)"
    elif [ "${nets:-0}" -gt 0 ]; then pass "a Wi-Fi scan finds networks ($nets)"; else info "the Wi-Fi scan found no networks"; fi
else
    skip "no Wi-Fi adapter"
fi
if status_py "s['bt'].get('adapter')"; then
    pass "the Bluetooth controller is up ($(status_val "len(s['bt']['devices'])") known devices)"
    # BlueZ keeps calling a dead combo chip "UP", so ask the chip (panda-bt probes it every 30 s).
    check "the Bluetooth controller answers HCI commands" wait_status "s['bt'].get('alive') is True" 70
elif status_py "s['host'].get('bt_controller')"; then
    fail "Bluetooth: the board has a controller ($(status_val "s['host']['bt_controller']")) but panda-bt does not answer: $(status_val "s['bt'].get('error')")"
else
    skip "this board has no Bluetooth controller"
fi

# ---------------------------------------------------------------------------------------------
section "internet radio"
if [ -n "$NORADIO" ]; then
    skip "radio check skipped (--no-radio)"
else
    uuid=$(get "/api/radio/search?order=clickcount" 25 | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    for st in d["stations"]:
        if st.get("codec", "").upper() in ("MP3", "AAC", "AAC+") and not st.get("hls"):
            print(st["stationuuid"]); break
except Exception:
    pass' 2>/dev/null)
    if [ -z "$uuid" ]; then
        skip "radio-browser.info is not reachable from here/the board"
    else
        resp=$(post /api/radio "{\"cmd\": \"play\", \"uuid\": \"$uuid\"}" 30)
        code=${resp##*$'\n'}
        if [ "$code" != 200 ]; then
            fail "play a station ($code: ${resp%$'\n'*})"
        else
            pass "play a station request accepted"
            check "the stream starts (status shows it playing with a codec)" wait_status "s['radio'].get('playing') and s['radio'].get('codec')" 45
            check "Radio is the only audible source" wait_status "[x['source'] for x in s['audio']['streams'] if x['state']=='running' and not x['mute']] == ['Radio']" 15
            sleep 8                                           # the start of a stream may glitch: count after it
            e0=$(status_val "s['audio']['sink'].get('errors')")
            sleep 20
            e1=$(status_val "s['audio']['sink'].get('errors')")
            check "no xruns in 20 s of playback ($e0 -> $e1)" test "$e0" = "$e1"
            check "it is still playing after 20 s" status_py "s['radio'].get('playing') and not s['radio'].get('buffering')"
            info "playing: $(status_val "s['radio'].get('station', {}).get('name')") - $(status_val "s['radio'].get('title')") ($(status_val "s['radio'].get('codec')") $(status_val "(s['radio'].get('bitrate') or 0) // 1000") kbps)"
            if [ "$(post_code /api/transport '{"action": "toggle"}')" = 404 ]; then
                skip "transport toggle (this version has no /api/transport)"
            else
                check "transport toggle pauses it" wait_status "s['radio'].get('paused')" 10
                post_code /api/transport '{"action": "toggle"}' >/dev/null
                check "transport toggle resumes it" wait_status "s['radio'].get('playing') and not s['radio'].get('paused')" 10
            fi
            post_code /api/radio '{"cmd": "stop"}' >/dev/null
            check "stop returns it to idle" wait_status "not s['radio'].get('playing')" 10
        fi
    fi
fi

# ---------------------------------------------------------------------------------------------
section "restarts"
if [ -n "$QUICK" ]; then
    skip "restarts skipped (--quick)"
else
    code=$(post_code /api/service '{"name": "speaker-radio"}')
    if [ "$code" = 200 ]; then
        pass "the Restart request for the radio is accepted"
        check "the radio comes back after Restart" wait_status "s['radio']['available']" 40
    else
        fail "the Restart request for the radio is accepted ($code)"
    fi
    code=$(post_code /api/service '{"name": "audio"}')
    if [ "$code" = 200 ]; then
        pass "'Restart audio' request accepted"
        sleep 5
        check "every service is started again within 90 s of 'Restart audio'" wait_status \
            "all(v in ('started','not in a runlevel') for v in s['services'].values()) and s['radio']['available'] and s['audio']['sink']" 90
    else
        fail "'Restart audio' request accepted ($code)"
    fi
fi

# ---------------------------------------------------------------------------------------------
section "the board itself (ssh)"
if [ -z "$SSH_TARGET" ]; then
    skip "set SMOKE_SSH=user@host for process, memory and kernel-log checks"
elif ! remote true; then
    fail "ssh to $SSH_TARGET works (public key refused or host unreachable)"
else
    pass "ssh to $SSH_TARGET works"
    # /proc is mounted with hidepid on the board: use doas, but only ever print pids and sizes (the
    # Spotify key is in a process argument, so a full process list must not be shown). The [x] trick
    # keeps pgrep from matching the shell that runs it.
    # (supervise-daemon's own command line repeats the service's, so it is skipped.)
    pids() { printf '%s\n' 'for p in $(pgrep -f "$1"); do case "$(cat /proc/$p/comm 2>/dev/null)" in supervise-daem*) ;; *) echo $p ;; esac; done' | ssh "${SSH_OPTS[@]}" "$SSH_TARGET" doas sh -s -- "$1" 2>/dev/null; }
    rss_kb() { local p max=0 kb; for p in $(pids "$1"); do
        kb=$(ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "doas awk '/VmRSS/{print \$2}' /proc/$p/status" 2>/dev/null); [ "${kb:-0}" -gt "$max" ] && max=$kb; done; echo "$max"; }
    for pair in "[p]anda-arbiter.py:60000" "[p]anda-web.py:80000" "[a]udio-client-name=radio:200000"; do
        pat=${pair%:*}; limit=${pair##*:}
        n=$(pids "$pat" | wc -l)
        if [ "$n" -eq 0 ]; then fail "$pat is not running"; continue; fi
        if [ "$n" -gt 1 ]; then fail "$pat runs $n times (expected 1)"; else pass "exactly one $pat"; fi
        kb=$(rss_kb "$pat")
        if [ "$kb" -lt "$limit" ]; then pass "$pat uses ${kb} kB (limit $limit)"; else fail "$pat uses ${kb} kB (limit $limit)"; fi
    done
    check "no crashed services in OpenRC" test -z "$(remote "rc-status -a 2>/dev/null | grep -i crashed")"
    oops=$(remote "dmesg 2>/dev/null | grep -iE 'oops|BUG:|Call trace|segfault|out of memory' | tail -3")
    if [ -z "$oops" ]; then pass "no oops, segfault or OOM in the kernel log"; else fail "kernel log: $oops"; fi
    info "load: $(remote cat /proc/loadavg)"
fi

# ---------------------------------------------------------------------------------------------
section "logs (no new tracebacks since the start of this run)"
for name in $LOGS; do
    now=$(get "/api/logs?name=$name" 15 | grep -c Traceback)
    if [ "$now" -gt "${TB0[$name]}" ]; then fail "log $name: $((now - TB0[$name])) new traceback(s)"; else pass "log $name: no new traceback"; fi
done

# ---------------------------------------------------------------------------------------------
section "by hand (needs a person and a phone)"
cat <<'EOF'
  1. Play something over AirPlay, then start Bluetooth audio on a phone: one source is audible at a time.
  2. Start Spotify Connect while Bluetooth plays; then stop Spotify: nothing comes back by itself
     (except AirPlay, which was only muted).
  3. Pause AirPlay from the phone, then start it again: it is audible (not stuck muted).
  4. Play a radio station from the web page, then start AirPlay: the radio stops.
  5. In the web page: kill nothing, press "Restart radio" when it says not responding.
EOF

echo
echo "$passed passed, $failed failed, $skipped skipped"
[ "$failed" -eq 0 ]
