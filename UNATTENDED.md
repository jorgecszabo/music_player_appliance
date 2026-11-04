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

---

## Results log

(Filled in as tasks complete.)
