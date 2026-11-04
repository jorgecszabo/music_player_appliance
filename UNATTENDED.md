# Unattended work plan (night of 2026-11-04)

The owner is asleep. Everything here runs without them: no phone, no button presses, no
card writes, no Fedora changes. Results are logged at the end of this file, one commit per task.

## Ground rules

- **Fedora is off limits.** No config changes, no `pactl`/`wpctl` changes on the host, no
  line-in recording (it needs host volume changes and it's night). Throwaway podman containers
  are fine (e.g. the PipeWire RAOP sender used for the AirPlay test).
- **Quiet:** the board's output feeds the PC line-in (and possibly speakers). Before any test
  audio, set the codec's hardware gain to its minimum (`Headset` playback, restore afterwards)
  and measure digitally on the board (PipeWire sink monitor), not by ear.
- **Secrets:** never run `top`/`ps`/`pgrep -a` or read `/proc/*/cmdline` as root (Soloist's API
  key is in its argv). Never read or print `/etc/panda/soloist.key`.
- **Don't lose the board:** no kernel, bootloader, `extlinux`, network, sshd or doas changes
  on the device tonight. Firewall changes only if a test proves SSH stays reachable. Before a
  risky change, copy the overlay (`panda.apkovl.tar.gz` -> `.bak` on the card). Reboots only when
  a task needs one, at most a few. If the board doesn't come back within 5 minutes, stop
  touching it and write down what happened.
- **Bluetooth:** the phone isn't available. Don't forget devices, don't pair, don't test BT.
- **Factory reset:** implement it, but test only the dry-run mode (a real reset wipes the API
  key and the pairing).
- **No SD card writes.** Image builds go to `build/` only; the owner writes the card.
- Commit after each task (attribution line), and keep PLAN.md's Status up to date.

## Tasks, in order

### 1. AirPlay: clipping, choppiness, delay (diagnose and fix)

Reports: clipping on loud music from the PC, choppy playback, large delay.

Measure (digital, on the board): play known signals with the PipeWire RAOP sender in a
container (the sender Fedora itself uses). Record the board's sink monitor with `pw-record`.
- **Clipping:** a 0 dBFS sine near fs/4 with 45 degree phase (true peak about +3 dBFS), plus
  a -1 dBFS sweep. Count samples at full scale after resampling 44.1 -> 96 kHz. Suspect:
  intersample overs from the resampler, with no headroom.
- **Choppiness:** a continuous 1 kHz sine for 60 s. Detect gaps/discontinuities in the
  recording, check `pw-top` errors, and turn on shairport-sync's statistics. Suspects: the ALSA
  path (shairport -> pipewire-alsa plugin with small buffers), `interpolation = "basic"`, or
  CPU/timing.
- **Delay:** measure from sending to arriving at the sink. Classic AirPlay buffers about 2 s by
  design (sender-side `latency`); separate the sender's share from the board's share.

Fixes to try, keeping only what measurably helps:
- Headroom: a fixed -3 dB (or so) gain before the output. Implement it as part of the task 2
  filter-chain, or as a sink volume if task 2 slips.
- shairport-sync: larger `audio_backend_buffer_desired_length_in_seconds`, pipewire-alsa
  latency (`PIPEWIRE_ALSA` / node.latency), `interpolation = "soxr"` if CPU allows,
  statistics on during tests only.
- If the ALSA path is the problem: build shairport-sync with the native PipeWire backend
  (`--with-pipewire`) as our own aport, cross or in the armv7 container, and switch to it.
- Delay: document what's possible on the receiver side. Lowering the sender's latency is a
  Fedora-side setting (not touched; report the option).

### 2. Volume cap + tone (bass/treble) + headroom

- PipeWire filter-chain sink between the streams and the ALSA output: preamp (headroom),
  low-shelf (bass) and high-shelf (treble) biquads, at 96 kHz. Make it the default sink.
- Volume cap: a maximum for the output volume, enforced on the filter sink (and for
  per-stream volumes where cheap).
- Settings in `/etc/panda/audio.conf` (lbu), applied live without a restart where possible.
- Web page: replace "under construction" with Bass/Treble/Cap sliders (retro style).
- Measure CPU at 96 kHz; must stay small.

### 3. Last source wins

- When a new stream starts playing, pause or mute the others. Soloist via its WebSocket/ctl
  API (pause), AirPlay via shairport (mute or stop the session), Bluetooth via AVRCP Pause on
  the bluez MediaPlayer1 (panda-bt). Fallback: mute the older stream in PipeWire.
- A small watcher (probably inside panda-web's collector or a separate service).
- Test with AirPlay (container sender) against Spotify idle. Bluetooth paths only by code
  review (no phone).

### 3b. Internet radio (requested 2026-11-04)

- Stations from radio-browser.info (its public API: search by name/country/tag/top voted,
  resolve the stream URL via its `url_resolved`, send a click on play as the API asks).
- Playback with mpv (headless, `--no-video`, PipeWire audio output) as user speaker, a small
  `speaker-radio` service controlled through mpv's JSON IPC socket. It plays into the tone
  filter like every other source and takes part in last source wins.
- Web page: a "Radio" window in the same 90s style (station search, results table, favorites,
  play/stop, now-playing ICY title on the LCD). Favorites stored in /etc/panda (lbu, saved with
  the same debounce as other settings).
- Watch CPU: decoding MP3/AAC/Opus at the station's rate, resampled to 96 kHz.

### 4. Now-playing metadata

- Spotify: Soloist's `-w` WebSocket API (localhost only), feeding title/artist to the LCD.
- AirPlay: shairport-sync's metadata pipe, parsed for title/artist.

### 5. Factory reset

- At boot, if S2 is held: delete the overlay (`*.apkovl.tar.gz`) from the card, play the
  reset chime, reboot into the image defaults. Check how early the button can be read.
  Fallback: an OpenRC service early in boot.
- Also from the web page (Settings, with a confirm dialog) and `web-admin.sh factory-reset`.
- Test only the dry-run mode (logs what it would delete).

### 6. Boot and runtime cleanup

- `rc_parallel`, only needed services, getty on the serial console only. Check what's enabled.
- Measure boot time before and after (one or two reboots, carefully).

### 7. Image integration (host side only, no card writes)

- Kernel r3: the soft-lockup/hung-task/watchdog-sysfs options from the watchdog task; drop the
  phantom bq27000/HDQ battery driver. Cross-build it.
- genapkovl: ship `appliance/rootfs`, run setup.sh at first boot (packages from the card's
  apk cache; Soloist downloaded online). Add every package to `packages.list`. Soloist runtime
  and chimes onto the card (chimes pre-synthesized on the host).
- Build `build/sdimage/` and check its contents. The owner writes and tests the card later.

### 8. If time is left

- McPDM side quest: read-only investigation (dynamic debug if available, driver reading).
  Prepare a patch for later, don't install it.
- Web page: watchdog/kernel info, small polish.

### 9. Last: platform-specific vs generic (requested 2026-11-04, after everything else)

Goal: building this same appliance for another board should be easy. Done after all other
tasks are committed.
- Audit every file for board assumptions (OMAP4/PandaBoard, TWL6040, WL1271, mmcblk0p1,
  armv7, ttyS2, LED/button names, watchdog device, 96 kHz, crackle workarounds, raw-mode
  MLO/u-boot, Soloist arm32 build, Debian armhf runtime) and move them behind `board.conf` or
  into `boards/<board>/`.
- Generic `appliance/` + generic scripts; per-board: kernel aport/fragment, bootloader,
  card writer, mkimage profile, board.conf (audio quirks such as the TWL6040 workarounds as
  board options, not hard-coded).
- Document "porting to a new board" step by step (README), with a template board directory
  (e.g. `boards/_template/`) and a checklist of what to fill in and test.
- Check the portability by dry-running the generic parts for a second architecture where
  possible (e.g. generate configs for an aarch64 board.conf), without hardware.

---

## Results log

**Reminder for the end of the night:** the volume cap is at step 0 (-30 dB) for quiet testing.
Set it back to 15 (0 dB) before finishing, keeping the owner's tone settings: the owner set
**bass +10 dB** from their phone (192.168.0.105) during the night; I reset it by mistake while
chasing a "phantom" change and restored it (web-admin.sh set-audio 10 0 15 at the end).

### Task 1: AirPlay (done)

Method: test signals sent from throwaway PipeWire containers (Alpine 1.6.8, then a Fedora 44
image with PipeWire 1.6.11 = the owner's version), the board's output recorded digitally
(sink monitor, f32 96 kHz) and analyzed (overs, level dips, sine-predictor discontinuities).

- **Found: the codec ran at full hardware gain.** WirePlumber's fallback profile mapped the
  output volume onto the TWL6040 `Headset` control (100% = 0 dB), overriding board.conf's -10 dB.
  Fixed with `api.alsa.soft-mixer` (software volume only). This also made the board overload
  the PC line-in. (fcd4c2c)
- **Clipping: confirmed and fixed.** A 0 dBFS fs/4 sine at 45 deg (true peak +3 dB) arrived at
  +0.93 dBFS with 14449 samples over full scale (44.1 -> 96 kHz resampler overs, no headroom).
  With the task 2 tone filter's -3 dB headroom: -2.07 dBFS, 0 overs.
- **Native PipeWire backend for shairport-sync.** Alpine's package passes `--with-pw`/`--with-pa`,
  which 5.x ignores (`--with-pipewire`/`--with-pulseaudio`), so it had no PipeWire backend and
  used the pipewire-alsa plugin. Local aport `aports/shairport-sync` (r100) fixes the flags and
  drops PulseAudio/D-Bus/MPRIS. Delay-measurement jitter ("Av Sync Window") 9-15 ms -> 2-3 ms.
  deploy.sh ships our packages; setup.sh installs them (apk cache, world pinned).
- **Choppiness: not reproducible with the owner's PipeWire.** With Alpine's PipeWire 1.6.8 as
  the sender there's a click exactly every 2^20 frames (23.778 s): `module-raop-sink`
  `stream_send_packet` encodes only `iov[1]` and drops `iov[2]` when a packet straddles the RTP
  ring buffer (2^22 bytes) wrap. PipeWire 1.6.11 (Fedora) passes all iovs; with a Fedora 44
  sender: 0 clicks, no dips, steady level over 60 s. Remaining sub-sample steps (-41 dB re
  signal) are shairport's sync corrections; inaudible. Receiver-side knobs (interpolation soxr,
  drift tolerance 10/25 ms, disable_synchronization, ignore_volume_control) made no difference,
  so the config stays at the defaults (`interpolation = "basic"`).
  If the owner still hears chops: test again (the gain/clipping fixes may have been it); else
  look at the network path (UDP `RcvbufErrors` was 73 at one point, 0 during tests).
- **Delay: set by the sender.** shairport logs "New latency: 66150" frames (1.5 s) requested by
  PipeWire's raop sink, plus about 0.1 s of PipeWire output buffering. Lowering it is a Fedora-side
  setting (`raop.latency.ms` on the raop sink/discover module); not touched.
- Upstream notes for later: Alpine shairport-sync APKBUILD configure flags; Alpine 3.24's
  PipeWire 1.6.8 raop-sink iov bug (fixed by 1.6.11 / master 696667af).

### Task 2: tone, headroom, volume cap (done)

- PipeWire filter-chain sink `panda-eq` (default sink; its volume is the master volume):
  preamp -> 100 Hz low shelf -> 8 kHz high shelf at 96 kHz. Preamp = -(3 dB + largest boost), so
  boosting can't clip. Measured: a -6 dBFS tone comes out at exactly -9.00 dBFS.
- Volume cap = the codec's hardware gain (board.conf `AUDIO_HW_VOLUME_CONTROL/_MAX/_STEP_DB`),
  applied at boot by panda-audio-init from `/etc/panda/audio.conf`.
- Web page: Bass/Treble/Max volume sliders + Flat; `web-admin.sh set-audio` validates and
  applies live (pw-cli Props on the filter node, amixer for the cap); saved 15 s after the
  last change. Gotcha: Alpine ships `libpipewire-module-filter-chain` in **pipewire-pulse**.

### Task 3: last source wins (done; Spotify/Bluetooth paths untested)

- `speaker-arbiter` (user speaker): `pw-dump --monitor` through a pty (it block-buffers pipes),
  parsed per object (updates are partial, the top-level array stays open; re-parsing per line
  was quadratic). A new playing source stops the others: Spotify `soloist ctl pause` (Soloist
  now has `-w 127.0.0.1:47000`), Bluetooth AVRCP Pause, Radio via mpv IPC `stop`; AirPlay and
  anything still playing after 2 s are muted, and unmuted when they're the only source again.
- Tested: AirPlay vs a local stream (muted -9 -> -60 dB while the newcomer played, unmuted
  after), and AirPlay stopping the radio. Spotify pause and Bluetooth pause need the phone
  (Soloist was logged out by the restarts; the owner picks "Panda" in the app once).

### Task 3b: internet radio (done)

- `speaker-radio`: mpv idle, audio only, `--ao=pipewire`, locked down (`--config=no
  --load-scripts=no --ytdl=no`), reconnects, JSON IPC socket `/run/speaker/radio.sock` (0660,
  group speaker; a stale socket must be removed before start or mpv's new 0600 one replaces
  the chmodded file). About +57 MiB of RAM for mpv and its libraries.
- panda-web talks to radio-browser.info server-side (all.api.radio-browser.info, descriptive
  User-Agent, `/json/url/<uuid>` to count the click and resolve the URL), so the page keeps
  `connect-src 'self'`. Search by name/genre/country, top voted, popular, favorites (★, stored
  in `/var/lib/panda-web/radio-favorites.json`, in lbu, debounced save).
- "Radio Tuner" window: amber hi-fi display with station + ICY song title, results table.
- Tested: Radio Paradise AAC 320k, ICY title shown, 0 xruns, mpv about 21% of one core.

### Task 4: now playing (done; Spotify untested)

- AirPlay: shairport metadata pipe -> `/run/speaker/nowplaying.json`. Fedora's PipeWire sends
  no track info, only the sender (`snua`, `clip`): the LCD shows "AirPlay - PipeWire/1.6.11
  (192.168.0.110)". iPhone/Mac title/artist (`minm`/`asar`) are parsed too.
- Spotify: `soloist ctl now --json` polled every 5 s while its stream plays (field names
  guessed defensively; needs a logged-in session to test).
- Radio: ICY title from mpv. Bluetooth: AVRCP track from panda-bt (already there).

### Task 5: factory reset (done; real reset not run)

- `/etc/panda/factory-reset.sh [--dry-run]`: restores the **factory overlay**
  (`<boot>/factory/<hostname>.apkovl.tar.gz`) over the saved one, removes other overlays,
  reboots. Refuses without a valid factory copy: in diskless Alpine `lbu commit` overwrites
  the image's original overlay, and deleting it would boot a system with no network or SSH.
- deploy.sh pushes the image's original overlay (build/sdimage) as the factory copy; for now
  that's the Phase 3 overlay (a reset would need `scripts/deploy.sh` afterwards). Task 7's
  image makes first boot run the full setup, and its overlay becomes the factory copy.
- Button: `panda-reset-button` (boot runlevel): S2 held at power-on -> both LEDs blink fast,
  still held after 5 s -> reset; released -> normal boot. Tested the not-held path (1.35 s,
  with the factory overlay moved away as a safety net). Not tested with the button pressed.
- Web page: Settings > "Factory reset..." (pre-check, two confirmation dialogs).

### Task 6: boot cleanup (done)

- Login consoles only on the serial console: tty1-6 gettys commented out in `/etc/inittab`
  (no display on the board), applied live with `kill -HUP 1`.
- `rc_parallel="YES"`: default runlevel done 40 s after kernel start instead of 48 s; all 22
  services start (2 reboots tested). Services are already minimal (each one is used).
