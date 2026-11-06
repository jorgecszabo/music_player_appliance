// Fake speaker for the static demo of the web control panel (docs/panel-demo.html).
// Stands in for panda-web.py: answers the page's /api/ calls from made-up state,
// so the real index.html/app.js/style.css run unchanged in a browser, offline.
// scripts/build-demo.sh inlines this ahead of app.js.
(() => {
  "use strict";
  const t0 = Date.now();
  const uuid = (n) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;
  const STATIONS = [
    { stationuuid: uuid(1), name: "Radio Swiss Jazz", countrycode: "CH", codec: "MP3", bitrate: 128, tags: "jazz,smooth jazz,swiss" },
    { stationuuid: uuid(2), name: "SomaFM Groove Salad", countrycode: "US", codec: "AAC", bitrate: 128, tags: "ambient,chillout,downtempo,electronic" },
    { stationuuid: uuid(3), name: "FIP", countrycode: "FR", codec: "AAC", bitrate: 192, tags: "eclectic,jazz,rock,world" },
    { stationuuid: uuid(4), name: "BBC Radio 3", countrycode: "GB", codec: "AAC", bitrate: 320, tags: "classical,culture,public radio" },
    { stationuuid: uuid(5), name: "Radio Paradise Main Mix", countrycode: "US", codec: "FLAC", bitrate: 0, tags: "eclectic,rock,listener supported" },
    { stationuuid: uuid(6), name: "KEXP 90.3", countrycode: "US", codec: "AAC", bitrate: 160, tags: "alternative,indie,seattle" },
  ];
  const favs = new Set([uuid(1), uuid(3)]);

  const s = {
    host: { hostname: "panda", speaker_name: "Panda", board: "pandaboard", board_name: "PandaBoard (OMAP4430)",
            board_blurb: "an upcycled 2011 PandaBoard", board_badge: "OMAP4", spotify: true,
            bt_controller: "TI WL1271 (BT 2.1+EDR, onboard)", kernel: "6.18.60-0-pandaboard", alpine: "3.24.0", uptime: 0 },
    cpu: { cores: 2, percent: 14.2, load: [0.31, 0.27, 0.22], temp: 47.5 },
    mem: { total: 1003520, available: 641024, shmem: 186368 },
    net: {
      links: { eth0: { state: "down", speed: "-1" }, wlan0: { state: "up" } },
      addrs: [{ if: "wlan0", family: "inet", addr: "192.168.1.42/24" }, { if: "wlan0", family: "inet6", addr: "fe80::21a:2bff:fe3c:4d5e/64" }],
    },
    audio: {
      sink: { id: 40, name: "alsa_output.platform-sound.HiFi", description: "Built-in audio (TWL6040 headphone jack)",
              volume: 0.72, mute: false, format: "S32LE", rate: 96000, quantum: "1024", errors: "0" },
      output: "alsa_output.platform-sound.HiFi",
      outputs: [{ name: "alsa_output.platform-sound.HiFi", description: "Built-in audio (TWL6040 headphone jack)", usb: false }],
      usb_card: null,
      streams: [{ id: 71, source: "AirPlay", device: "iPhone", codec: "alac", format: "S16LE", rate: 44100,
                  channels: 2, state: "running", errors: "0", volume: 0.85, mute: false, elapsed: 83, playing: true }],
    },
    nowplaying: { AirPlay: { artist: "Nujabes", title: "Aruarian Dance", client: "iPhone", client_ip: "192.168.1.23" } },
    bt: {
      alias: "Panda", window: false, window_left: 0, forgetting: false, track: null,
      devices: [
        { name: "Pixel 7", address: "AC:3E:B1:12:34:56", paired: true, connected: false, path: "/org/bluez/hci0/dev_AC_3E_B1_12_34_56" },
        { name: "ThinkPad X220", address: "60:D8:19:AB:CD:EF", paired: true, connected: false, path: "/org/bluez/hci0/dev_60_D8_19_AB_CD_EF" },
      ],
    },
    wifi: {
      iface: "wlan0", state: "completed", country: "DE", adapter_setting: "auto",
      adapters: [{ name: "wlan0", description: "Realtek RTL8192SU (802.11b/g/n, 2.4 GHz, USB)", driver: "r92su",
                   bands: ["2.4 GHz"], mac: "00:1a:2b:3c:4d:5e", active: true }],
      connection: { ssid: "HomeNet", signal: -58, freq: 2437, speed: 72, ip: "192.168.1.42" },
      saved: [{ ssid: "HomeNet", security: "wpa2" }, { ssid: "Workshop", security: "wpa2/wpa3" }],
    },
    services: Object.fromEntries(["panda-firewall", "panda-audio-init", "speaker-pipewire", "speaker-wireplumber",
      "speaker-soloist", "speaker-shairport", "speaker-radio", "speaker-arbiter", "bluetooth", "panda-bt", "panda-wifi",
      "avahi-daemon", "chronyd", "sshd", "crond", "panda-web"].map((n) => [n, "started"])),
    sync: { leap: "Normal", offset: "+0.000412 seconds", stratum: 3, reference: "time.cloudflare.com" },
    lbu: { changes: 0, files: [] },
    tone: { bass: 2, treble: 0, cap: 13, cap_max: 15, cap_step_db: 2, available: true, onboard: true,
            output: "onboard", usb_cap: 0, usb_cap_min: -30, usb_profile: "auto" },
    radio: { available: true, playing: false },
    spotify: { key_set: true, key_changed: "2026-10-28T19:04:00Z" },
    ota: {
      booted: "a", trial: null, confirmed: true, rolled_back: false,
      slots: { a: { version: "r15", installed: "2026-11-02T21:10:00Z" }, b: { version: "r14", installed: "2026-10-30T18:45:00Z" } },
    },
    upload: { active: false },
    hits: 42,
  };
  const startUptime = 3 * 86400 + 5 * 3600 + 17 * 60;
  const elapsedAt = { base: 83, t: Date.now() };

  function tick() {
    const now = Date.now();
    s.time = now / 1000;
    s.host.uptime = startUptime + (now - t0) / 1000;
    s.cpu.percent = 10 + Math.random() * 12;
    s.cpu.temp = 46 + Math.random() * 3;
    for (const x of s.audio.streams) {
      if (x.source !== "Radio" && x.playing) x.elapsed = elapsedAt.base + (now - elapsedAt.t) / 1000;
    }
    if (s.bt.window) {
      s.bt.window_left = Math.max(0, 120 - Math.round((now - s.bt.windowStart) / 1000));
      if (!s.bt.window_left) s.bt.window = false;
    }
    return s;
  }

  const LOG = (name) => [
    `2026-11-06 09:12:03 ${name}: started`,
    `2026-11-06 09:12:04 ${name}: (this is the static demo: no real speaker behind this page)`,
    `2026-11-06 09:41:17 ${name}: client 192.168.1.23 connected`,
  ].join("\n");

  const NETS = [
    { ssid: "HomeNet", security: "wpa2", signal: -58, freq: 2437, bssid: "a0:b1:c2:d3:e4:f5", saved: true },
    { ssid: "Workshop", security: "wpa2/wpa3", signal: -71, freq: 2412, bssid: "a0:b1:c2:d3:e4:f6", saved: true },
    { ssid: "Neighbours-5G", security: "wpa3", signal: -80, freq: 5180, bssid: "b4:c5:d6:e7:f8:09", bands: ["2.4 GHz", "5 GHz"] },
    { ssid: "Cafe Guest", security: "open", signal: -84, freq: 2462, bssid: "c8:d9:ea:fb:0c:1d" },
  ];

  function playRadio(st) {
    s.radio = { available: true, playing: true, station: st, codec: st.codec.toLowerCase(), bitrate: st.bitrate * 1000,
                rate: 44100, title: "Demo track - Artist" };
    s.audio.streams = [{ id: 88, source: "Radio", codec: st.codec.toLowerCase(), format: "F32LE", rate: 44100,
                         channels: 2, state: "running", errors: "0", volume: 1, mute: false }];
  }

  function handle(path, q, body) {
    switch (path) {
      case "/api/status": return tick();
      case "/api/logs": return { text: LOG(q.get("name")) };
      case "/api/wifi/scan": return { networks: NETS };
      case "/api/radio/favorites": return { stations: STATIONS.filter((x) => favs.has(x.stationuuid)) };
      case "/api/radio/countries": return { countries: [{ code: "US", name: "United States", count: 6123 }, { code: "DE", name: "Germany", count: 4210 }] };
      case "/api/radio/tags": return { tags: [{ name: "jazz", count: 2311 }, { name: "classical", count: 1874 }, { name: "ambient", count: 702 }] };
      case "/api/radio/search": {
        const name = (q.get("q") || "").toLowerCase(), tag = (q.get("tag") || "").toLowerCase();
        const cc = (q.get("countrycode") || q.get("country") || "").toUpperCase();
        const list = STATIONS.filter((x) => (!name || x.name.toLowerCase().includes(name)) &&
          (!tag || x.tags.includes(tag)) && (!cc || x.countrycode === cc));
        return { stations: list, page: 0, more: false };
      }
      case "/api/volume": {
        const t = body.id === s.audio.sink.id ? s.audio.sink : s.audio.streams.find((x) => x.id === body.id);
        if (t && "volume" in body) t.volume = body.volume;
        if (t && "mute" in body) t.mute = body.mute;
        return { ok: true };
      }
      case "/api/audio": Object.assign(s.tone, body); return { ok: true };
      case "/api/radio":
        if (body.cmd === "play") playRadio(STATIONS.find((x) => x.stationuuid === body.uuid) || STATIONS[0]);
        else if (body.cmd === "stop") { s.radio = { available: true, playing: false }; s.audio.streams = []; }
        else if (body.cmd === "fav") { body.add ? favs.add(body.uuid) : favs.delete(body.uuid); }
        return { ok: true, stations: STATIONS.filter((x) => favs.has(x.stationuuid)) };
      case "/api/bt":
        if (body.cmd === "pair") Object.assign(s.bt, { window: body.open, windowStart: Date.now(), window_left: 120 });
        else if (body.cmd === "forget") s.bt.devices = s.bt.devices.filter((d) => d.path !== body.path);
        else if (body.cmd === "forget-all") s.bt.devices = [];
        return { ok: true };
      case "/api/name":
        s.host.speaker_name = s.bt.alias = body.name;
        return { ok: true, result: "renamed" };
      case "/api/system":
        if (body.action === "factory-reset-check") return { ok: true };
        if (body.action === "save") return { ok: true };
        return { error: "This is a demo: there's no speaker to restart or reset." };
      case "/api/service": return { ok: true };
      default: return { error: "This is a demo: that needs a real speaker." };
    }
  }

  window.fetch = async (url, opts = {}) => {
    const u = new URL(url, "http://panda.local/");
    let body = {};
    try { body = opts.body ? JSON.parse(opts.body) : {}; } catch { /* not JSON */ }
    const data = handle(u.pathname, u.searchParams, body);
    await new Promise((r) => setTimeout(r, 60));
    return new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });
  };

  // OTA upload: refuse, like a real error would surface.
  window.XMLHttpRequest = class {
    open() {} setRequestHeader() {}
    get upload() { return {}; }
    send() {
      this.status = 400;
      this.responseText = JSON.stringify({ error: "This is a demo: updates need a real speaker." });
      setTimeout(() => this.onload && this.onload(), 300);
    }
  };

  // No server-sent events: app.js falls back to polling /api/status every 3 s.
  delete window.EventSource;
  window.EventSource = undefined;

  document.addEventListener("DOMContentLoaded", () => {
    const b = document.createElement("div");
    b.textContent = "Static demo with made-up data: nothing here talks to a real speaker.";
    b.style.cssText = "background:#ffffe1;color:#000;border:1px solid #000;padding:4px 8px;margin:4px;font:12px sans-serif;text-align:center";
    document.body.prepend(b);
  });
})();
