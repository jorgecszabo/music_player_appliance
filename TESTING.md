# Test plan and status

Written 2026-11-07. The suite described here is implemented, except the on-device smoke tests
(section 13), which need a board, and the parts marked "not done" in the table below.

## Running it

```
python3 -m venv .venv && .venv/bin/pip install -r tests/requirements.txt   # once
scripts/check.sh               # ruff, shellcheck, nft syntax, unit + consistency + fuzz tests (~2.5 min)
scripts/check.sh -k radio      # extra arguments go to pytest; FUZZ_EXAMPLES=1000 for a longer fuzz
scripts/test-graph.sh          # the audio-graph integration suite in a podman container (~4 min)
scripts/test-graph.sh tests/graph/test_failures.py -k hung     # a part of it
GRAPH_FUZZ_EXAMPLES=50 GRAPH_FUZZ_STEPS=10 scripts/test-graph.sh tests/graph/test_random.py   # longer random run
scripts/test-ota.sh            # OTA against a built image (unchanged)
scripts/smoke.sh 192.168.0.56  # a running board: API checks, radio playback, restarts (SMOKE_SSH=panda@host for more)
```

Failures of the graph suite leave logs, `pw-dump` and a summary in `build/graph-artifacts/<test>/`.

### On a board

```
PANDA_HOST=panda@<board> scripts/deploy.sh --restart            # code under test, live, until the next reboot
SMOKE_SSH=panda@<board> scripts/smoke.sh <board> [--quick] [--no-radio] [--no-wifi-scan]
```

To test an installed version, update through OTA first (`scripts/build-ota.sh`, then the web page or
`web-admin.sh ota-install` / `ota-activate`), then run `smoke.sh`. Practical notes:

- Use the Ethernet address when there is one; a Wi-Fi link can drop during a run.
- The scripts run ssh non-interactively: a key with a passphrase must be in the agent (`ssh-add`).
- `smoke.sh` restarts the radio and the audio stack (not with `--quick`) and plays a station: use an idle speaker.
- `/proc` is mounted with `hidepid` on the board, so process checks go through `doas` and only print pids and
  sizes; never list command lines as root (the Spotify key is in one).
- Tracebacks in the board's logs are counted before and after the run: old ones are reported, only new ones fail.
- Failing "regression probes" mean the board runs code from before the fixes: deploy first.
- Section 13's checklist (AirPlay, Bluetooth, Spotify with a phone) is printed at the end of the run and is manual.

Layout of the code: `tests/unit` and `tests/fuzz` (run by `check.sh`), `tests/graph` (the container suite),
`tests/fakes` (fake peers: mpv, BlueZ, soloist, radio-browser, a stream server), `scripts/check.sh`,
`scripts/test-graph.sh`, `scripts/smoke.sh`. The graph suite's container is `tests/graph/Containerfile`.

## What exists

