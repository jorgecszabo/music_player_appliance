#!/usr/bin/env python3
"""Apply the output choice (OUTPUT in /etc/panda/audio.conf) to the running audio graph.

  audio-route.py            apply once and print what was done
  audio-route.py --master   also restore the master volume (audio.conf MASTER_VOLUME);
                            panda-arbiter passes it when the tone filter (re)starts

Every source plays into the tone filter (panda-eq); this decides where the
filter's output stream (panda-eq.output) goes:
  OUTPUT=onboard  the board's own output (board.conf AUDIO_PW_SINK)
  OUTPUT=usb      the first USB audio output, when one is plugged in
The choice is a "target.object" entry in PipeWire's default metadata; WirePlumber
moves the stream at once. Without a USB card (or when it's unplugged) the stream
falls back to the filter's configured target, the onboard output, and moves back
when the card returns.

Also, for each USB card: its output mode (ACP profile, audio.conf USB_PROFILE:
"auto" = WirePlumber's choice, or a profile name such as output:analog-stereo,
output:iec958-stereo, pro-audio), its hardware playback controls at full level
and unmuted (they come up at a random level; volume is software), and its
software volume set to the USB volume cap (audio.conf USB_MAX_DB).

Idempotent. Run by panda-arbiter (user speaker) when outputs come and go, and by
web-admin.sh (root) when the settings change. Board-independent: USB outputs are
recognized by device.bus = "usb".
"""
import json
import os
import re
import subprocess
import sys

ENV = dict(os.environ, PIPEWIRE_RUNTIME_DIR="/run/speaker", XDG_RUNTIME_DIR="/run/speaker")
EQ_OUTPUT = "panda-eq.output"


def shell_conf(path):
    out = {}
    try:
        lines = open(path).read().splitlines()
    except OSError:
        return out
    for line in lines:
        m = re.match(r'^([A-Z_][A-Z0-9_]*)=(.*)$', line)
        if m:
            v = m.group(2).strip()
            q = re.match(r'^(["\'])(.*?)\1', v)
            v = q.group(2) if q else re.split(r'\s+#', v, maxsplit=1)[0].strip()
            out[m.group(1)] = v
    return out


def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=ENV)
        return r.returncode, r.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def usb_cap_db(audio):
    try:
        return max(-60, min(0, int(audio.get("USB_MAX_DB", "0"))))
    except ValueError:
        return 0


def dump():
    """(nodes, devices) as {"id", "props", "params"}."""
    rc, out = run(["pw-dump", "--no-colors"], timeout=15)
    try:
        objs = json.loads(out) if rc == 0 else []
    except ValueError:
        objs = []
    res = {"PipeWire:Interface:Node": [], "PipeWire:Interface:Device": []}
    for o in objs:
        if o.get("type") in res:
            info = o.get("info") or {}
            res[o["type"]].append({"id": o["id"], "props": info.get("props") or {},
                                   "params": info.get("params") or {}})
    return res["PipeWire:Interface:Node"], res["PipeWire:Interface:Device"]


def usb_devices(ds, board_card):
    return sorted((d for d in ds if d["props"].get("device.bus") == "usb"
                   and d["props"].get("device.api") == "alsa"
                   and (not board_card or d["props"].get("api.alsa.card.name") != board_card)),
                  key=lambda d: d["id"])


def set_profile(dev, want):
    """Switch a USB card to the profile named in audio.conf (if it has it).
    Returns True if it changed (its nodes are recreated: route again after)."""
    profiles = dev["params"].get("EnumProfile", [])
    if not want or want == "auto":
        # WirePlumber's own pick: the best available profile that plays.
        ok = [p for p in profiles if p.get("name") != "off" and p.get("available") != "no"]
        if not ok:
            return False
        want = max(ok, key=lambda p: p.get("priority", 0))["name"]
    cur = [p.get("name") for p in dev["params"].get("Profile", [])]
    if want in cur:
        return False
    for p in profiles:
        if p.get("name") == want:
            run(["wpctl", "set-profile", str(dev["id"]), str(p["index"])])
            print(f"USB card {dev['props'].get('device.description')}: mode {want}")
            return True
    print(f"USB card {dev['props'].get('device.description')}: has no mode {want}; left as is")
    return False


def usb_outputs(ns, board_card):
    """USB playback sinks, oldest (lowest id) first. Not the board's own card
    (board.conf AUDIO_CARD), should that be a USB one."""
    return sorted((n for n in ns if n["props"].get("media.class") == "Audio/Sink"
                   and n["props"].get("device.bus") == "usb"
                   and n["props"].get("node.name", "").startswith("alsa_output.")
                   and (not board_card or n["props"].get("api.alsa.card.name") != board_card)),
                  key=lambda n: n["id"])


