#!/usr/bin/env python3
"""Front panel: a Win98-style UI on a small TFT with touch and buttons (panda-lcd).

Runs as user panda-lcd (groups video, input, speaker) on boards whose board.conf
sets LCD_FB (boards/rpi1: 320x240 ILI9341 + STMPE610 touch + 4 buttons).

- Data: the web page's status stream (http://127.0.0.1:8080/api/events), so the
  panel and the page always agree. Actions go through the same API
  (/api/transport, /api/volume, /api/radio, /api/bt, /api/system).
- Display: draws the whole frame with Pillow, compares it with the last one,
  and copies only the changed bands into the framebuffer (fbtft sends only the
  touched lines over SPI). Nothing is redrawn while nothing changes.
- Input: the buttons (LCD_KEYS_NAME, grabbed: nothing else acts on them) are
  media keys: play/pause, next, volume down/up; in a dialog Play = OK and
  Next = Cancel. Touch (LCD_TOUCH_NAME) maps raw ADC values to pixels with an
  affine calibration (/var/lib/panda-lcd/calibration.json, made on the
  Calibrate screen).
- Idle: when nothing has played for SAVER_S, a screensaver; after DARK_S the
  backlight goes off. Any input only wakes the screen.
- Spectrum: panda-spectrum (C, fixed-point FFT) on the output's monitor, only
  while Now Playing is visible and something plays (RPI1.md task 7).
- On SIGTERM: draws the last screen ("safe to turn off", "restarting", or
  blank) from /run/panda-lcd/final, which the init script writes.

  panda-lcd.py                  run (needs board.conf LCD_FB)
  panda-lcd.py --render-test D  draw every screen with sample data into D/*.png
"""
import json
import math
import mmap
import os
import queue
import select
import signal
import subprocess
import sys
import threading
import time
import urllib.request

from PIL import Image, ImageChops, ImageDraw, ImageFont

W, H = 320, 240
API = "http://127.0.0.1:8080"
FONTS = "/usr/share/panda/fonts"
STATE_DIR = "/var/lib/panda-lcd"
CAL_FILE = STATE_DIR + "/calibration.json"
FINAL_FILE = "/run/panda-lcd/final"
SPECTRUM = "/usr/libexec/panda/panda-spectrum"
SAVER_S = 120
DARK_S = 600
OSD_S = 1.5
CRASH_S = 5
MARQUEE_HZ = 4
SUPERVISED = ("speaker-pipewire", "speaker-wireplumber", "speaker-shairport", "speaker-radio",
              "speaker-arbiter", "speaker-soloist", "panda-bt", "panda-wifi")

# Win98 palette, the same as web/style.css.
DESKTOP = (0, 128, 128)
FACE = (192, 192, 192)
LIGHT = (223, 223, 223)
HILIGHT = (255, 255, 255)
SHADOW = (128, 128, 128)
DARK = (10, 10, 10)
TITLE_A, TITLE_B = (0, 0, 128), (16, 132, 208)
TITLE_OFF_A, TITLE_OFF_B = (128, 128, 128), (181, 181, 181)
FIELD = (255, 255, 255)
TEXT = (0, 0, 0)
LCD_BG, LCD_FG, LCD_DIM, LCD_PEAK = (0, 0, 0), (0, 224, 0), (10, 92, 10), (224, 224, 0)
BSOD = (0, 0, 170)
SAFE = (224, 128, 0)
BAD = (192, 0, 0)


def log(*a):
    print("panda-lcd:", *a, flush=True)


def shell_conf(path):
    out = {}
    try:
        lines = open(path).read().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if "=" not in line or line.startswith("#"):
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] in "\"'" and v[:1] in v[1:]:
            v = v[1:v.index(v[0], 1)]
        else:
            v = v.split(" #")[0].strip()
        out[k.strip()] = v
    return out


# --- fonts -----------------------------------------------------------------------------
class Fonts:
    def __init__(self, base=FONTS):
        f = lambda name, size: ImageFont.truetype(os.path.join(base, name), size)
        self.ui = f("tahoma.ttf", 11)
        self.bold = f("tahomabd.ttf", 11)
        self.time = f("VT323-Regular.ttf", 30)       # 12 px per character
        self.marq = f("VT323-Regular.ttf", 20)       # 8 px
        self.small = f("VT323-Regular.ttf", 15)      # 6 px
        self.term = f("VT323-Regular.ttf", 15)