| Section | Where | State |
|---|---|---|
| 1 Static checks | `scripts/check.sh`, `ruff.toml`, `tests/unit/test_consistency.py`, `test_firewall.py` | done (lints, drift checks, OpenRC graph, nft syntax) |
| 2 `panda_ipc` | `tests/unit/test_panda_ipc.py`, `tests/fakes/peer.py` | done |
| 3 `panda-web` | `test_web_api.py` (validation + Hypothesis on the POST API), `test_web_misc.py` (GET, Host check, OTA upload), `test_web_radio.py`, `test_web_collector.py` | done |
| 4 `web-admin.sh` | `test_web_admin.py` (runs the script with stub commands) | done |
| 5 `panda-arbiter` | `test_arbiter.py` | done (the parser, state machine and actions are tested in place; no refactor was needed) |
| 6 `panda-bt` | `test_bt.py` | done (control socket, pairing policy, window) |
| 7 `panda-wifi` | `test_wifi.py` | done (injection, PSK vector, import, scan parsing, socket) |
| 8 OTA, `audio-route.py`, configs | `test_ota.py`, `test_audio_route.py`, `test_configs.py` | done; `setup.sh` itself is not tested (users and services) |
| 9 Fuzzing | `tests/fuzz/test_parsers.py`, the Hypothesis tests above | done for the Python parsers; not done: byte-level fuzzing of the OTA tar parser beyond truncation, kernel driver |
| 10 | (front panel on hold) | not planned |
| 11 Front end | `test_frontend.py` (headless Chromium + the demo's fake API) | done |
| 12 Graph suite | `tests/graph/`, `tests/fakes/`, `scripts/test-graph.sh` | done (see below) |
| 13 On-device smoke | `scripts/smoke.sh <host> [--quick] [--no-radio]`, `SMOKE_SSH=user@host` for the ssh checks | written; run on 2026-11-07 against a PandaBoard on Ethernet after a live `deploy.sh`: 48 pass; the one failure is the board's Bluetooth chip (below) |

The graph suite (`tests/graph/`): `test_boot`, `test_transitions` (all 12 pairs) and
`test_transitions_more` (24 triples, repeats, rapid alternation, simultaneous starts),
`test_pause_failures` (a pause that does not work), `test_lifecycle`, `test_routing`,
`test_failures` (kill, hang or misconfigure each service), `test_consistency` (API, SSE and
`nowplaying` against `pw-dump`), `test_radio_streams` (real mpv against a fake station server),
`test_random` (a Hypothesis state machine against a model of the policy) and `test_soak`
(nightly: `GRAPH_SOAK_MINUTES=60 scripts/test-graph.sh tests/graph/test_soak.py -m soak`).

Seams added to the code for the suite (environment variables, nothing sets them on the appliance):
`PANDA_RUN_DIR`, `PANDA_SPEAKER_RUN_DIR`, `PANDA_CONF_DIR`, `PANDA_STATE_DIR`, `PANDA_WEB_DIR`,
`PANDA_ADMIN_CMD`, `PANDA_RADIO_BROWSER_BASE`, `PANDA_PERIOD_SCALE`, `PANDA_SOLOIST_CTL`,
`PANDA_BOARD_CONF`, `PANDA_PAUSE_GRACE_S`, `PANDA_ROUTE_SETTLE_S`, `PANDA_ROUTE_CMD`, and
`PANDA_DEBUG_EVENTS=1` (the arbiter logs one JSON line per decision; useful on a board too).

## Known limitations found while writing the suite

- On the test PandaBoard (192.168.0.55 Ethernet; the onboard Wi-Fi has no driver), `hci0` exists but the WL1271 does not answer HCI Reset (`Opcode 0x0c03 failed: -110`), so `panda-bt` crash-loops with `DBusError: Authentication Failed` at `set_powered(True)`. It predates the deploy and survives `panda-radio-init`/`bluetooth` restarts; `smoke.sh` reports it as a Bluetooth FAIL. The process checks over ssh need `doas` (the board mounts `/proc` with hidepid).
- A pause request takes a few hundred milliseconds to execute (the AVRCP one about a second: a
  D-Bus round trip in a fresh process). If the source it targets is restarted within that time,
  the late pause can hit the new run. The arbiter checks before sending, not during.
- The rig uses Alpine's `mpv` and `pipewire`, not the appliance's own builds, on x86_64 with a
  null sink. It cannot show real-time scheduling, xruns or ALSA behaviour.
- `/api/status` is a snapshot that can be a couple of seconds old (the collector period): a
  client that restarts a service and polls once may see the old state.
- Two streams of the same source (two AirPlay senders): the newer one is audible, the older one
  is muted, like any two sources.

The components below are in dependency order, cheapest and most isolated first. The most
important part is section 12, the integration suite that drives the audio graph through
its states. The unit tests exist to make that suite's failures easy to localise.

Status of the claims: the descriptions of `panda_ipc`, `panda-web`, `panda-arbiter` and
`web-admin.sh` come from reading the code. The ones for `panda-bt`, `panda-wifi`,
`panda-ota` and the shell scripts come from their header comments and
should be confirmed against the code when each test file is written.

Scope note: this is an appliance on obsolete hardware that works in practice (see the
project notes). The plan is ordered by value for effort, and section 14 says what to
skip. Tests must not encode quirks of specific USB dongles or phones; hardware quirks
belong in `board.conf` and are checked on a device (section 13).

## 0. Principles

1. **Test at the boundary where bugs have actually appeared.** Peers that are down,
   restarting, hung, or answering garbage; input that crosses a trust boundary; state that
   is derived from several sources and disagrees.
2. **Real processes where behaviour depends on them, fakes where it doesn't.** PipeWire,
   WirePlumber, mpv and the arbiter are real in the integration suite. Phones, Spotify,
   the radio-browser.info service and hardware are fakes.
3. **No sleeps for synchronisation.** Wait for an observable condition with a timeout
   (`wait_until(predicate, timeout)`). Sleeps are only for "nothing should happen" checks,
   and those use a scaled clock (section 12.3).
4. **Every bug we fix gets a test first** (a regression test that fails before the fix).
5. **Tiers by cost.** T0 static (seconds), T1 unit/component (under a minute), T2
   integration in a container (minutes), T3 on-device smoke (manual or scripted over ssh).
   `scripts/check.sh` runs T0 and T1; `scripts/test-graph.sh` runs T2.

Layout:

```
tests/
  conftest.py            shared fixtures (fake sockets, temp /run dirs, stub PATH)
  unit/                  T1: one file per component below
  fuzz/                  property-based and mutational tests
  graph/                 T2: the integration suite (section 12)
  fakes/                 fake peers: mpv, bt, wifi, soloist, bluez, ICY server, radio-browser
scripts/check.sh         T0 + T1
scripts/test-graph.sh    T2 (podman)
```

Tools: `pytest`, `hypothesis` (property-based and stateful tests), `atheris` or a small
mutator for byte-level fuzzing, `shellcheck`, `ruff`, `bats` or plain pytest + subprocess for
shell. All dev-only; none goes on the appliance.

## 1. Static checks (T0) — `scripts/check.sh`

**What is worth checking**

- Python: `ruff` (or `pyflakes`) for undefined names and unused imports. This is the cheapest
  way to catch typos after a refactor such as the `panda_ipc` extraction.
- `python3 -m py_compile` on everything under `appliance/rootfs/etc/panda/`.
- Shell: `shellcheck` on `*.sh` and on the OpenRC scripts (`#!/sbin/openrc-run`, with
  `-s sh` and the OpenRC dialect directive).
- JS: no Node here, so use headless Chromium on the demo page: it must load with no console
  errors (section 11).
- **OpenRC dependency lint** (small custom script): parse every `depend()` block, build the
  graph, and fail on dependency cycles, on `need X` where `X` does not exist, and on a
  service that `restart_audio` in `web-admin.sh` starts before one it needs.
- Consistency lints that catch drift (the bug that hid the Restart button):
  - every name in `RESTARTABLE` is in `SERVICES`, is accepted by `web-admin.sh restart`,
    and has an init script;
  - every `LOGS` name is accepted by `web-admin.sh logs`;
  - every `/api/...` path the JS uses exists in `panda-web.py`;
  - every `$("id")` in `app.js` exists in `index.html`.
- `nft -c -f firewall.nft` (syntax check) if nftables is available.
- `docs/panel-demo.html` is up to date with its sources (rebuild and `git diff --exit-code`).

**Technique:** plain scripts, no fixtures. Fail fast.

## 2. `panda_ipc.py` (T1)

Small, central, stdlib only: the easiest component to test thoroughly.

**What is worth testing**

- `line_call`: normal reply; socket file missing; socket exists but nobody listening
  (refused); permission denied; peer accepts then closes without replying; peer replies
  with partial line then hangs (timeout); reply is not JSON; reply is JSON but not an
  object; very large reply; peer sends two lines. Always returns a dict, never raises,
  error text names the peer.
- `mpv_raw` / `mpv`: success; mpv-side error (`"property unavailable"`) is a normal reply, not
  an exception; unrelated events interleaved before the reply (mpv sends `{"event": ...}`
  lines); reply with a different `request_id` is skipped; socket closed with no reply raises
  `OSError`; hung peer times out in about `timeout`; `mpv()` returns `None` for both failure
  kinds.
- `radio_probe`: unreachable → `None`; idle → `{"idle": True}`; playing → all properties;
  peer dies after the second property → partial result and total time bounded by about
  2 × `timeout`, not one timeout per property.

**Technique:** a `FakePeer` fixture: a thread listening on a Unix socket in a temp dir, with
a scripted behaviour per connection (reply, close, stall, garbage, partial). Used by every
later component that talks to a socket.

## 3. `panda-web.py` — back end (T1)

The biggest single file and the security boundary (it feeds `web-admin.sh`, which runs as
root, and wpa_supplicant config). Test the handler layer without a real network.

### 3.1 Request validation (highest value)

For every POST endpoint (`/api/radio`, `/api/service`, `/api/audio`, `/api/volume`,
`/api/name`, `/api/spotify-key`, `/api/bt`, `/api/wifi*`, `/api/system`, `/api/transport`):

- table-driven valid and invalid inputs, including wrong types (list, null, number where a
  string is expected, nested objects), missing keys, huge strings, unicode edge cases
  (RTL marks, NUL, newline, surrogates), and boundary values for each regex and numeric range;
- assertion on **what reaches the collaborators**: stub `run()`, `bt_call`, `wifi_call`,
  `mpv_raw` and record their arguments. The invariant is "the only argv that can reach
  `ADMIN` matches a documented verb with arguments that match the documented pattern".
  This is the root boundary, tested directly.
- errors come back as the documented JSON error with a 4xx status, never a 500 or a
  traceback in the body.

### 3.2 Endpoint behaviour

- `/api/service`: restartable names run `ADMIN restart <name>`; `audio` runs `restart-audio`;
  anything else → "not restartable"; `speaker-soloist` removed when `SPOTIFY=no`.
- `/api/status` shape: contains `restartable`, `radio`, `services`, etc., and every key
  `app.js` reads exists (schema check shared with section 11).
- `radio_status` states: unreachable + `rc_states` returns `starting` / `stopping` /
  `started` / `stopped` / `crashed` / missing → `starting` / `stuck` / `stopped`; the 2 s
  cache; message text for each state. Playing/idle/paused/buffering mapping.
- `/api/radio` play: bad uuid; unknown station; station with `file://`, `rtsp://`, empty or
  oversized URL → rejected; down player → the "starting/stuck/stopped" message; mpv error
  reply is not reported as "player down".
- `transport`: source choice (audible stream wins, then paused radio, paused Bluetooth,
  muted AirPlay, last radio station, else "nothing to play"); next/prev wrap around
  favourites; empty favourites.
- Favourites file: missing, corrupt JSON, wrong types, more than 100 entries, concurrent
  writes (atomic replace).
- OTA upload endpoint: size limit, content-type, truncated body, concurrent uploads (the
  `UPLOAD` lock), cleanup of partial files.
- Rate/DoS basics: a slow client does not block other requests (threaded server), many SSE
  clients do not leak threads.

### 3.3 Collector and SSE

- The collector builds the snapshot even when each probe fails (each peer unreachable in
  turn); a slow probe does not block the others for more than its timeout.
- SSE: a new client gets the current snapshot; clients get a new event after
  `wake_fast`; a disconnected client is dropped; sequence numbers are monotonic.
- Fast/slow loop periods (use an injected clock).

### 3.4 radio-browser.info client

- Mirror discovery: DNS failure, reverse lookup failure, filtering by suffix, shuffle,
  1-hour cache.
- `rb_get`: first mirror fails, second works; all fail → 502 with the documented error; cache
  TTL and the 300-entry cap; response not JSON; response huge; response with wrong types.
- Needs a seam (section 12.4): the base URL and scheme must be overridable for tests.

**Technique:** import the module with `importlib` (it has a `__main__` guard), start
`Handler` on `127.0.0.1:0` in a thread, use `http.client`. Collaborators replaced with
fakes through module attributes (`monkeypatch`). Hypothesis for 3.1 (section 9).

## 4. `web-admin.sh` (T1, root boundary)

Runs as root through `doas`, with arguments from the web server. Worth testing even though
the web server also validates, because this is the last line of defence.

- Each verb with valid args, and with: empty, missing, extra args, leading `-`, whitespace,
  newline, `;`, `$(...)`, backticks, glob characters, NUL-adjacent bytes, very long values.
- `set-name`, `set-audio`, `set-master`, `set-soloist-key`, `logs <name>`: the regexes match
  the Python side (a single shared test table drives both).
- `set-soloist-key` reads the key from stdin and never echoes it; `logs` redacts the key
  from `soloist` logs; a test that plants the key in a log line and checks the output.
- `restart <svc>`: only allowed names; `spotify || die` when `SPOTIFY=no`; `bluetooth`
  restarts `panda-bt` afterwards.
- `restart-audio` order (stop dependents before pipewire, start in dependency order). Assert
  against a recorded call log, and that it matches the `depend()` graph from section 1.

**Technique:** put stub `rc-service`, `lbu`, `reboot`, `doas`, `logger` and `soloist` scripts
first on `PATH` that append their argv to a log file; run the script with `sh`; assert on the
log. Use a temp directory for `/etc/panda` paths where the script allows it (add a
`PANDA_ETC` override if not).

## 5. `panda-arbiter.py` (T1)

The arbiter's decisions are the product's central behaviour. Unit-test the logic with the
process and I/O layers cut away, then test it with real PipeWire in section 12.

**Refactor needed (small):** separate the `pw-dump` object parser, the `Arbiter.update`
state machine, and the actions (`stop_source`, `set_mute`, `bt_pause`, `soloist_ctl`) so
tests can feed objects and record actions.

**What is worth testing**

- `source_of(props)`: every documented name/app pairing (bluez_input, airplay/shairport,
  spotify/soloist, radio), unknown clients, missing keys, mixed case.
- `Arbiter.update` fed with recorded `pw-dump` object sequences (capture real ones on a
  board and save as fixtures):
  - node appears (idle) → running → idle/suspended → removed;
  - partial updates without `type`; updates for non-stream nodes; `chime` and
    `panda-eq.output` ignored;
  - state flaps (running → idle → running within a few ms);
  - removal of a stream that was muted by us → `muted_by_us` cleaned, survivor unmuted;
  - the `info is None` removal path.
- `started()` / `stopped()` decisions: "last wins", the 2 s `PAUSE_GRACE_S` and the mute
  fallback (the pause did not work), a source being stopped while another stops at the same
  moment, unmute when the muted stream is the only one left, a stream that starts anew while
  muted wins.
- Output routing: sink or tone filter appears/disappears → `schedule_route` debounced to one
  run; `master=True` only for `panda-eq.output`; timer replaced on each event.
- `NowPlaying`: the clock arithmetic (`position`, `running`, pause/resume, new title reset,
  `PRECISE_S` rule), `drop`/`clear`, JSON file written atomically, no stale fields.
- Metadata parsers: see section 9 for fuzzing `airplay_meta`/`airplay_progress` and the
  Spotify `now --json` shapes (`ms_field`: strings, bools, negatives, huge numbers, lists).
- `mpv_command`: logs and returns when the player is down (uses `panda_ipc`).

**Technique:** feed recorded fixtures; fake clock injected into `NOW` and the grace logic;
recording stubs for the actions.

## 6. `panda-bt.py` (T1)

Root service: pairing agent, window timer, LED, reconnect, control socket.

- **Control socket protocol:** each command with valid and invalid args (`pair`, `forget`
  with a bad path, `forget-all`, `media` with each action), garbage lines, no newline, two
  requests on one connection, a client that disconnects mid-reply.
- **Pairing window state machine:** closed → open (button press or command) → closed by
  timeout or second press; pairing accepted only while open; long press = forget all; LED
  state follows. Test with a fake clock and a fake input device.
- **Agent policy:** "Just Works" accepted only inside the window; paired devices are marked
  trusted; unsolicited pairing outside the window is rejected.
- **lbu writes:** only on new pairing or forget, never on routine connects; serialised with
  `media.lock`.
- **bluetoothd goes away:** the service exits (to be restarted by supervise-daemon).

**Technique:** `dbus-daemon --session` with a Python fake of the parts of `org.bluez` it
uses (Adapter1, Device1, MediaPlayer1, AgentManager1) using `dbus_next`; fake `/dev/input`
via a pipe or `evdev` stub; LED as a temp file. The fake BlueZ is shared with section 12.

## 7. `panda-wifi.py` (T1)

Runs as root, owns wpa_supplicant and udhcpc, and handles untrusted input from the air
(SSIDs, scan results).

- **Adapter selection:** table of adapter sets (onboard only, USB only, both, USB
  hot-plugged, unplugged) → chosen adapter and switch events. Generic rule only; no
  dongle-specific cases.
- **Config generation (injection tests, high value):** SSIDs and passwords containing `"`,
  `\`, `#`, newline, `}`, `network={`, `ctrl_interface=`, NUL, very long, non-UTF-8 bytes,
  emoji, leading/trailing spaces. The generated wpa_supplicant config must parse to exactly
  one network with exactly that SSID, and nothing else must be injectable. Verify with
  `wpa_supplicant -c <file> -n` (config check) where available, plus a parser test.
- **Scan result parsing:** `iw` / wpa_cli output fixtures with odd SSIDs, hidden networks,
  duplicate BSSIDs, 5 GHz vs 2.4 GHz, missing fields. Output to the web page must be escaped
  there (section 11).
- **Saved networks:** PSK derivation matches known test vectors (PBKDF2 of the standard
  examples); passwords and PSKs never appear in replies, logs or error messages (grep the
  captured output); WPA3-only (SAE) keeps the password only in the root file; file mode 0600.
- **`--convert` mode:** round trip, malformed input.
- **First-boot import:** `wifi.json` on the boot partition imported, then deleted.
- **Control socket:** same protocol tests as section 6.
- **Connection state machine:** associating → DHCP → connected; auth failure; DHCP timeout;
  link loss and reconnect; wired takes priority by route metric.

**Technique:** pure-function tests for config/PSK/parsers; fake `wpa_supplicant`,
`wpa_cli`, `iw` and `udhcpc` scripts on `PATH` for the state machine; `hypothesis.text()`
for the SSID/password injection test.

## 8. `panda-ota.py`, `audio-route.py`, `tone-conf.sh`, `gen-configs.sh`, `setup.sh`, `factory-reset.sh` (T1)

- **OTA:** keep `scripts/test-ota.sh` as the end-to-end test and add unit tests that do not
  need a built image:
  - bundle parser on synthetic bundles: header fields, truncated at every offset, wrong
    magic, oversize lengths, negative sizes;
  - signature verified **before** anything else is parsed or written (assert no file is
    created for a bundle with a bad signature);
  - slot state machine (`state` file): install → activate → trial → confirm / rollback →
    revert, with every illegal transition rejected;
  - one test per boot scheme (extlinux and `rpi`).
- **`audio-route.py`:** table of (`audio.conf`, present cards, tone filter present) →
  expected `wpctl` / `amixer` commands; USB volume cap clamps; master volume restore;
  missing config file. Stub `wpctl`, `pw-dump`, `amixer`.
- **`tone-conf.sh`, `gen-configs.sh`, `setup.sh`:** run into a temp root, compare the output
  files with golden files (one per board in `boards/*/`); check that the speaker name is
  escaped wherever it is interpolated (Avahi, shairport, Bluetooth alias, hostname); run
  twice to prove idempotence.
- **`factory-reset.sh --dry-run`:** lists exactly the expected paths and never touches
  anything; the non-dry run is tested only inside a temp root.

**Technique:** golden files; stub binaries on `PATH`; temp-root execution.

## 9. Fuzzing and property-based testing

This is where input we do not control gets exercised. Run in CI for a few seconds per target
and nightly for minutes. Failing inputs are saved under `tests/fuzz/corpus/` as regression
cases.

| Target | Controlled by | Technique | Property |
|---|---|---|---|
| HTTP handler (all POST/GET paths) | LAN client | Hypothesis: random JSON bodies, query strings, headers | no 5xx, no traceback in body, only valid argv reaches `ADMIN`/`run` stubs |
| `web-admin.sh` args | `panda-web`, or an attacker who bypasses it | Hypothesis `text()` / `binary()` as argv and stdin | exits non-zero on anything outside the documented pattern; stub log shows no unexpected call |
| radio-browser JSON | third party | Hypothesis JSON strategies with wrong types / huge values | `station_summary` never raises, output bounded, UI escapes (section 11) |
| ICY metadata / titles | stream operator | mutational: random bytes in `StreamTitle` | truncated to 200 chars, valid JSON out |
| AirPlay metadata pipe (`airplay_meta`, `airplay_progress`) | AirPlay sender | atheris / mutational on the XML-ish items and base64 payloads | no exception, no unbounded memory, bounded output |
| Spotify `now --json` | local soloist | Hypothesis JSON with wrong types | `ms_field` and the field extraction never raise |
| `pw-dump` object stream | local | mutate recorded dumps: truncated objects, odd indentation, huge values | the parser resyncs and never wedges the monitor loop |
| wifi scan output / SSIDs | anyone in radio range | Hypothesis text with control chars | config injection impossible (section 7), page output escaped |
| control sockets (`bt.sock`, `wifi.sock`) | local processes | random lines / byte streams | server survives and keeps serving the next client |
| OTA bundle | uploader | mutate a valid bundle: flip, truncate, extend | rejected before any write; no partial slot |
| `panda_ipc` replies | local peers | random replies from `FakePeer` | never raises out of `line_call` / `mpv()` |

Out of scope on purpose: kernel and radio-frame fuzzing (the out-of-tree `r92su` driver
parses frames in the kernel). It would need `mac80211_hwsim` or syzkaller and is a separate
project (section 14).

## 10. (not used)

The front panel (`panda-lcd`) is on hold and not maintained, so it has no tests in this plan.
The section number stays so the references to the other sections remain valid.

## 11. Front end (`web/app.js`, `index.html`, `style.css`) (T1)

No Node on the host, so use headless Chromium against the demo page (`scripts/build-demo.sh`
already produces it). Driver: Chromium DevTools protocol through a small Python script, or
Playwright if it is installed.

- **No console errors or uncaught exceptions on load**, for each fixture status.
- **Fixture matrix for `/api/status`:** radio idle / playing / paused / buffering / down in
  each state (`starting`, `stuck`, `stopped`); each service state (`started`, `stopped`,
  `crashed`, `not in a runlevel`); Bluetooth with and without a connected device; Wi-Fi
  connected / scanning / failed; OTA in each slot state. Assert on visible text and on which
  buttons are enabled or hidden (for example the "Restart radio" button only when the player
  is down and not starting).
- **XSS and injection:** station names, tags, ICY titles, SSIDs, device names and the speaker
  name containing `<img src=x onerror=...>`, `"`, `'`, `</script>`, very long strings; assert
  no script runs and the text is shown literally. Include the dialog helper and the
  services table.
- **API contract:** every `fetch`/`post` path and method used by `app.js` exists in
  `panda-web.py` (static check from section 1); every key read from `status` exists in the
  snapshot (schema check).
- **Behaviour:** buttons disable while a request is in flight; errors show the dialog;
  reconnection after the SSE stream drops; the page works with `localStorage` unavailable.
- **Layout smoke:** screenshots at phone and desktop widths for human review (not asserted).

## 12. Integration suite: the audio graph (T2) — the most important part

This suite runs the real PipeWire, WirePlumber, `panda-arbiter`, `panda-web` and mpv in a
container, drives them through the states of the audio graph with fake sources and fake
peers, and checks what the system does. Unit tests say each part is right; this suite says
the parts work together, including when one of them dies.

### 12.1 The model being tested

**Entities**

- *Sources* (PipeWire playback streams, identified by `node.name` / `application.name`):
  `Radio` (mpv, client name `radio`), `AirPlay` (shairport), `Spotify` (soloist),
  `Bluetooth` (`bluez_input.*`). Plus ignored nodes (`chime`, `panda-eq.output`).
- *Sinks:* the onboard output (`alsa_output.*`), an optional USB card, and the tone filter
  (`panda-eq`).
- *Services* (OpenRC state): `speaker-pipewire`, `speaker-wireplumber`, `speaker-radio`,
  `speaker-arbiter`, `speaker-shairport`, `speaker-soloist`, `panda-bt`, `panda-web`.
- *Observers:* `nowplaying.json`, `/api/status`, `/api/events` (SSE).

**State of each source**

```
absent ──register──► idle ──start──► running ──stop──► idle ──remove──► absent
                                      │  ▲
                        arbiter pause │  │ resume / start anew
                                      ▼  │
                                   paused (by the source's own mechanism)
                                   or muted (by the arbiter, AirPlay and last resort)
```

**Edges the arbiter owns** (stop mechanism per source):

| Source stopped | Mechanism | Fake used in the rig |
|---|---|---|
| Radio | `mpv stop` over IPC | real mpv |
| Spotify | `soloist ctl pause` | fake `soloist` binary that records calls and can ignore them |
| Bluetooth | AVRCP `MediaPlayer1.Pause` over system D-Bus | fake `org.bluez` on a private bus |
| AirPlay | mute the stream (`wpctl set-mute`) | none needed: real PipeWire mute |
| any, if still running 2 s later | mute as a fallback | the same |

**Invariants** (checked after every scenario, and continuously during the random test):

- I1. After settling, at most one source is audible (running and not muted).
- I2. If any source started after the others, the most recently started one is audible.
- I3. A stream muted by the arbiter is unmuted when it becomes the only running stream or when
  it starts anew; no stream stays muted forever (no "silent speaker" state).
- I4. `nowplaying.json` contains exactly the sources currently running (plus paused ones that
  keep metadata, as the code defines it), and its clock only advances while running.
- I5. `/api/status` (`audio.streams`, `radio`, `nowplaying`) agrees with `pw-dump` within one
  collector period.
- I6. Killing or restarting any one service never leaves the system in a state it cannot
  leave without a reboot: after the supervisor's respawn delay plus a settle time, the
  invariants hold again and a new play request works.
- I7. The arbiter never raises out of its event loop; its log contains no traceback during any
  scenario (grep after each scenario).
- I8. The API never returns a 5xx and never hangs longer than its documented timeout,
  whatever state the graph is in.
- I9. Output routing runs at most once per settle window and only for sink or tone-filter
  events.

### 12.2 Test rig

A podman image on x86_64 Alpine (same major version as the appliance, `ALPINE_VERSION` from
`container/Containerfile`) with: `pipewire`, `wireplumber`, `pipewire-tools` (`pw-dump`,
`pw-cat`), `mpv` (any build with the PipeWire output; the appliance build has no scripting,
so the test uses the same aport if it can be built natively), `dbus`, `python3`,
`py3-dbus-next`, `ffmpeg` (to generate tones), `pytest`. No kernel modules and no sound
hardware.

- **Audio backend:** PipeWire runs with the **null audio sink** (`support.null-audio-sink`
  or `adapter` with `factory.name=support.null-audio-sink`) in place of the ALSA card, named
  `alsa_output.test` so the arbiter's routing sees it. The tone filter (`panda-eq`) is
  loaded from the real config if `pipewire-filter-graph` is available, else faked by a
  null sink named `panda-eq.output`.
- **Processes:** started by a small test supervisor (`tests/graph/rig.py`), not by OpenRC,
  so the test can kill and restart each one and read its log. The supervisor mimics
  `supervise-daemon`: respawn after `respawn_delay`. A second variant runs the real OpenRC
  inside the container (T2b) to test the init scripts and `restart_audio` ordering.
- **Fake sources**
  - `pw-cat --playback` with `-P node.name=airplay` / `spotify` / `bluez_input.AA_BB_...`
    playing a generated tone, started and stopped by the test. They create real PipeWire
    stream nodes that the arbiter classifies exactly as it does in production.
  - Real mpv for `Radio`, fed by a **fake ICY server** (`fakes/icy_server.py`): serves a
    tone as an HTTP stream; can send ICY metadata, stall, drop the connection, return 404,
    redirect, serve a playlist, or serve very slowly. This also exercises mpv's reconnect.
  - **Fake `soloist`** (`fakes/soloist`): an executable that replaces the soloist ctl
    command, records `pause`/`now` calls, and can be told to ignore `pause` (so the 2 s
    grace and mute fallback run) or to print malformed JSON.
  - **Fake BlueZ** (`fakes/fake_bluez.py`): `dbus-daemon` private system bus with
    `org.bluez` objects: a connected device with a `MediaPlayer1` that records `Pause`, and
    modes "fails", "hangs" and "no player".
  - **Fake shairport metadata pipe** writer for AirPlay now-playing items.
  - **Fake radio-browser** (`fakes/radio_browser.py`): HTTP server for station search, UUID
    lookup and click counting.
- **Real front ends under test:** `panda-web` (against fake `panda-bt`/`panda-wifi` sockets
  from section 2, and a stub `doas`/`web-admin.sh` that records calls and, for `restart`,
  restarts the matching rig process), and the SSE stream.
- **Clock:** the arbiter's constants (`PAUSE_GRACE_S`, `ROUTE_SETTLE_S`) and the collector
  periods are read from environment variables, so the rig can run them scaled (grace 2 s →
  0.4 s). Scenarios that depend on real durations also run at scale 1 in a nightly job.

**Observation helpers** (`tests/graph/observe.py`)

- `graph()` → parsed `pw-dump`: streams by source with `state`, `mute`, `volume`.
- `audible()` → sources running and not muted.
- `nowplaying()` → parsed `/run/speaker/nowplaying.json`.
- `status()` → `GET /api/status`; `events()` → the SSE stream into a queue.
- `calls()` → what each fake recorded (soloist pauses, AVRCP pauses, mpv commands,
  `web-admin` verbs, route runs).
- `wait_until(pred, timeout, what)` with a message that prints the full graph on failure.
- Every failure dumps: `pw-dump`, `nowplaying.json`, `/api/status`, all process logs, and the
  recorded calls into `tests/graph/artifacts/<scenario>/`.

### 12.3 Scenarios

Each scenario starts from a clean rig (fresh PipeWire, all services up) unless stated, ends
with the invariant check (I1–I9) and the log scan (I7).

**A. Boot and convergence**

- A1. Start order from nothing: pipewire → wireplumber → radio → arbiter → web. The arbiter
  finds the existing sinks; `/api/status` reports `radio.available`, correct `services`.
- A2. Start in the wrong order (arbiter before PipeWire is ready; web before everything):
  everything converges within the retry period; no crash loop.
- A3. Sinks present before the arbiter starts → routing runs once; tone filter present →
  master volume restore runs once.

**B. Source transition matrix** (the core)

- B1. All 12 ordered pairs (A running, B starts) over {Radio, AirPlay, Spotify, Bluetooth}:
  A is stopped by *its own mechanism* (check the fake's recorded call), B stays audible,
  `nowplaying.json` and the API show B only. Expected results are written down in a table in
  the test file and confirmed against the arbiter code the first time.
- B2. Same source restarts (stop then start; start twice; new stream of the same source while
  the old one is still registered): no spurious pause of itself.
- B3. Three sources: A, then B, then C (24 ordered triples, run in parallel batches): only C is
  audible; both A and B were stopped once each, not repeatedly.
- B4. B stops while A is paused/muted: A returns only by the rules in I3 (AirPlay is unmuted;
  sources that were paused via their own mechanism stay paused until the user resumes, as the
  code defines it).
- B5. Rapid alternation (A, B, A, B at 50 ms spacing, 100 cycles): the end state satisfies I1/I2;
  no thread or file descriptor growth in the arbiter (compare `/proc/<pid>/fd` count and thread
  count before and after).
- B6. Two sources start at the same instant: exactly one remains audible, the choice is stable
  across 50 repetitions (document which one wins).

**C. Pause that does not work**

- C1. Fake soloist ignores `pause`: after the grace the stream is muted; when Spotify is later
  the only stream it is unmuted.
- C2. Fake BlueZ `Pause` fails, hangs (arbiter's 15 s subprocess timeout; the arbiter keeps
  serving other events), or has no player: same fallback.
- C3. mpv unreachable when the arbiter sends `stop` (socket missing, hung, permission
  denied): logged, stream muted by fallback, no exception.
- C4. A source that resumes itself right after being paused (soloist un-pauses): the arbiter
  does not fight forever (bounded retries or a documented single pass); record what happens
  and pin it down as the expected behaviour.

**D. Stream lifecycle in PipeWire**

- D1. Stream node removed while running: arbiter drops it, `nowplaying` entry cleared, a muted
  survivor unmuted.
- D2. Stream node removed while muted by us: `muted_by_us` cleaned.
- D3. Node appears already `running`; node goes `running → suspended → running`; partial
  updates without `type`; ids reused after removal.
- D4. Ignored nodes (`chime`, `panda-eq.output`) never trigger pauses or `nowplaying` entries;
  the chime playing over a running source does not stop it.
- D5. Streams with the same source name but different ids (two AirPlay sessions).

**E. Outputs and routing**

- E1. Add a second sink (USB-like) at runtime: route runs once after the settle window with the
  expected arguments; remove it: route runs again.
- E2. Burst of five sink events within the settle window → one run.
- E3. Tone filter node restarts → route runs with master restore.
- E4. `audio.conf` choosing USB when no USB sink exists (and the reverse): falls back
  predictably, volume cap applied only to USB.

**F. Failure injection** (the part that tests resilience; run each at three starting states:
idle, one source playing, two sources with one paused/muted)

| Killed / restarted | Expected |
|---|---|
| `speaker-radio` (mpv) | stream disappears; `radio.available=false` with `state` `starting` then recovers; a muted survivor is unmuted; play request after recovery works; during the outage the API returns the "starting/stuck/stopped" message and the Restart button path (`/api/service`) restarts it |
| `speaker-radio` hung (`SIGSTOP`) | `radio.state` is `stuck`; status polls stay bounded (≈ 2 × 1 s) and the collector keeps serving other fields; `Restart radio` recovers it |
| `radio.sock` with wrong mode (0600, as before `start_post`) | same as hung: `stuck` |
| `speaker-arbiter` | no new pauses while it is down (documented); after respawn it rebuilds state from `pw-dump` and enforces I1 for streams that started while it was away |
| `speaker-pipewire` | all streams disappear; sources must re-register (mpv, fake sources are restarted by the test as a real player would); arbiter's `pw-dump` exits and it restarts; invariants hold after |
| `speaker-wireplumber` | links drop and return; no stuck mute; routing re-runs |
| `panda-web` | SSE clients reconnect; arbiter and mpv are unaffected; `RADIO["station"]` loss is documented (the station name is empty after a restart even if mpv still plays) |
| `panda-bt` / `panda-wifi` | `/api/status` shows `bt`/`wifi` as unavailable with the `... unavailable` error; nothing else degrades; status poll stays within the timeout |
| `soloist` fake | arbiter logs and mutes by fallback |
| all of the above in sequence, randomly | I6 after each |

- F-extra. `restart_audio` from the API (stub `web-admin.sh` runs the real function against
  the rig): ordering respected, playback resumes only after the user asks again, no orphaned
  processes, no duplicate arbiter.
- F-extra. `Restart radio` pressed repeatedly (button disabled for 5 s on the client, but the
  API called directly 20 times): one restart at a time, no pile-up of `rc-service` processes.

**G. Control-plane consistency** (the layers must agree with the graph)

- G1. After every transition in B, within one collector period: `/api/status` `audio.streams`
  and `radio` match `pw-dump`; the SSE stream delivers at least one event for the change; no
  event shows a state that was never in the graph (no phantom "both playing").
- G2. `POST /api/transport` toggle/next/prev for each source state (the choice rules in
  section 3.2), including radio down.
- G3. `POST /api/volume` and mute: the stream's volume/mute changes in `pw-dump`, and the
  arbiter does not treat the change as a source event.
- G4. The SSE stream (`/api/events`) shows the same source and title as `/api/status` for each state.
- G5. `nowplaying` content for each source: ICY title changes on the fake stream appear in the
  API; AirPlay items from the fake pipe; BlueZ track from the fake `MediaPlayer1`; Spotify
  JSON variants from the fake soloist (the shapes in the code, plus malformed ones).

**H. Radio specifics against the ICY server**

- H1. Play: station lookup → click count → URL → stream running → `nowplaying` and `radio`
  show title/codec/bitrate.
- H2. Stream errors: 404, DNS failure, connection refused, TLS error, slow start (past mpv's
  15 s network timeout), mid-stream drop (mpv reconnects with its `reconnect` options), HLS,
  playlist (`.pls`/`.m3u`), redirect loop, very long headers. The player stays up; the radio box
  returns to "Radio off"; no hang in the API.
- H3. `file://`, `rtsp://`, `edit://` and other schemes returned by the fake radio-browser are
  rejected before reaching mpv.
- H4. Favourites survive a `panda-web` restart; next/prev walk them in order.
- H5. radio-browser outage: search returns the documented 502; play of a favourite still works
  if the URL is cached or fails cleanly.

**I. Randomised (stateful) test**

A Hypothesis `RuleBasedStateMachine` generates sequences of events against the live rig:

- rules: `start(source)`, `stop(source)`, `start_when_playing(source)`, `kill(service)`,
  `hang(service)`, `restart(service)`, `add_sink`, `remove_sink`, `fake_ignores_pause(source)`,
  `api_call(endpoint, random valid body)`, `wait(random short)`;
- a reference model (a few dozen lines) predicts the audible source and which pause
  mechanisms were called;
- invariants I1–I9 are checked after each step once the system settles;
- Hypothesis shrinks a failing sequence to a minimal, replayable list of steps, which the
  suite saves as a regression scenario in `tests/graph/regressions/`.

This is the best tool for finding the interaction bugs this plan cannot predict. It runs for
about 5 minutes per nightly job and 30 s in the quick tier.

**J. Soak**

- J1. 1 hour of random scenario I at scale 1 on the slowest tier we can emulate (CPU limited
  with `--cpus=0.3` to mimic a 1 GHz core): no memory growth beyond a bound (RSS of arbiter,
  panda-web, mpv, pipewire sampled each minute), no fd or thread growth, no log growth beyond a
  bound.
- J2. 24 hours of idle with periodic SSE clients: collector CPU stays bounded.

### 12.4 Seams the code needs for this suite

Small changes, each also good for production clarity. None changes behaviour by default.

1. Environment overrides in `panda-arbiter.py`: `PANDA_RUN_DIR` (for `/run/speaker`),
   `PANDA_SOLOIST_CTL` (command to run instead of the runtime/`ld.so` invocation),
   `PANDA_PAUSE_GRACE_S`, `PANDA_ROUTE_SETTLE_S`, `PANDA_ROUTE_CMD`, `PANDA_BOARD_CONF`.
2. In `panda-web.py`: `PANDA_RUN_DIR`, `PANDA_ADMIN_CMD` (instead of
   `doas -n /etc/panda/web-admin.sh`), `PANDA_RADIO_BROWSER_BASE` (instead of
   `https://<server>`), `PANDA_STATE_DIR` (favourites), collector periods from env.
3. In `panda_ipc.py`: socket paths from `PANDA_RUN_DIR` so the module works in a temp dir.
4. Split the arbiter into parser / state machine / actions (section 5).
5. `PANDA_DEBUG_EVENTS=1` makes the arbiter write one JSON line per decision (stream, old/new
   state, action, reason) to its log. Scenarios assert on decisions rather than on guessed
   timing, and the same output helps debug on a real board.
6. An unprivileged "test mode" for `web-admin.sh` is not needed: tests replace it with
   `PANDA_ADMIN_CMD`.

### 12.5 Running it

- `scripts/test-graph.sh` builds (or reuses) the rig image and runs `pytest tests/graph -m "not soak"`
  in `podman run --rm`, with artifacts mounted out.
- Tiers: `quick` (A, B1, C, D, F for radio and arbiter, 5 minutes), `full` (all except soak,
  about 25 minutes), `nightly` (full + I at 5 minutes + J1).
- Scenarios run in parallel in separate containers (each has its own PipeWire), up to the
  host's core count.
- Flakiness policy: no retries by default; a scenario that needs one is marked and gets a
  tracking note. Timing assertions use the scaled clock and wide margins; "nothing happens"
  checks wait a multiple of the settle window.

### 12.6 What this suite cannot cover

- Real audio devices, ALSA buffers, xruns, underruns and real-time scheduling on a single
  slow core (the glitches that originally mattered).
- Real BlueZ, real AirPlay senders, real Spotify sessions.
- Real Wi-Fi: association, loss, retransmits.
- CPU and memory behaviour on ARMv6/ARMv7 hardware.

Those are covered by the on-device smoke tests below.

## 13. On-device smoke tests (T3)

A script (`scripts/smoke.sh <host>`) over ssh against a deployed board, run after each
release. Each step prints PASS/FAIL, none needs a person except where marked.

- Services all `started`; `/api/status` valid; no `crashed`; uptime and memory within a bound.
- Radio: play a known stable stream, check it runs for 60 s with no xruns (`pw-top` / the
  panel's xrun counter) and the reported bitrate; stop; kill mpv and see it come back with the
  radio box recovering; press the Restart button path through the API.
- Source switching with a phone (manual): AirPlay → Bluetooth → radio → Spotify, one audible
  at a time; checklist in the output.
- Restart audio from the API: playback can be started again within a bound.
- OTA: `scripts/test-ota.sh` against the device slots (install, trial boot, confirm).
- Wi-Fi: scan returns networks; connect/forget a test network; wired + Wi-Fi metrics.
- Resource budget: arbiter + web + mpv RSS and CPU at idle and while playing against stored
  baselines for each board (flag a regression of more than 20%).

## 14. Deliberately not tested

- Kernel drivers (`drivers/r92su`), bootloaders, the board kernels, mkimage output.
- Alpine packages themselves; third-party code (PipeWire, mpv, BlueZ, shairport, wpa_supplicant).
- Exact pixel layout of the web page (screenshots are for review only).
- Dongle- or phone-specific behaviours (they belong in `board.conf` and device checks).
- Performance microbenchmarks beyond the soak and resource baselines.
- The front panel (`panda-lcd`): on hold and not maintained.

## 15. Order of work

1. `scripts/check.sh` with section 1 and the consistency lints (an hour; would have caught the
   missing Restart button).
2. `panda_ipc` tests and `FakePeer` (section 2).
3. Seams in section 12.4 (items 1–3, 5) and the rig skeleton: PipeWire with the null sink,
   fake sources, `observe.py`. Get scenario B1 for one pair green.
4. Scenarios B, C, D, F-radio, F-arbiter (the core of the graph suite).
5. `panda-web` validation tests and the HTTP fuzz target (sections 3.1 and 9); `web-admin.sh`
   tests (section 4).
6. The Hypothesis stateful test (12.3 I).
7. `panda-wifi` injection tests and the front-end XSS fixtures.
8. `panda-bt`, `panda-ota` unit tests.
9. Soak and the device smoke script.
10. CI: run `check.sh` on every push and `test-graph.sh quick` on pull requests (or a
    scheduled local job, since the repository is private and has a single developer).