def db_ranges(card):
    """{"PCM": (min dB, max dB) or None} for each playback volume control, from the
    TLV in amixer contents (dBminmax, or dBscale with the raw range)."""
    rc, out = run(["amixer", "-c", card, "contents"])
    res = {}
    if rc != 0:
        return res
    for block in re.split(r"(?m)^(?=numid=)", out):
        m = re.search(r"name='(.*) Playback Volume'", block)
        if not m:
            continue
        rng = None
        raw = re.search(r"min=(-?\d+),max=(-?\d+)", block)
        mm = re.search(r"dBminmax-min=(-?[\d.]+)dB,max=(-?[\d.]+)dB", block)
        sc = re.search(r"dBscale-min=(-?[\d.]+)dB,step=(-?[\d.]+)dB", block)
        if mm:
            rng = (float(mm.group(1)), float(mm.group(2)))
        elif sc and raw:
            lo, step = float(sc.group(1)), float(sc.group(2))
            rng = (lo, lo + step * (int(raw.group(2)) - int(raw.group(1))))
        res[m.group(1)] = rng
    return res


def init_mixer(card):
    """Every playback control of a USB card at full level, unmuted: 0 dB when its dB
    range is believable (from below 0 dB to 0 dB or more), else 100%. Cheap cards
    often report nonsense (the Edifier R19U: 0..999 as 0..+3.9 dB, where raw 0 is
    silence), so a range that starts at or above 0 dB isn't trusted."""
    rc, out = run(["amixer", "-c", card, "scontents"])
    if rc != 0:
        return []
    ranges = db_ranges(card)
    done = []
    for block in re.split(r"(?m)^(?=Simple mixer control )", out):
        m = re.match(r"Simple mixer control '(.*)',(\d+)", block)
        caps = re.search(r"Capabilities:(.*)", block)
        if not m or not caps:
            continue
        name, caps = f"{m.group(1)},{m.group(2)}", caps.group(1).split()
        if "pvolume" in caps:
            rng = ranges.get(m.group(1))
            level = "0dB" if rng and rng[0] < 0 <= rng[1] else "100%"
            run(["amixer", "-q", "-c", card, "sset", name, level])
            done.append(f"{name} {level}")
        if "pswitch" in caps:
            run(["amixer", "-q", "-c", card, "sset", name, "unmute"])
    return done


def master_volume(audio):
    """audio.conf MASTER_VOLUME: wpctl's (cubic) scale, 0..1, as the web slider."""
    try:
        return max(0.0, min(1.0, float(audio.get("MASTER_VOLUME", "1.0"))))
    except ValueError:
        return 1.0


def main():
    board = shell_conf("/etc/panda/board.conf")
    audio = shell_conf("/etc/panda/audio.conf")
    want = audio.get("OUTPUT", "onboard")
    ns, ds = dump()
    # Output mode first: a profile switch recreates the card's nodes (the
    # arbiter sees them and runs this again).
    changed = [set_profile(d, audio.get("USB_PROFILE", "auto"))
               for d in usb_devices(ds, board.get("AUDIO_CARD_NAME") or board.get("AUDIO_CARD"))]
    if any(changed):
        return 0
    eq = next((n for n in ns if n["props"].get("node.name") == EQ_OUTPUT), None)
    usb = usb_outputs(ns, board.get("AUDIO_CARD_NAME") or board.get("AUDIO_CARD"))

    if "--master" in sys.argv[1:]:
        sink = next((n for n in ns if n["props"].get("node.name") == "panda-eq"), None)
        if sink is not None:
            v = master_volume(audio)
            run(["wpctl", "set-volume", str(sink["id"]), f"{v:.3f}"])
            print(f"master volume restored: {round(v * 100)}%")

    cap = usb_cap_db(audio)
    for n in usb:
        card = n["props"].get("api.alsa.card") or n["props"].get("alsa.card")
        if card is not None:
            ctl = init_mixer(str(card))
            print(f"USB card {card}: playback controls: {', '.join(ctl) or 'none'}")
        # wpctl's volume scale is cubic: amplitude = v^3.
        v = (10 ** (cap / 20)) ** (1 / 3)
        run(["wpctl", "set-volume", str(n["id"]), f"{v:.4f}"])
        print(f"{n['props'].get('node.name')}: volume cap {cap} dB")

    if eq is None:
        print(f"no {EQ_OUTPUT} node (tone filter not running)")
        return 1
    if want == "usb" and usb:
        target = usb[0]["props"]["node.name"]
        run(["pw-metadata", str(eq["id"]), "target.object", target, "Spa:String"])
        print(f"output: {target}")
    elif want == "usb":
        # Nothing plugged in: keep any earlier USB target (WirePlumber falls back
        # to the onboard output and returns when that card comes back).
        print(f"output: USB selected but none plugged in; onboard ({board.get('AUDIO_PW_SINK', '?')})")
    else:
        run(["pw-metadata", "-d", str(eq["id"]), "target.object"])
        print(f"output: onboard ({board.get('AUDIO_PW_SINK', '?')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