# --- drawing primitives -------------------------------------------------------------
class Painter:
    """ImageDraw plus Win98 widgets. Every tappable thing registers a hit box."""

    def __init__(self, fonts):
        self.im = Image.new("RGB", (W, H), DESKTOP)
        self.d = ImageDraw.Draw(self.im)
        self.d.fontmode = "1"
        self.f = fonts
        self.hits = []                    # (box, action), last drawn = on top
        self._grad = {}

    def reset(self, bg=DESKTOP):
        self.d.rectangle((0, 0, W - 1, H - 1), fill=bg)
        self.hits = []

    def hit(self, box, action):
        self.hits.append((box, action))

    def text(self, xy, s, font=None, fill=TEXT, width=None, anchor="la"):
        font = font or self.f.ui
        s = str(s)
        if width is not None and font.getlength(s) > width:
            while s and font.getlength(s + "...") > width:
                s = s[:-1]
            s += "..."
        self.d.text(xy, s, font=font, fill=fill, anchor=anchor)
        return font.getlength(s)

    def bevel(self, box, kind="raised", fill=FACE):
        x0, y0, x1, y1 = box
        d = self.d
        if fill is not None:
            d.rectangle(box, fill=fill)
        if kind == "raised":
            outer, inner = (HILIGHT, DARK), (LIGHT, SHADOW)
        elif kind == "sunken":
            outer, inner = (SHADOW, HILIGHT), (DARK, LIGHT)
        elif kind == "thin-sunken":
            outer, inner = (SHADOW, HILIGHT), None
        elif kind == "thin-raised":
            outer, inner = (HILIGHT, SHADOW), None
        elif kind == "default":           # the default button: black frame + raised
            d.rectangle(box, outline=DARK)
            self.bevel((x0 + 1, y0 + 1, x1 - 1, y1 - 1), "raised", None)
            return
        else:
            return
        d.line((x0, y1 - 1, x0, y0, x1 - 1, y0), fill=outer[0])
        d.line((x0, y1, x1, y1, x1, y0), fill=outer[1])
        if inner:
            d.line((x0 + 1, y1 - 2, x0 + 1, y0 + 1, x1 - 2, y0 + 1), fill=inner[0])
            d.line((x0 + 1, y1 - 1, x1 - 1, y1 - 1, x1 - 1, y0 + 1), fill=inner[1])

    def gradient(self, box, a, b):
        x0, y0, x1, y1 = box
        key = (x1 - x0 + 1, a, b)
        g = self._grad.get(key)
        if g is None:
            n = key[0]
            g = Image.new("RGB", (n, 1))
            g.putdata([tuple(int(a[c] + (b[c] - a[c]) * i / max(1, n - 1)) for c in range(3)) for i in range(n)])
            self._grad[key] = g
        self.im.paste(g.resize((key[0], y1 - y0 + 1)), (x0, y0))

    def window(self, box, title, icon=None, active=True, close=None, dialog=False):
        """A window frame (a dialog has only the close button); returns the client box."""
        x0, y0, x1, y1 = box
        self.bevel(box, "raised")
        tb = (x0 + 3, y0 + 3, x1 - 3, y0 + 17)
        self.gradient(tb, *((TITLE_A, TITLE_B) if active else (TITLE_OFF_A, TITLE_OFF_B)))
        tx = x0 + 5
        if icon:
            icon(self, tx, y0 + 4)
            tx += 17
        nbtn = 1 if dialog else 3
        self.text((tx, y0 + 5), title, self.f.bold, HILIGHT, width=x1 - 5 - 16 * nbtn - tx)
        bx = x1 - 5 - 13
        self.caption_button((bx, y0 + 5, bx + 12, y0 + 15), "x")
        if close is not None:
            self.hit((bx - 4, y0, x1, y0 + 20), close)
        if not dialog:
            self.caption_button((bx - 16, y0 + 5, bx - 4, y0 + 15), "max")
            self.caption_button((bx - 29, y0 + 5, bx - 17, y0 + 15), "min")
        return (x0 + 4, y0 + 20, x1 - 4, y1 - 4)

    def caption_button(self, box, glyph):
        self.bevel(box, "raised")
        x0, y0, x1, y1 = box
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        d = self.d
        if glyph == "x":
            d.line((cx - 3, cy - 3, cx + 2, cy + 2), fill=TEXT)
            d.line((cx - 2, cy - 3, cx + 3, cy + 2), fill=TEXT)
            d.line((cx + 2, cy - 3, cx - 3, cy + 2), fill=TEXT)
            d.line((cx + 3, cy - 3, cx - 2, cy + 2), fill=TEXT)
        elif glyph == "max":
            d.rectangle((cx - 4, cy - 3, cx + 3, cy + 3), outline=TEXT)
            d.line((cx - 4, cy - 2, cx + 3, cy - 2), fill=TEXT)
        else:
            d.rectangle((cx - 3, cy + 2, cx + 2, cy + 3), fill=TEXT)

    def button(self, box, label, action=None, default=False, pressed=False, icon=None, font=None):
        self.bevel(box, "sunken" if pressed else ("default" if default else "raised"))
        x0, y0, x1, y1 = box
        off = 1 if pressed else 0
        if icon:
            icon(self, (x0 + x1) // 2 - 6 + off, (y0 + y1) // 2 - 6 + off)
        else:
            self.text(((x0 + x1) // 2 + off, (y0 + y1) // 2 + off), label, font or self.f.ui, anchor="mm")
        if action:
            self.hit(box, action)

    def radio(self, xy, label, on, action):
        x, y = xy
        d = self.d
        d.ellipse((x, y, x + 11, y + 11), fill=FIELD)
        d.arc((x, y, x + 11, y + 11), 45, 225, fill=SHADOW)
        d.arc((x + 1, y + 1, x + 10, y + 10), 45, 225, fill=DARK)
        d.arc((x, y, x + 11, y + 11), 225, 405, fill=HILIGHT)
        d.arc((x + 1, y + 1, x + 10, y + 10), 225, 405, fill=LIGHT)
        if on:
            d.ellipse((x + 4, y + 4, x + 7, y + 7), fill=TEXT)
        w = self.text((x + 17, y), label)
        self.hit((x - 2, y - 2, x + 20 + int(w), y + 13), action)   # rows are 16 px apart

    def statusbar(self, y, x0, x1, cells):
        """cells: [(text, width or None)], None = the rest."""
        fixed = sum(w for _, w in cells if w)
        rest = (x1 - x0 + 1) - fixed - 2 * (len(cells) - 1)
        x = x0
        for t, w in cells:
            w = w or rest
            self.bevel((x, y, x + w - 1, y + 15), "thin-sunken")
            self.text((x + 4, y + 2), t, width=w - 8)
            x += w + 2


# --- icons (16x16 or smaller, drawn) ------------------------------------------------------
def icon_panda(p, x, y):
    d = p.d
    d.ellipse((x, y, x + 4, y + 4), fill=TEXT)
    d.ellipse((x + 9, y, x + 13, y + 4), fill=TEXT)
    d.ellipse((x + 1, y + 2, x + 12, y + 13), fill=HILIGHT, outline=TEXT)
    d.ellipse((x + 3, y + 5, x + 5, y + 8), fill=TEXT)
    d.ellipse((x + 8, y + 5, x + 10, y + 8), fill=TEXT)
    d.point((x + 6, y + 10), fill=TEXT)
    d.point((x + 7, y + 10), fill=TEXT)


def icon_speaker(p, x, y, fill=TEXT):
    d = p.d
    d.polygon((x + 1, y + 4, x + 4, y + 4, x + 7, y + 1, x + 7, y + 11, x + 4, y + 8, x + 1, y + 8), fill=fill)
    d.arc((x + 6, y + 3, x + 11, y + 9), -60, 60, fill=fill)


def icon_speaker_w(p, x, y):
    icon_speaker(p, x, y, HILIGHT)


def icon_radio(p, x, y):
    d = p.d
    d.line((x + 2, y + 4, x + 10, y + 1), fill=TEXT)
    d.rectangle((x, y + 4, x + 13, y + 11), fill=BAD, outline=TEXT)
    d.ellipse((x + 2, y + 5, x + 6, y + 9), fill=TEXT)
    d.line((x + 8, y + 6, x + 11, y + 6), fill=HILIGHT)
    d.line((x + 8, y + 8, x + 11, y + 8), fill=HILIGHT)


def icon_radio_w(p, x, y):
    icon_radio(p, x, y)


def icon_bt(p, x, y, fill=TITLE_A):
    d = p.d
    pts = (x + 1, y + 3, x + 7, y + 8, x + 4, y + 11, x + 4, y, x + 7, y + 3, x + 1, y + 8)
    d.line(pts, fill=fill, width=1)


def icon_bt_w(p, x, y):
    icon_bt(p, x + 2, y + 1, HILIGHT)


def icon_net(p, x, y):
    d = p.d
    d.rectangle((x, y, x + 7, y + 5), fill=(0, 170, 170), outline=TEXT)
    d.rectangle((x + 6, y + 6, x + 13, y + 11), fill=(0, 170, 170), outline=TEXT)
    d.line((x + 3, y + 6, x + 3, y + 9, x + 5, y + 9), fill=TEXT)


def icon_gear(p, x, y):
    d = p.d
    d.line((x + 6, y, x + 6, y + 12), fill=TEXT, width=2)
    d.line((x, y + 6, x + 12, y + 6), fill=TEXT, width=2)
    d.ellipse((x + 2, y + 2, x + 10, y + 10), fill=SHADOW, outline=TEXT)
    d.ellipse((x + 5, y + 5, x + 7, y + 7), fill=HILIGHT)


def icon_power(p, x, y):
    d = p.d
    d.rectangle((x, y, x + 12, y + 12), fill=TITLE_A)
    d.arc((x + 2, y + 3, x + 10, y + 11), -60, 240, fill=(255, 255, 0))
    d.line((x + 6, y + 2, x + 6, y + 6), fill=(255, 255, 0))


def icon_wifi(n):
    def draw(p, x, y):
        for i in range(4):
            h = 2 + i * 2
            p.d.rectangle((x + i * 3, y + 9 - h, x + i * 3 + 1, y + 9), fill=TEXT if i < n else SHADOW)
    return draw


def icon_wired(up):
    def draw(p, x, y):
        d = p.d
        d.rectangle((x + 1, y + 1, x + 8, y + 6), fill=(0, 170, 170) if up else SHADOW, outline=TEXT)
        d.line((x + 4, y + 7, x + 4, y + 10), fill=TEXT)
        d.line((x, y + 10, x + 9, y + 10), fill=TEXT)
        if not up:
            d.line((x, y, x + 9, y + 9), fill=BAD)
            d.line((x + 9, y, x, y + 9), fill=BAD)
    return draw


def icon_big(kind):
    def draw(p, x, y):
        d = p.d
        if kind == "err":
            d.ellipse((x, y, x + 27, y + 27), fill=(224, 0, 0), outline=(96, 0, 0))
            d.line((x + 8, y + 8, x + 19, y + 19), fill=HILIGHT, width=3)
            d.line((x + 19, y + 8, x + 8, y + 19), fill=HILIGHT, width=3)
        else:
            d.ellipse((x, y, x + 27, y + 27), fill=FIELD, outline=TEXT)
            if kind == "info":
                d.ellipse((x + 2, y + 2, x + 25, y + 25), fill=(0, 0, 255))
            glyph, col = {"info": ("i", HILIGHT), "ask": ("?", TITLE_A)}.get(kind, ("!", TEXT))
            d.text((x + 14, y + 14), glyph, font=p.f.bold, fill=col, anchor="mm")
    return draw


def icon_transport(kind):
    def draw(p, x, y):
        d = p.d
        if kind == "play":
            d.polygon((x + 3, y + 1, x + 3, y + 11, x + 10, y + 6), fill=TEXT)
        elif kind == "pause":
            d.rectangle((x + 2, y + 1, x + 4, y + 11), fill=TEXT)
            d.rectangle((x + 7, y + 1, x + 9, y + 11), fill=TEXT)
        elif kind == "next":
            d.polygon((x - 1, y + 1, x - 1, y + 11, x + 5, y + 6), fill=TEXT)
            d.polygon((x + 5, y + 1, x + 5, y + 11, x + 11, y + 6), fill=TEXT)
            d.rectangle((x + 12, y + 1, x + 13, y + 11), fill=TEXT)
        elif kind == "prev":
            d.rectangle((x - 1, y + 1, x, y + 11), fill=TEXT)
            d.polygon((x + 7, y + 1, x + 7, y + 11, x + 1, y + 6), fill=TEXT)
            d.polygon((x + 13, y + 1, x + 13, y + 11, x + 7, y + 6), fill=TEXT)
    return draw


def grey(icon):
    """A disabled button's icon: drawn, then recoloured grey with a white emboss."""
    def draw(p, x, y):
        tmp = Painter.__new__(Painter)
        tmp.im = Image.new("RGB", (18, 16), FACE)
        tmp.d = ImageDraw.Draw(tmp.im)
        tmp.f = p.f
        icon(tmp, 2, 2)
        mask = tmp.im.convert("L").point(lambda v: 255 if v < 100 else 0)
        p.im.paste(HILIGHT, (x - 1, y - 1), mask)
        p.im.paste(SHADOW, (x - 2, y - 2), mask)
    return draw


# --- the framebuffer ---------------------------------------------------------------------
class Framebuffer:
    """mmap of /dev/fbN; 16 bpp (RGB565, little-endian) or 32 bpp (XRGB8888)."""

    BAND = 16      # rows compared and copied together

    def __init__(self, dev):
        name = os.path.basename(dev)
        sysfs = f"/sys/class/graphics/{name}"
        self.bpp = int(open(f"{sysfs}/bits_per_pixel").read())
        self.stride = int(open(f"{sysfs}/stride").read())
        vw, vh = (int(v) for v in open(f"{sysfs}/virtual_size").read().split(","))
        if (vw, vh) != (W, H):
            raise SystemExit(f"{dev} is {vw}x{vh}, the panel UI is {W}x{H} (rotate in config.txt?)")
        if self.bpp not in (16, 32):
            raise SystemExit(f"{dev}: {self.bpp} bpp isn't supported")
        self.fd = os.open(dev, os.O_RDWR)
        self.mem = mmap.mmap(self.fd, self.stride * H)
        self.front = None
        r5 = [v & 0xF8 for v in range(256)]
        self.lut_hi = (r5, [v >> 5 for v in range(256)])
        self.lut_lo = ([(v << 3) & 0xE0 for v in range(256)], [v >> 3 for v in range(256)])

    def pack(self, im):
        if self.bpp == 32:
            return im.tobytes("raw", "BGRX")
        r, g, b = im.split()
        hi = ImageChops.add(r.point(self.lut_hi[0]), g.point(self.lut_hi[1]))
        lo = ImageChops.add(g.point(self.lut_lo[0]), b.point(self.lut_lo[1]))
        return Image.merge("LA", (lo, hi)).tobytes()

    def show(self, im, force=False):
        bpp = self.bpp // 8
        for y0 in range(0, H, self.BAND):
            box = (0, y0, W, min(H, y0 + self.BAND))
            part = im.crop(box)
            if not force and self.front is not None:
                bb = ImageChops.difference(part, self.front.crop(box)).getbbox()
                if bb is None:
                    continue
                x0, x1 = bb[0], bb[2]
                part = part.crop((x0, 0, x1, part.height))
            else:
                x0 = 0
            data = self.pack(part)
            row = part.width * bpp
            for i in range(part.height):
                off = (y0 + i) * self.stride + x0 * bpp
                self.mem[off:off + row] = data[i * row:(i + 1) * row]
        self.front = im.copy()


class PngSink:
    """--render-test: frames go to PNG files (scaled 2x) instead."""

    def __init__(self, outdir):
        self.outdir = outdir
        self.front = None

    def show(self, im, force=False, name="frame"):
        im.resize((W * 2, H * 2), Image.NEAREST).save(os.path.join(self.outdir, name + ".png"))
        self.front = im.copy()


class Backlight:
    def __init__(self, name):
        self.path = f"/sys/class/backlight/{name}" if name else None
        self.on = True

    def set(self, on):
        if on == self.on or not self.path:
            self.on = on
            return
        self.on = on
        try:
            with open(self.path + "/bl_power", "w") as f:
                f.write("0" if on else "4")
        except OSError:
            try:
                mx = open(self.path + "/max_brightness").read().strip() if on else "0"
                with open(self.path + "/brightness", "w") as f:
                    f.write(mx)
            except OSError as e:
                log("backlight:", e)


# --- the web page's API -------------------------------------------------------------------
class Api:
    def __init__(self, base=API):
        self.base = base
        self.status = {}
        self.status_at = 0.0          # time.monotonic() of the snapshot
        self.cb = None

    def post(self, path, body, timeout=30):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Panda-Panel": "1",
                                              "Host": "localhost"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return json.load(e)
            except ValueError:
                return {"error": f"HTTP {e.code}"}
        except (OSError, ValueError) as e:
            return {"error": str(e)}

    def get(self, path, timeout=20):
        req = urllib.request.Request(self.base + path, headers={"Host": "localhost"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except (OSError, ValueError) as e:
            return {"error": str(e)}

    def events(self):
        """Read /api/events forever (reconnects); every snapshot goes to self.cb."""
        while True:
            try:
                req = urllib.request.Request(self.base + "/api/events", headers={"Host": "localhost"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = []
                    for raw in r:
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif not line and data:
                            try:
                                snap = json.loads("\n".join(data))
                            except ValueError:
                                snap = None
                            data = []
                            if isinstance(snap, dict) and "error" not in snap:
                                self.status, self.status_at = snap, time.monotonic()
                                if self.cb:
                                    self.cb()
            except (OSError, ValueError) as e:
                log("events:", e)
            time.sleep(3)


# --- input ----------------------------------------------------------------------------
class Input:
    """evdev: the buttons (grabbed) and the touchscreen. Events go into a queue:
    ("key", name), ("down", x, y), ("move", x, y), ("up", x, y), ("raw", rx, ry)."""

    KEYS = {164: "play", 163: "next", 114: "down", 115: "up"}   # KEY_PLAYPAUSE, _NEXTSONG, _VOLUME*

    def __init__(self, q, keys_name, touch_name, cal):
        import evdev
        self.evdev = evdev
        self.q = q
        self.keys_name, self.touch_name = keys_name, touch_name
        self.cal = cal
        self.raw_mode = False

    def find(self, name):
        for path in self.evdev.list_devices():
            try:
                dev = self.evdev.InputDevice(path)
            except OSError:
                continue
            if dev.name == name:
                return dev
            dev.close()
        return None

    def run(self):
        e = self.evdev.ecodes
        while True:
            devs = {}
            keys = self.find(self.keys_name) if self.keys_name else None
            touch = self.find(self.touch_name) if self.touch_name else None
            if keys:
                try:
                    keys.grab()
                except OSError as err:
                    log("can't grab the buttons:", err)
                devs[keys.fd] = keys
            if touch:
                devs[touch.fd] = touch
            if not devs:
                time.sleep(10)
                continue
            log("input:", ", ".join(d.name for d in devs.values()))
            x = y = 0
            down = False
            try:
                while True:
                    r, _, _ = select.select(list(devs), [], [], 30)
                    for fd in r:
                        dev = devs[fd]
                        for ev in dev.read():
                            if dev is keys and ev.type == e.EV_KEY and ev.value == 1:
                                if ev.code in self.KEYS:
                                    self.q.put(("key", self.KEYS[ev.code]))
                            elif dev is touch:
                                if ev.type == e.EV_ABS and ev.code == e.ABS_X:
                                    x = ev.value
                                elif ev.type == e.EV_ABS and ev.code == e.ABS_Y:
                                    y = ev.value
                                elif ev.type == e.EV_KEY and ev.code == e.BTN_TOUCH:
                                    down = bool(ev.value)
                                    if self.raw_mode:
                                        if not down:
                                            self.q.put(("raw", x, y))
                                    else:
                                        self.q.put(("down" if down else "up",) + self.cal.map(x, y))
                                elif ev.type == e.EV_SYN and down and not self.raw_mode:
                                    self.q.put(("move",) + self.cal.map(x, y))
            except OSError as err:
                log("input device gone:", err)
                for d in devs.values():
                    d.close()
                time.sleep(2)


class Calibration:
    """Affine raw ADC -> pixel: x = a*rx + b*ry + c, y = d*rx + e*ry + f."""

    POINTS = ((32, 24), (288, 120), (160, 216))

    def __init__(self, path=CAL_FILE):
        self.path = path
        self.m = None
        try:
            with open(path) as f:
                self.m = [float(v) for v in json.load(f)["matrix"]][:6]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if not self.m or len(self.m) != 6:
            # Until calibrated: the STMPE610's 12-bit range stretched over the screen.
            self.m = [W / 4096, 0, 0, 0, H / 4096, 0]
            self.ok = False
        else:
            self.ok = True

    def map(self, rx, ry):
        a, b, c, d, e, f = self.m
        x = min(W - 1, max(0, round(a * rx + b * ry + c)))
        y = min(H - 1, max(0, round(d * rx + e * ry + f)))
        return x, y

    @staticmethod
    def solve(raw, scr):
        """Three raw points and their screen points -> the 6 coefficients."""
        (x1, y1), (x2, y2), (x3, y3) = raw
        det = x1 * (y2 - y3) - y1 * (x2 - x3) + (x2 * y3 - x3 * y2)
        if abs(det) < 1e-6:
            raise ValueError("the three taps are in a line")
        out = []
        for k in (0, 1):
            s1, s2, s3 = (p[k] for p in scr)
            a = (s1 * (y2 - y3) - y1 * (s2 - s3) + (s2 * y3 - s3 * y2)) / det
            b = (x1 * (s2 - s3) - s1 * (x2 - x3) + (x2 * s3 - x3 * s2)) / det
            c = (x1 * (y2 * s3 - y3 * s2) - y1 * (x2 * s3 - x3 * s2) + s1 * (x2 * y3 - x3 * y2)) / det
            out += [a, b, c]
        return out

    def save(self, m):
        self.m, self.ok = m, True
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".new"
        with open(tmp, "w") as f:
            json.dump({"matrix": m, "made": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)
        os.replace(tmp, self.path)


# --- spectrum (panda-spectrum) ----------------------------------------------------------
class Spectrum:
    """Starts panda-spectrum while wanted; it prints 24 bytes (bands, 0-255) per frame."""

    BANDS = 24

    def __init__(self, cb):
        self.cb = cb
        self.proc = None
        self.bands = bytes(self.BANDS)
        self.lock = threading.Lock()
        self.available = os.access(SPECTRUM, os.X_OK)

    def want(self, on):
        with self.lock:
            if on and self.proc is None and self.available:
                env = dict(os.environ, PIPEWIRE_RUNTIME_DIR="/run/speaker", XDG_RUNTIME_DIR="/run/speaker")
                try:
                    self.proc = subprocess.Popen([SPECTRUM], stdout=subprocess.PIPE,
                                                 stderr=subprocess.DEVNULL, env=env)
                except OSError as e:
                    log("spectrum:", e)
                    self.available = False
                    return
                threading.Thread(target=self.read, args=(self.proc,), daemon=True).start()
            elif not on and self.proc is not None:
                self.proc.terminate()
                self.proc = None
                self.bands = bytes(self.BANDS)

    def read(self, proc):
        while True:
            b = proc.stdout.read(self.BANDS)
            if not b or len(b) < self.BANDS:
                break
            self.bands = b
            self.cb()
        with self.lock:
            if self.proc is proc:
                self.proc = None
                self.bands = bytes(self.BANDS)


# --- the UI ---------------------------------------------------------------------------------
def fmt_time(s):
    if s is None:
        return "--:--"
    s = max(0, int(s))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def vol_db(v):
    if v is None:
        return ""
    return "mute" if v <= 0.001 else f"{60 * math.log10(v):.0f} dB"


class Panel:
    def __init__(self, fonts, sink, api, board, test=False):
        self.p = Painter(fonts)
        self.sink = sink
        self.api = api
        self.board = board
        self.test = test
        self.q = queue.Queue()
        self.screen = "now"            # now | radio | net | bt | calibrate | saver | crash
        self.menu = None               # None | "start" | "radio"
        self.dialog = None             # None | "shut" | "settings" | "pair" | "neterr" | "msg"
        self.msg = ""
        self.shut_pick = "off"
        self.favorites = []
        self.last_input = time.monotonic()
        self.last_play = time.monotonic()
        self.osd_until = 0.0
        self.crash = None              # (service, until)
        self.prev_services = {}
        self.marq = 0
        self.vol_drag = None           # volume while dragging the slider
        self.pressed = None            # hit box under the finger
        self.backlight = Backlight(board.get("LCD_BACKLIGHT"))
        self.cal = Calibration()
        self.cal_raw = []
        self.neterr_dismissed = False
        self.net_seen_up = False         # no "connection lost" while booting
        self.pair_quiet_until = 0.0      # after Stop, until the status catches up
        self.vol_box = (0, 0, -1, -1)
        self.swallow = False
        self.stars = [[(i * 7919 % 200 - 100) / 100, (i * 104729 % 200 - 100) / 100, (i * 31 % 97) / 97 + 0.03,
                       i % 17 == 0] for i in range(60)]
        self.spectrum = Spectrum(lambda: self.q.put(("spectrum",)))
        self.input = None
        if not self.cal.ok and board.get("LCD_TOUCH_NAME") and not test:
            self.screen = "calibrate"

    # --- data from the status --------------------------------------------------------
    def st(self):
        return self.api.status or {}

    def current(self):
        """The stream to show: the audible one, else a paused/muted one, else None."""
        streams = (self.st().get("audio") or {}).get("streams") or []
        live = [x for x in streams if x.get("state") == "running"]
        for group in ([x for x in live if not x.get("mute")], live, streams):
            if group:
                return group[-1]
        return None

    def track(self, stream):
        st = self.st()
        if stream is None:
            return {"source": None, "playing": False, "elapsed": None, "duration": None, "from": "",
                    "mute": False}
        src = stream.get("source")
        np = (st.get("nowplaying") or {}).get(src) or {}
        t = {"source": src, "title": np.get("title"), "artist": np.get("artist"), "album": np.get("album"),
             "from": np.get("client") or stream.get("device") or "", "playing": stream.get("playing"),
             "elapsed": stream.get("elapsed"), "duration": stream.get("duration"),
             "rate": stream.get("rate"), "format": stream.get("format") or "", "mute": stream.get("mute")}
        if src == "Bluetooth":
            bt = (st.get("bt") or {}).get("track") or {}
            t.update(title=bt.get("title"), artist=bt.get("artist"), album=bt.get("album"))
        elif src == "Radio":
            r = st.get("radio") or {}
            t.update(artist=(r.get("station") or {}).get("name"), title=r.get("title"),
                     rate=r.get("rate") or t["rate"])
            t["from"] = (r.get("station") or {}).get("name") or "internet radio"
            if r.get("paused"):
                t["playing"] = False
        if t["playing"] is None:
            t["playing"] = stream.get("state") == "running" and not stream.get("mute")
        if t["elapsed"] is not None and t["playing"]:
            t["elapsed"] += time.monotonic() - self.api.status_at
        return t

    def master(self):
        return (self.st().get("audio") or {}).get("sink") or {}

    def name(self):
        return (self.st().get("host") or {}).get("speaker_name") or "Panda"

    def sources_text(self):
        host = self.st().get("host") or {}
        return "AirPlay or Bluetooth" if host.get("spotify") is False else "Spotify, AirPlay or Bluetooth"

    # --- drawing ---------------------------------------------------------------------------
    def render(self):
        p = self.p
        if self.screen == "saver":
            self.draw_saver()
            return
        if self.screen == "calibrate":
            self.draw_calibrate()
            return
        if self.screen == "crash":
            self.draw_crash()
            return
        p.reset(DESKTOP)
        modal = self.dialog is not None
        if self.screen == "radio":
            self.draw_radio(not modal)
        elif self.screen == "net":
            self.draw_net(not modal)
        elif self.screen == "bt":
            self.draw_bt(not modal)
        else:
            self.draw_now(not modal and self.menu is None)
        self.draw_taskbar()
        if self.menu:
            self.draw_menu()
        if modal:
            p.hits = []                   # only the dialog takes taps
            self.draw_dialog()
        if time.monotonic() < self.osd_until:
            self.draw_osd()

    def draw_taskbar(self):
        p = self.p
        d = p.d
        d.rectangle((0, 218, W - 1, H - 1), fill=FACE)
        d.line((0, 218, W - 1, 218), fill=LIGHT)
        d.line((0, 219, W - 1, 219), fill=HILIGHT)
        sb = (2, 221, 54, 238)
        p.bevel(sb, "sunken" if self.menu else "raised")
        icon_panda(p, 5, 223)
        p.text((22, 224), "Start", p.f.bold)
        p.hit((0, 219, 56, H - 1), ("start",))
        # Tray: network, Bluetooth, clock.
        tray = (252, 221, 317, 238)
        p.bevel(tray, "thin-sunken")
        self.net_icon()(p, 256, 225)
        bt = self.st().get("bt") or {}
        if not bt.get("error"):
            connected = any(x.get("connected") for x in bt.get("devices") or [])
            icon_bt(p, 271, 224, TITLE_A if connected else SHADOW)
        p.text((313, 230), time.strftime("%H:%M"), anchor="rm")
        p.hit(tray, ("open", "net"))
        # The task button: the open window.
        label, icon = {"radio": ("Radio - Presets", icon_radio), "net": ("Network", icon_net),
                       "bt": ("Bluetooth", None)}.get(self.screen, (self.now_title(), icon_speaker))
        tb = (58, 221, 248, 238)
        p.bevel(tb, "sunken", LIGHT)
        x = 62
        if icon:
            icon(p, x, 224)
            x += 16
        else:
            icon_bt(p, x + 2, 224)
            x += 14
        p.text((x, 224), label, p.f.bold, width=tb[2] - x - 3)

    def net_icon(self):
        st = self.st()
        wifi = st.get("wifi") or {}
        links = ((st.get("net") or {}).get("links")) or {}
        sig = wifi.get("signal") if isinstance(wifi, dict) else None
        if wifi.get("connected") or wifi.get("ssid"):
            bars = 4 if (sig or -100) > -55 else 3 if (sig or -100) > -67 else 2 if (sig or -100) > -78 else 1
            return icon_wifi(bars)
        return icon_wired(any(v.get("state") == "up" for v in links.values()))

    def now_title(self):
        t = self.track(self.current())
        return f"Now Playing - {t['source']}" if t["source"] else "Now Playing"

    def draw_now(self, active):
        p = self.p
        d = p.d
        cur = self.current()
        t = self.track(cur)
        cl = p.window((2, 2, 317, 215), self.now_title(), icon_speaker_w, active, ("saver",))
        x0, y0, x1, y1 = cl
        # The green display.
        lcd = (x0, y0, x1, y0 + 76)
        p.bevel(lcd, "sunken", LCD_BG)
        lx, ly = x0 + 6, y0 + 4
        p.text((lx, ly), fmt_time(t.get("elapsed")) if t["source"] else "--:--", p.f.time, LCD_FG)
        if not t["source"]:
            state = "STOP"
        elif t.get("mute"):
            state = "MUTED"
        else:
            state = "> PLAY" if t["playing"] else "|| PAUSE"
        p.text((lx, ly + 34), state, p.f.small, LCD_FG)
        p.text((lx, ly + 50), (t["source"] or "IDLE").upper(), p.f.small, LCD_DIM)
        mx = x0 + 92
        mw = x1 - 6 - mx
        chars = int(mw // 8)
        if t["source"]:
            line = " - ".join(s for s in (t.get("artist"), t.get("title")) if s) or t["source"]
        else:
            line = f'Nothing is playing. Pick "{self.name()}" in {self.sources_text()}.'
        if len(line) > chars:
            loop = line + "   ***   "
            i = self.marq % len(loop)
            line = (loop + loop)[i:i + chars]
        p.text((mx, ly), line, p.f.marq, LCD_FG)
        if t["source"]:
            rate = t.get("rate")
            bits = {"S16LE": "16BIT", "S24LE": "24BIT", "S32LE": "32BIT", "F32LE": "FLOAT"}.get(
                str(t.get("format")).replace("_", ""), "")
            fmt = " ".join(s for s in ((f"{rate / 1000:g}KHZ" if rate else ""), bits) if s)
            w = p.text((mx, ly + 22), fmt + " ", p.f.small, LCD_FG) if fmt else 0
            p.text((mx + w, ly + 22), "STEREO", p.f.small, LCD_DIM)
        self.draw_bands((mx, ly + 40, x1 - 6, lcd[3] - 4))
        # Seek bar (display only: the sender owns the position).
        sy = lcd[3] + 4
        p.bevel((x0, sy, x1, sy + 9), "thin-sunken", FIELD)
        dur, el = t.get("duration"), t.get("elapsed")
        if dur and el is not None:
            fx = x0 + 1 + int((x1 - x0 - 2) * min(1.0, el / dur))
            if fx > x0 + 1:
                d.rectangle((x0 + 1, sy + 1, fx, sy + 8), fill=TITLE_A)
        # Transport and volume.
        by = sy + 14
        can_skip = t["source"] in ("Radio", "Bluetooth", "Spotify")
        for i, (kind, act) in enumerate((("prev", ("transport", "prev")),
                                         ("pause" if t["playing"] else "play", ("transport", "toggle")),
                                         ("next", ("transport", "next")))):
            box = (x0 + i * 44, by, x0 + i * 44 + 40, by + 29)
            enabled = bool(t["source"]) and (can_skip or i == 1)
            ic = icon_transport(kind)
            p.button(box, "", act if enabled else None, icon=ic if enabled else grey(ic),
                     pressed=self.pressed == box)
        vx = x0 + 140
        m = self.master()
        vol = self.vol_drag if self.vol_drag is not None else m.get("volume")
        p.text((vx, by - 1), "Volume")
        p.text((x1, by - 1), vol_db(vol), anchor="ra")
        tr = (vx + 5, by + 18, x1 - 5, by + 21)
        p.bevel(tr, "sunken", FIELD)
        if vol is not None:
            tx = tr[0] + int((tr[2] - tr[0]) * min(1.0, max(0.0, vol)))
            p.bevel((tx - 5, by + 10, tx + 5, by + 29), "raised")
        self.vol_box = (vx, by + 8, x1, by + 31)
        p.hit(self.vol_box, ("volume",))
        # Where it comes from.
        fy = by + 38
        if t["source"]:
            w = p.text((x0, fy), "From: ")
            src = t["from"] or "?"
            w2 = p.text((x0 + w, fy), src, p.f.bold, width=x1 - x0 - w - 70)
            p.text((x0 + w + w2, fy), f" ({t['source']})", width=x1 - (x0 + w + w2))
            if t.get("album"):
                p.text((x0, fy + 16), "Album: " + t["album"], width=x1 - x0)
        else:
            p.text((x0, fy), f'Speaker name: "{self.name()}"', width=x1 - x0)
        errs = m.get("errors")
        p.statusbar(y1 - 15, x0, x1, [
            (f"Buffer OK, {errs} xruns" if errs is not None else "Buffer OK", None),
            (vol_db(vol) or "-", 56),
            (("Muted" if t.get("mute") else "Playing" if t["playing"] else "Paused") if t["source"] else "Idle", 58)])

    def draw_bands(self, box):
        x0, y0, x1, y1 = box
        d = self.p.d
        bands = self.spectrum.bands
        n = len(bands)
        bw = max(3, (x1 - x0 + 2) // n)
        h = y1 - y0
        for i, v in enumerate(bands):
            bh = int(v * h / 255) // 2 * 2
            bx = x0 + i * bw
            if bx + bw - 2 > x1:
                break
            for yy in range(0, bh, 2):
                d.line((bx, y1 - yy, bx + bw - 2, y1 - yy), fill=LCD_PEAK if yy > h * 0.7 else LCD_FG)
            if not bh:
                d.line((bx, y1, bx + bw - 2, y1), fill=LCD_DIM)

    def draw_menu(self):
        p = self.p
        items = [("now", "Now Playing", icon_speaker), ("radio", "Radio", icon_radio),
                 ("bt", "Bluetooth", None), ("net", "Network", icon_net),
                 ("settings", "Settings", icon_gear), None, ("shut", "Shut Down...", icon_power)]
        h = sum(22 if it else 6 for it in items) + 4
        box = (2, 219 - h, 162, 219)
        p.bevel(box, "raised")
        band = (5, box[1] + 3, 22, box[3] - 3)
        p.gradient(band, TITLE_A, TITLE_B)
        lbl = Image.new("RGB", (band[3] - band[1] + 1, band[2] - band[0] + 1))
        ld = ImageDraw.Draw(lbl)
        ld.fontmode = "1"
        lbl.paste(p.im.crop(band).rotate(-90, expand=True))
        tw = ld.textlength("Panda", font=p.f.bold)
        ld.text((4, 3), "Panda", font=p.f.bold, fill=HILIGHT)
        ld.text((4 + tw + 2, 3), "98", font=p.f.ui, fill=LIGHT)
        p.im.paste(lbl.rotate(90, expand=True), (band[0], band[1]))
        y = box[1] + 3
        for it in items:
            if it is None:
                p.d.line((25, y + 2, box[2] - 3, y + 2), fill=SHADOW)
                p.d.line((25, y + 3, box[2] - 3, y + 3), fill=HILIGHT)
                y += 6
                continue
            key, label, icon = it
            row = (24, y, box[2] - 3, y + 21)
            hot = self.menu == "radio" and key == "radio"
            if hot:
                p.d.rectangle(row, fill=TITLE_A)
            if icon:
                icon(p, 28, y + 4)
            else:
                icon_bt(p, 31, y + 5)
            p.text((48, y + 5), label, fill=HILIGHT if hot else TEXT)
            if key == "radio":
                p.d.polygon((row[2] - 8, y + 7, row[2] - 8, y + 14, row[2] - 4, y + 10),
                            fill=HILIGHT if hot else TEXT)
            p.hit(row, ("menu", key))
            y += 22
        if self.menu == "radio":
            favs = self.favorites[:8] or [None]
            sh = len(favs) * 20 + 4
            sub = (163, 219 - sh, 318, 219)
            p.bevel(sub, "raised")
            for i, fav in enumerate(favs):
                row = (166, sub[1] + 2 + i * 20, sub[2] - 3, sub[1] + 21 + i * 20)
                if fav is None:
                    p.text((row[0] + 4, row[1] + 4), "No favorites yet", fill=SHADOW)
                    continue
                icon_radio(p, row[0] + 3, row[1] + 4)
                p.text((row[0] + 21, row[1] + 4), fav.get("name") or "?", width=row[2] - row[0] - 24)
                p.hit(row, ("station", fav.get("stationuuid")))

    def draw_radio(self, active):
        p = self.p
        cl = p.window((2, 2, 317, 215), "Radio - Presets", icon_radio_w, active, ("open", "now"))
        x0, y0, x1, y1 = cl
        lb = (x0, y0, x1, y1 - 18)
        p.bevel(lb, "sunken", FIELD)
        r = self.st().get("radio") or {}
        cur = (r.get("station") or {}).get("stationuuid") if r.get("playing") else None
        for i, fav in enumerate(self.favorites[:8]):
            row = (lb[0] + 2, lb[1] + 2 + i * 21, lb[2] - 2, lb[1] + 22 + i * 21)
            sel = fav.get("stationuuid") == cur
            if sel:
                p.d.rectangle(row, fill=TITLE_A)
            icon_radio(p, row[0] + 3, row[1] + 4)
            col = HILIGHT if sel else TEXT
            p.text((row[0] + 22, row[1] + 4), fav.get("name") or "?", fill=col, width=row[2] - row[0] - 70)
            br = fav.get("bitrate")
            if br:
                p.text((row[2] - 4, row[1] + 4), f"{br}k", fill=col, anchor="ra")
            p.hit(row, ("station", fav.get("stationuuid")))
        if not self.favorites:
            p.text((lb[0] + 8, lb[1] + 8), "No favorites yet.", width=lb[2] - lb[0] - 16)
            p.text((lb[0] + 8, lb[1] + 24), "Add stations on the web page:", width=lb[2] - lb[0] - 16)
            p.text((lb[0] + 8, lb[1] + 40), f"http://{self.hostname()}.local/", p.f.bold)
        state = "Paused" if r.get("paused") else "On air" if r.get("playing") else "Stopped"
        p.statusbar(y1 - 15, x0, x1, [(f"{len(self.favorites)} presets, edit on the web page", None),
                                      (state, 60)])

    def hostname(self):
        return (self.st().get("host") or {}).get("hostname") or "panda"

    def draw_net(self, active):
        p = self.p
        st = self.st()
        cl = p.window((2, 2, 317, 215), "Network Properties", icon_net, active, ("open", "now"))
        x0, y0, x1, y1 = cl
        g = (x0, y0 + 6, x1 - 102, y1 - 20)
        p.bevel(g, "thin-sunken", None)
        p.d.rectangle((g[0] + 1, g[1] + 1, g[2] - 1, g[3] - 1), outline=HILIGHT)
        p.d.rectangle((g[0] + 6, g[1] - 6, g[0] + 72, g[1] + 6), fill=FACE)
        p.text((g[0] + 8, g[1] - 6), "Connection")
        net = st.get("net") or {}
        wifi = st.get("wifi") or {}
        addr = next((a["addr"].split("/")[0] for a in net.get("addrs") or [] if a.get("family") == "inet"), "none")
        ifn = next((a["if"] for a in net.get("addrs") or [] if a.get("family") == "inet"), "")
        link = (net.get("links") or {}).get(ifn) or {}
        rows = []
        if wifi.get("ssid") and ifn.startswith("wl"):
            rows += [("Network", wifi.get("ssid")), ("Signal", f"{wifi.get('signal', '?')} dBm")]
        else:
            sp = link.get("speed")
            rows += [("Network", f"Ethernet ({ifn})" if ifn else "not connected"),
                     ("Speed", f"{sp} Mbit/s" if sp and sp != "-1" else "?")]
        up = (st.get("host") or {}).get("uptime") or 0
        sync = st.get("sync") or {}
        rows += [("Address", addr), ("Name", f"{self.hostname()}.local"),
                 ("Uptime", f"{int(up // 86400)} d {int(up // 3600 % 24):02d}:{int(up // 60 % 60):02d}"),
                 ("Clock", f"stratum {sync['stratum']}" if sync.get("stratum") else "not synced")]
        for i, (k, v) in enumerate(rows):
            yy = g[1] + 10 + i * 17
            p.text((g[0] + 8, yy), k, p.f.bold)
            p.text((g[0] + 62, yy), v, width=g[2] - g[0] - 66)
        url = f"http://{self.hostname()}.local/"
        qx, qy = x1 - 96, y0 + 6
        p.bevel((qx, qy, x1, qy + 96), "sunken", FIELD)
        self.draw_qr(url, (qx + 2, qy + 2, x1 - 2, qy + 94))
        p.text(((qx + x1) // 2, qy + 100), "Scan for the", anchor="ma")
        p.text(((qx + x1) // 2, qy + 113), "web page", anchor="ma")
        ok = any(v.get("state") == "up" for v in (net.get("links") or {}).values())
        p.statusbar(y1 - 15, x0, x1, [(url, None), ("Connected" if ok else "No network", 76)])

    def draw_qr(self, text, box):
        try:
            import qrcode
            q = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
            q.add_data(text)
            q.make(fit=True)
            m = q.get_matrix()
        except ImportError:
            self.p.text(((box[0] + box[2]) // 2, (box[1] + box[3]) // 2), "(no qrcode)", anchor="mm")
            return
        n = len(m)
        s = max(1, min(box[2] - box[0] + 1, box[3] - box[1] + 1) // n)
        ox = box[0] + (box[2] - box[0] + 1 - n * s) // 2
        oy = box[1] + (box[3] - box[1] + 1 - n * s) // 2
        for y, row in enumerate(m):
            for x, v in enumerate(row):
                if v:
                    self.p.d.rectangle((ox + x * s, oy + y * s, ox + x * s + s - 1, oy + y * s + s - 1), fill=TEXT)

    def draw_bt(self, active):
        p = self.p
        bt = self.st().get("bt") or {}
        cl = p.window((2, 2, 317, 215), "Bluetooth", icon_bt_w, active, ("open", "now"))
        x0, y0, x1, y1 = cl
        lb = (x0, y0 + 16, x1, y1 - 56)
        p.text((x0, y0 + 1), "Paired phones and computers:")
        p.bevel(lb, "sunken", FIELD)
        devs = [x for x in bt.get("devices") or [] if x.get("paired")]
        for i, dv in enumerate(devs[:6]):
            yy = lb[1] + 3 + i * 18
            icon_bt(p, lb[0] + 5, yy + 1, TITLE_A if dv.get("connected") else SHADOW)
            p.text((lb[0] + 18, yy), dv.get("name") or dv.get("address") or "?",
                   p.f.bold if dv.get("connected") else p.f.ui, width=lb[2] - lb[0] - 100)
            if dv.get("connected"):
                p.text((lb[2] - 5, yy), "connected", anchor="ra")
        if not devs:
            p.text((lb[0] + 6, lb[1] + 4), "None yet.", fill=SHADOW)
        if bt.get("error"):
            p.text((x0, y1 - 50), bt["error"], fill=BAD, width=x1 - x0)
        else:
            p.button((x0, y1 - 48, x0 + 120, y1 - 24), "Pair a device...", ("pair", True), default=True)
            p.text((x0 + 128, y1 - 42), f'Shows up as "{self.name()}"', width=x1 - x0 - 128)
        p.statusbar(y1 - 15, x0, x1, [(f"{len(devs)} paired", None),
                                      ("Pairing on" if bt.get("window") else "Pairing off", 76)])

    def dialog_frame(self, title, icon, lines, buttons, height=None):
        p = self.p
        lh = 14
        h = height or max(40, len(lines) * lh + 8) + 20 + 36
        box = (26, (218 - h) // 2, 293, (218 - h) // 2 + h)
        cl = p.window(box, title, None, True, ("dialog", "cancel"), dialog=True)
        x0, y0, x1, y1 = cl
        tx = x0 + 6
        if icon:
            icon(p, x0 + 6, y0 + 8)
            tx = x0 + 42
        for i, (txt, font) in enumerate(lines):
            p.text((tx, y0 + 8 + i * lh), txt, font or p.f.ui, width=x1 - tx - 4)
        n = len(buttons)
        bw = 72
        bx = (x0 + x1) // 2 - (n * bw + (n - 1) * 8) // 2
        for i, (label, act, default) in enumerate(buttons):
            b = (bx + i * (bw + 8), y1 - 28, bx + i * (bw + 8) + bw, y1 - 5)
            p.button(b, label, act, default=default, pressed=self.pressed == b)
        return cl

    def draw_dialog(self):
        p = self.p
        bt = self.st().get("bt") or {}
        if self.dialog == "pair":
            left = bt.get("window_left") or 0
            self.dialog_frame("Bluetooth Pairing", icon_big("info"), [
                ("Pairing is on.", p.f.bold),
                ("On your phone, open Bluetooth", None),
                (f'and pick "{self.name()}".', None),
                (f"Time left: {left // 60}:{left % 60:02d}", None)],
                [("Stop", ("pair", False), True)])
        elif self.dialog == "neterr":
            self.dialog_frame("Network Error", icon_big("err"), [
                ("The network connection was lost.", None),
                ("AirPlay and radio have stopped.", None),
                ("This closes when the speaker", None),
                ("is back on the network.", None)],
                [("OK", ("dialog", "cancel"), True)])
        elif self.dialog == "shut":
            cl = self.dialog_frame("Shut Down Speaker", icon_power_big, [
                ("What do you want the speaker to do?", None)],
                [("OK", ("dialog", "ok"), True), ("Cancel", ("dialog", "cancel"), False)], height=128)
            for i, (k, label) in enumerate((("off", "Shut down"), ("restart", "Restart"),
                                             ("dark", "Turn off the screen"))):
                p.radio((cl[0] + 42, cl[1] + 26 + i * 16), label, self.shut_pick == k, ("shutpick", k))
        elif self.dialog == "settings":
            self.dialog_frame("Display Settings", icon_big("info"), [
                (f"Screensaver: {SAVER_S // 60} min idle.", None),
                (f"Screen off: {DARK_S // 60} min idle.", None),
                ("Taps land in the wrong place?", None),
                ("Calibrate: tap three crosshairs.", None)],
                [("OK", ("dialog", "cancel"), True), ("Calibrate...", ("calibrate",), False)])
        elif self.dialog == "msg":
            self.dialog_frame("Panda", icon_big("err"), [(line, None) for line in self.msg.split("\n")][:4],
                              [("OK", ("dialog", "cancel"), True)])

    def draw_osd(self):
        p = self.p
        m = self.master()
        v = m.get("volume") or 0
        box = (278, 104, 314, 214)
        p.bevel(box, "raised")
        icon_speaker(p, 290, 108)
        bar = (290, 124, 302, 196)
        p.bevel(bar, "sunken", FIELD)
        top = bar[3] - 2 - int((bar[3] - bar[1] - 4) * min(1.0, v))
        p.d.rectangle((bar[0] + 2, top, bar[2] - 2, bar[3] - 2), fill=TITLE_A)
        p.text((296, 200), f"{round(v * 100)}", anchor="ma")

    def draw_saver(self):
        p = self.p
        p.reset(LCD_BG)
        d = p.d
        for s in self.stars:
            if not self.test:
                s[2] -= 0.012
            if s[2] <= 0.03:
                s[0], s[1], s[2] = (time.monotonic_ns() % 200 - 100) / 100, (time.monotonic_ns() // 7 % 200 - 100) / 100, 1.0
            sx, sy = 160 + s[0] / s[2] * 110, 120 + s[1] / s[2] * 110
            if not (-20 < sx < 340 and -20 < sy < 260):
                s[2] = 0
                continue
            r = max(1, (1 - s[2]) * (9 if s[3] else 2.4))
            if s[3]:
                d.ellipse((sx - r * 1.05, sy - r * 1.15, sx - r * 0.15, sy - r * 0.25), fill=TEXT)
                d.ellipse((sx + r * 0.15, sy - r * 1.15, sx + r * 1.05, sy - r * 0.25), fill=TEXT)
                d.ellipse((sx - r, sy - r, sx + r, sy + r), fill=HILIGHT)
                d.ellipse((sx - r * 0.6, sy - r * 0.35, sx - r * 0.15, sy + r * 0.25), fill=TEXT)
                d.ellipse((sx + r * 0.15, sy - r * 0.35, sx + r * 0.6, sy + r * 0.25), fill=TEXT)
            else:
                v = int(255 * (1 - s[2] * 0.7))
                d.rectangle((sx, sy, sx + r - 1, sy + r - 1), fill=(v, v, v))
        p.hits = [((0, 0, W, H), ("wake",))]

    def draw_calibrate(self):
        p = self.p
        p.reset(FIELD)
        i = len(self.cal_raw)
        p.text((W // 2, 92), "Touch screen calibration", p.f.bold, anchor="mm")
        p.text((W // 2, 112), f"Tap the center of the cross ({i + 1} of 3).", anchor="mm")
        p.text((W // 2, 128), "Press Next to skip.", fill=SHADOW, anchor="mm")
        if i < 3:
            cx, cy = Calibration.POINTS[i]
            p.d.line((cx - 10, cy, cx + 10, cy), fill=BAD)
            p.d.line((cx, cy - 10, cx, cy + 10), fill=BAD)
            p.d.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), outline=BAD)
        p.hits = []

    def draw_crash(self):
        p = self.p
        p.reset(BSOD)
        svc = self.crash[0] if self.crash else "?"
        hd = " PANDA "
        w = p.f.term.getlength(hd)
        p.d.rectangle((W // 2 - w // 2 - 2, 20, W // 2 + w // 2 + 2, 34), fill=FACE)
        p.text((W // 2, 21), hd, p.f.term, BSOD, anchor="ma")
        lines = ["A fatal exception 0E has occurred in", f"{svc}. The service is being", "restarted.", "",
                 " * Playback resumes when your sender", "   reconnects.",
                 " * Press any button to continue _"]
        for i, line in enumerate(lines):
            p.text((12, 50 + i * 16), line, p.f.term, HILIGHT)
        p.hits = [((0, 0, W, H), ("crash-done",))]

    def draw_final(self, kind):
        p = self.p
        if kind == "off":
            p.reset(LCD_BG)
            p.text((W // 2, 108), "It's now safe to turn off", p.f.bold, SAFE, anchor="mm")
            p.text((W // 2, 126), "your speaker.", p.f.bold, SAFE, anchor="mm")
        elif kind == "reboot":
            p.reset(LCD_BG)
            p.text((W // 2, 120), "Restarting...", p.f.bold, SAFE, anchor="mm")
        else:
            p.reset(LCD_BG)

    # --- actions -----------------------------------------------------------------------------
    def act(self, a):
        kind = a[0]
        if kind == "start":
            self.menu = None if self.menu else "start"
        elif kind == "menu":
            key = a[1]
            if key == "radio":
                self.load_favorites()
                self.menu = None if self.menu == "radio" else "radio"
                return
            self.menu = None
            if key in ("now", "net", "bt"):
                self.screen = key
            elif key == "settings":
                self.dialog = "settings"
            elif key == "shut":
                self.shut_pick, self.dialog = "off", "shut"
        elif kind == "open":
            self.menu = None
            if a[1] == "radio":
                self.load_favorites()
            self.screen = a[1]
        elif kind == "station":
            self.menu = None
            if a[1]:
                self.bg(lambda: self.api.post("/api/radio", {"cmd": "play", "uuid": a[1]}))
        elif kind == "transport":
            self.bg(lambda: self.report(self.api.post("/api/transport", {"action": a[1]})))
        elif kind == "pair":
            self.bg(lambda: self.api.post("/api/bt", {"cmd": "pair", "open": a[1]}))
            self.dialog = "pair" if a[1] else None
            if not a[1]:
                self.pair_quiet_until = time.monotonic() + 5
        elif kind == "shutpick":
            self.shut_pick = a[1]
        elif kind == "dialog":
            d, self.dialog = self.dialog, None
            if d == "neterr":
                self.neterr_dismissed = True
            if d == "pair":
                self.bg(lambda: self.api.post("/api/bt", {"cmd": "pair", "open": False}))
                self.pair_quiet_until = time.monotonic() + 5
            if d == "shut" and a[1] == "ok":
                if self.shut_pick == "dark":
                    self.backlight.set(False)
                    self.last_input = time.monotonic() - DARK_S
                else:
                    action = "poweroff" if self.shut_pick == "off" else "reboot"
                    self.bg(lambda: self.api.post("/api/system", {"action": action}))
        elif kind == "calibrate":
            self.dialog = None
            self.start_calibration()
        elif kind == "saver":
            self.screen = "saver"
        elif kind in ("wake", "crash-done"):
            self.screen = "now"
            self.crash = None

    def report(self, res):
        if res.get("error") and res["error"] != "nothing to play":
            self.msg = res["error"]
            self.dialog = "msg"
            self.q.put(("redraw",))

    def bg(self, fn):
        def run():
            fn()
            self.q.put(("redraw",))
        threading.Thread(target=run, daemon=True).start()

    def load_favorites(self):
        def fetch():
            r = self.api.get("/api/radio/favorites")
            favs = r.get("stations") if isinstance(r, dict) else r
            if isinstance(favs, list):
                self.favorites = favs
        self.bg(fetch)

    def volume(self, v, commit=True):
        sink = self.master()
        if "id" not in sink:
            return
        v = min(1.0, max(0.0, v))
        if commit:
            self.vol_drag = None
            self.bg(lambda: self.api.post("/api/volume", {"id": sink["id"], "volume": round(v, 3)}))
            sink["volume"] = v         # shown at once; the next snapshot confirms
        else:
            self.vol_drag = v

    def start_calibration(self):
        self.cal_raw = []
        self.screen = "calibrate"
        if self.input:
            self.input.raw_mode = True

    def end_calibration(self, ok):
        if self.input:
            self.input.raw_mode = False
        if ok:
            try:
                self.cal.save(Calibration.solve(self.cal_raw, Calibration.POINTS))
                self.bg(lambda: self.api.post("/api/system", {"action": "save"}, timeout=90))
            except (ValueError, OSError) as e:
                self.msg, self.dialog = f"Calibration failed: {e}", "msg"
        self.cal_raw = []
        self.screen = "now"

    # --- events --------------------------------------------------------------------------------
    def wake(self):
        """Any input: True if it only woke the screen (then it does nothing else)."""
        now = time.monotonic()
        asleep = not self.backlight.on or self.screen == "saver"
        self.last_input = now
        if asleep:
            self.backlight.set(True)
            if self.screen == "saver":
                self.screen = "now"
            return True
        return False

    def key(self, k):
        if self.wake():
            return
        if self.screen == "calibrate":
            if k == "next":
                self.end_calibration(False)
            return
        if self.screen == "crash":
            self.act(("crash-done",))
            return
        if self.menu:
            self.menu = None
            return
        if self.dialog:
            if k == "play":
                self.act(("dialog", "ok") if self.dialog == "shut" else
                         ("pair", False) if self.dialog == "pair" else ("dialog", "cancel"))
            elif k == "next":
                self.act(("dialog", "cancel"))
            return
        if k == "play":
            self.act(("transport", "toggle"))
        elif k == "next":
            self.act(("transport", "next"))
        elif k in ("down", "up"):
            v = self.master().get("volume")
            if v is not None:
                self.volume(v + (0.05 if k == "up" else -0.05))
                self.osd_until = time.monotonic() + OSD_S

    def touch(self, kind, x, y):
        if kind == "down" and self.wake():
            self.swallow = True
            return
        if kind == "down":
            self.swallow = False
        if self.swallow:
            return
        if kind in ("down", "move") and self.screen == "now" and not self.dialog and not self.menu \
                and (self.vol_drag is not None or (kind == "down" and self.inside(self.vol_box, x, y))):
            x0, x1 = self.vol_box[0] + 5, self.vol_box[2] - 5
            self.volume((x - x0) / (x1 - x0), commit=False)
            return
        if kind == "up" and self.vol_drag is not None:
            self.volume(self.vol_drag)
            return
        hit = None
        for box, action in reversed(self.p.hits):
            if self.inside(box, x, y):
                hit = (box, action)
                break
        if kind == "down":
            self.pressed = hit[0] if hit else None
        elif kind == "up":
            self.pressed = None
            if hit:
                self.act(hit[1])
            elif self.menu:
                self.menu = None

    @staticmethod
    def inside(box, x, y):
        return box[0] <= x <= box[2] and box[1] <= y <= box[3]

    def raw_tap(self, rx, ry):
        self.cal_raw.append((rx, ry))
        if len(self.cal_raw) == 3:
            self.end_calibration(True)

    def check_status(self):
        """Things the status can trigger: crash screen, network error, pairing dialog."""
        st = self.st()
        services = st.get("services") or {}
        for svc in SUPERVISED:
            if services.get(svc) == "crashed" and self.prev_services.get(svc) not in (None, "crashed"):
                self.crash = (svc, time.monotonic() + CRASH_S)
                self.screen = "crash"
                self.backlight.set(True)
        self.prev_services = dict(services)
        links = (st.get("net") or {}).get("links")
        if links is not None:
            up = any(v.get("state") == "up" for v in links.values())
            if not up and self.net_seen_up and not self.neterr_dismissed and self.dialog is None:
                self.dialog = "neterr"
            elif up:
                self.net_seen_up = True
                self.neterr_dismissed = False
                if self.dialog == "neterr":
                    self.dialog = None
        bt = st.get("bt") or {}
        if bt.get("window") and self.dialog is None and time.monotonic() > self.pair_quiet_until:
            self.dialog = "pair"
        elif not bt.get("window") and self.dialog == "pair":
            self.dialog = None
        if any(x.get("state") == "running" for x in (st.get("audio") or {}).get("streams") or []):
            self.last_play = time.monotonic()

    def tick(self):
        now = time.monotonic()
        if self.crash and now > self.crash[1] and self.screen == "crash":
            self.act(("crash-done",))
        idle = now - max(self.last_input, self.last_play)
        if self.screen not in ("calibrate", "crash"):
            if idle > DARK_S:
                self.backlight.set(False)
            elif idle > SAVER_S and self.screen != "saver" and not self.dialog:
                self.menu = None
                self.screen = "saver"
        t = self.track(self.current())
        self.spectrum.want(self.backlight.on and self.screen == "now" and not self.dialog
                           and bool(t["source"]) and bool(t["playing"]))

    # --- main loop -------------------------------------------------------------------------------
    def frame(self):
        if not self.backlight.on:
            return
        self.render()
        self.sink.show(self.p.im)

    def run(self):
        self.api.cb = lambda: self.q.put(("status",))
        threading.Thread(target=self.api.events, daemon=True).start()
        if self.board.get("LCD_KEYS_NAME") or self.board.get("LCD_TOUCH_NAME"):
            try:
                self.input = Input(self.q, self.board.get("LCD_KEYS_NAME"), self.board.get("LCD_TOUCH_NAME"),
                                   self.cal)
                self.input.raw_mode = self.screen == "calibrate"
                threading.Thread(target=self.input.run, daemon=True).start()
            except ImportError:
                log("no python evdev: no buttons or touch")
        self.load_favorites()
        self.frame()
        period = 1 / MARQUEE_HZ
        next_tick = time.monotonic()
        while True:
            timeout = max(0.0, next_tick - time.monotonic())
            try:
                ev = self.q.get(timeout=timeout)
            except queue.Empty:
                ev = ("tick",)
            if ev[0] == "tick":
                next_tick = time.monotonic() + (0.08 if self.screen == "saver" else period)
                self.marq += 1
                self.tick()
            elif ev[0] == "status":
                self.check_status()
            elif ev[0] == "key":
                self.key(ev[1])
            elif ev[0] in ("down", "move", "up"):
                if self.screen == "calibrate":
                    continue
                self.touch(*ev)
            elif ev[0] == "raw":
                if self.wake():
                    continue
                self.raw_tap(ev[1], ev[2])
            # Drain what piled up (a burst of touch moves, spectrum frames) before drawing.
            if self.q.qsize() > 4 and ev[0] in ("move", "spectrum", "status"):
                continue
            self.frame()


def icon_power_big(p, x, y):
    d = p.d
    d.rectangle((x, y, x + 27, y + 27), fill=TITLE_A)
    d.arc((x + 5, y + 6, x + 22, y + 23), -60, 240, fill=(255, 255, 0), width=2)
    d.line((x + 13, y + 3, x + 13, y + 12), fill=(255, 255, 0), width=2)


# --- render test (no board) -------------------------------------------------------------------
SAMPLE = {
    "host": {"hostname": "panda", "speaker_name": "Panda", "spotify": False, "uptime": 274320},
    "audio": {"sink": {"id": 40, "volume": 0.79, "errors": 0},
              "streams": [{"id": 77, "source": "AirPlay", "state": "running", "mute": False, "rate": 44100,
                           "format": "S16LE", "elapsed": 74.0, "duration": 151.0, "playing": True}]},
    "nowplaying": {"AirPlay": {"title": "Roygbiv", "artist": "Boards of Canada",
                               "album": "Music Has the Right to Children", "client": "Fedora desktop"}},
    "radio": {"available": True, "playing": False},
    "bt": {"window": False, "devices": [{"name": "Pixel 7", "paired": True, "connected": True},
                                        {"name": "ThinkPad", "paired": True, "connected": False}]},
    "wifi": {},
    "net": {"addrs": [{"if": "eth0", "family": "inet", "addr": "192.168.1.42/24"}],
            "links": {"eth0": {"state": "up", "speed": "100"}}},
    "sync": {"stratum": "3"},
    "services": {},
}
FAVS = [{"stationuuid": f"{i:08x}-0000-0000-0000-000000000000", "name": n, "bitrate": b}
        for i, (n, b) in enumerate((("SomaFM Groove Salad", 128), ("SomaFM Drone Zone", 128),
                                    ("Radio Paradise Main Mix", 320), ("FIP", 128), ("NTS 1", 128),
                                    ("KEXP 90.3 FM", 128)))]


def render_test(outdir):
    import copy
    os.makedirs(outdir, exist_ok=True)
    here = os.path.dirname(os.path.abspath(__file__))
    fonts_dir = FONTS if os.path.isdir(FONTS) else os.path.join(here, "..", "..", "usr", "share", "panda", "fonts")
    api = Api()
    sink = PngSink(outdir)
    panel = Panel(Fonts(fonts_dir), sink, api, {}, test=True)
    panel.favorites = FAVS
    panel.spectrum.bands = bytes(int(200 * abs(math.sin(i * 0.5)) * (1 - i / 40)) + 20 for i in range(24))
    api.status_at = time.monotonic()

    def shot(name, **state):
        api.status = copy.deepcopy(state.pop("status", SAMPLE))
        api.status_at = time.monotonic()
        panel.screen, panel.menu, panel.dialog, panel.osd_until = "now", None, None, 0
        for k, v in state.items():
            setattr(panel, k, v)
        panel.render()
        sink.show(panel.p.im, name=name)

    idle = copy.deepcopy(SAMPLE)
    idle["audio"]["streams"] = []
    radio = copy.deepcopy(SAMPLE)
    radio["audio"]["streams"] = [{"id": 80, "source": "Radio", "state": "running", "rate": 48000, "format": "F32LE"}]
    radio["radio"] = {"available": True, "playing": True, "station": FAVS[2], "title": "Hiatus Kaiyote - Building a Ladder"}
    paused_bt = copy.deepcopy(SAMPLE)
    paused_bt["audio"]["streams"] = [{"id": 90, "source": "Bluetooth", "state": "idle", "device": "Pixel 7",
                                      "elapsed": 31, "duration": 245, "playing": False}]
    paused_bt["bt"]["track"] = {"title": "Kids", "artist": "MGMT", "status": "paused"}
    pairing = copy.deepcopy(SAMPLE)
    pairing["bt"].update(window=True, window_left=107)
    down = copy.deepcopy(SAMPLE)
    down["net"]["links"]["eth0"]["state"] = "down"
    shot("01-now-airplay")
    shot("02-now-idle", status=idle)
    shot("03-now-radio", status=radio)
    shot("04-now-bluetooth-paused", status=paused_bt)
    shot("05-start-menu", menu="start")
    shot("06-start-radio-submenu", menu="radio")
    shot("07-radio-presets", status=radio, screen="radio")
    shot("08-network", screen="net")
    shot("09-bluetooth", screen="bt")
    shot("10-pairing", status=pairing, screen="bt", dialog="pair")
    shot("11-network-error", status=down, dialog="neterr")
    shot("12-shut-down", dialog="shut")
    shot("13-settings", dialog="settings")
    shot("14-volume-osd", osd_until=time.monotonic() + 9)
    shot("15-calibrate", screen="calibrate")
    shot("16-screensaver", screen="saver")
    shot("17-crash", screen="crash", crash=("speaker-shairport", 0))
    panel.draw_final("off")
    sink.show(panel.p.im, name="18-safe-to-turn-off")
    print(f"wrote {len(os.listdir(outdir))} files to {outdir}")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--render-test":
        render_test(sys.argv[2])
        return
    board = shell_conf("/etc/panda/board.conf")
    dev = board.get("LCD_FB")
    if not dev:
        raise SystemExit("board.conf has no LCD_FB: no front panel on this board")
    for _ in range(30):                  # the fbtft module may still be loading
        if os.path.exists(dev):
            break
        time.sleep(1)
    fb = Framebuffer(dev)
    panel = Panel(Fonts(), fb, Api(), board)

    def final(signum, frame):
        try:
            kind = open(FINAL_FILE).read().strip()
        except OSError:
            kind = ""
        panel.spectrum.want(False)
        panel.draw_final(kind)
        fb.show(panel.p.im, force=True)
        if kind not in ("off", "reboot"):
            panel.backlight.set(False)
        os._exit(0)

    signal.signal(signal.SIGTERM, final)
    panel.run()


if __name__ == "__main__":
    main()
