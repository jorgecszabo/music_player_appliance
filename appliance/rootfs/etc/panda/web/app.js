// Panda Control Panel. Vanilla JS, no build step.
// Live status over server-sent events (/api/events), actions as JSON POSTs.
// Strings from devices (Bluetooth names, track titles) are only ever set via
// textContent, never as HTML.
"use strict";

const $ = (id) => document.getElementById(id);
let last = null;          // latest status snapshot
let dragging = new Set(); // sliders the user is moving (don't overwrite them)
const playingSince = {};  // stream id -> when it started running (LCD elapsed time)

// --- helpers -----------------------------------------------------------------------
function el(tag, props = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
}

function setText(id, text) {
  const e = $(id);
  if (e && e.textContent !== text) e.textContent = text;
}

async function post(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Panda-Panel": "1" },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.error) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

function fmtDuration(s) {
  s = Math.floor(s);
  const d = Math.floor(s / 86400), h = Math.floor(s / 3600) % 24, m = Math.floor(s / 60) % 60;
  return (d ? `${d} day${d > 1 ? "s" : ""}, ` : "") + `${h} h ${String(m).padStart(2, "0")} min`;
}

function fmtClock(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

function kHz(rate) {
  return rate ? `${(rate / 1000).toFixed(rate % 1000 ? 1 : 0)} kHz` : "-";
}

// A Windows 98 message box. Resolves true for OK.
function dialog(text, { title = "Panda", icon = "!", kind = "", cancel = true } = {}) {
  return new Promise((resolve) => {
    setText("modal-title", title);
    setText("modal-text", text);
    setText("modal-icon", icon);
    $("modal-icon").className = `dialog-icon ${kind}`;
    $("modal-cancel").hidden = !cancel;
    $("modal").hidden = false;
    $("modal-ok").focus();
    const done = (v) => {
      $("modal").hidden = true;
      $("modal-ok").onclick = $("modal-cancel").onclick = null;
      document.onkeydown = null;
      resolve(v);
    };
    $("modal-ok").onclick = () => done(true);
    $("modal-cancel").onclick = () => done(false);
    document.onkeydown = (e) => { if (e.key === "Escape") done(false); };
  });
}

const info = (text) => dialog(text, { icon: "i", kind: "info", cancel: false });
const oops = (err) => dialog(String(err.message || err), { title: "Error", icon: "X", kind: "err", cancel: false });

// --- rendering --------------------------------------------------------------------
function render(s) {
  last = s;
  if (s.error) { setText("live-text", s.error); return; }
  const name = s.host.speaker_name || "Panda";
  setText("speaker-name", name);
  document.querySelectorAll(".sn").forEach((e) => { e.textContent = name; });
  document.title = `${name} Control Panel`;
  if (document.activeElement !== $("name-input") && !$("name-input").dataset.edited) {
    $("name-input").value = name;
  }
  setText("hits", String(s.hits).padStart(6, "0"));
  setText("updated", new Date(s.time * 1000).toLocaleString());
  renderNowPlaying(s);
  renderVolume(s);
  renderBluetooth(s);
  renderSystem(s);
  renderServices(s);
  renderSave(s);
  renderSpotify(s);
  renderTone(s);
  renderRadio(s);
}

// --- radio ------------------------------------------------------------------------
let radioList = [];       // stations shown in the table
let radioFavs = new Set();

function renderRadio(s) {
  const r = s.radio || {};
  const on = !!r.playing;
  $("radio-led").classList.toggle("on", on);
  $("radio-stop").disabled = !on;
  if (!r.available) {
    setText("radio-station", "Radio player not running");
    setText("radio-title", "Check the speaker-radio service.");
  } else if (on) {
    setText("radio-station", (r.station && r.station.name) || "Radio");
    const fmt = [r.codec && r.codec.toUpperCase(), r.bitrate ? `${Math.round(r.bitrate / 1000)} kbps` : null,
      r.rate ? kHz(r.rate) : null].filter(Boolean).join(" / ");
    setText("radio-title", r.buffering ? "Buffering..." : (r.title || fmt || "Playing"));
  } else {
    setText("radio-station", "Radio off");
    setText("radio-title", "Pick a station below.");
  }
  const cur = on && r.station ? r.station.stationuuid : null;
  for (const tr of $("radio-results").tBodies[0].rows) tr.classList.toggle("playing", !!cur && tr.dataset.uuid === cur);
}

function drawStations(list, emptyText) {
  radioList = list;
  const tbody = $("radio-results").tBodies[0];
  tbody.replaceChildren();
  if (!list.length) {
    tbody.append(el("tr", {}, el("td", { colspan: 6, class: "center" }, emptyText)));
    return;
  }
  for (const st of list) {
    const fav = el("button", { class: `fav${radioFavs.has(st.stationuuid) ? " on" : ""}`, type: "button",
      title: "Favorite", "aria-label": `Favorite ${st.name}` }, "\u2605");
    fav.addEventListener("click", async () => {
      const add = !radioFavs.has(st.stationuuid);
      try {
        const r = await post("/api/radio", { cmd: "fav", uuid: st.stationuuid, add });
        radioFavs = new Set(r.stations.map((x) => x.stationuuid));
        fav.classList.toggle("on", add);
      } catch (e) { oops(e); }
    });
    const play = el("button", { class: "btn", type: "button" }, "Play");
    play.addEventListener("click", async () => {
      play.disabled = true;
      setText("radio-status", `Tuning in to ${st.name}...`);
      try { await post("/api/radio", { cmd: "play", uuid: st.stationuuid }); setText("radio-status", ""); }
      catch (e) { setText("radio-status", ""); oops(e); }
      play.disabled = false;
    });
    const fmt = [st.codec, st.bitrate ? `${st.bitrate}k` : null].filter(Boolean).join(" ");
    const tr = el("tr", {},
      el("td", {}, fav),
      el("td", {}, el("b", {}, st.name || "(no name)")),
      el("td", { class: "hide-narrow" }, st.countrycode || ""),
      el("td", {}, fmt || "-"),
      el("td", { class: "hide-narrow small" }, (st.tags || "").split(",").slice(0, 4).join(", ")),
      el("td", {}, play));
    tr.dataset.uuid = st.stationuuid;
    tbody.append(tr);
  }
  if (last) renderRadio(last);
}

async function radioSearch(params) {
  setText("radio-status", "Searching...");
  try {
    const r = await fetch(`/api/radio/search?${new URLSearchParams(params)}`);
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    drawStations(d.stations, "No stations found. Try another search!");
    setText("radio-status", `${d.stations.length} station(s)`);
  } catch (e) { setText("radio-status", ""); oops(e); }
}

async function radioFavorites(show) {
  try {
    const d = await (await fetch("/api/radio/favorites")).json();
    radioFavs = new Set(d.stations.map((x) => x.stationuuid));
    if (show) {
      drawStations(d.stations, "No favorites yet. Click the star next to a station.");
      setText("radio-status", `${d.stations.length} favorite(s)`);
    }
  } catch (e) { if (show) oops(e); }
}

function capDb(t, step) { return (step - t.cap_max) * t.cap_step_db; }

function renderTone(s) {
  const t = s.tone;
  if (!t) return;
  $("tone-cap").max = t.cap_max;
  for (const [id, v] of [["tone-bass", t.bass], ["tone-treble", t.treble], ["tone-cap", t.cap]]) {
    if (dragging.has(id) || toneTimer) continue;
    $(id).value = v;
  }
  showTone(t);
}

function showTone(t) {
  const sign = (v) => (v > 0 ? "+" : "") + v;
  setText("tone-bass-val", `${sign(+$("tone-bass").value)} dB`);
  setText("tone-treble-val", `${sign(+$("tone-treble").value)} dB`);
  setText("tone-cap-val", `${capDb(t, +$("tone-cap").value)} dB`);
}

let toneTimer = null;
function sendTone() {
  toneTimer = null;
  post("/api/audio", { bass: +$("tone-bass").value, treble: +$("tone-treble").value,
    cap: +$("tone-cap").value }).catch(oops);
}

function renderSpotify(s) {
  const sp = s.spotify;
  if (!sp) setText("spotify-text", "Checking the API key...");
  else if (sp.key_set) {
    const when = sp.key_changed ? new Date(sp.key_changed).toLocaleString() : "?";
    setText("spotify-text", `An API key is set (last changed ${when}).`);
  } else {
    setText("spotify-text", "No API key! Spotify Connect is off until you set one.");
  }
  $("spotify-text").className = sp && !sp.key_set ? "st-bad" : "";
}

function activeStream(s) {
  const streams = (s.audio && s.audio.streams) || [];
  return streams.find((x) => x.state === "running") || null;
}

function renderNowPlaying(s) {
  const streams = (s.audio && s.audio.streams) || [];
  const act = activeStream(s);
  const bt = s.bt || {};
  const track = bt.track;
  const sink = s.audio && s.audio.sink;

  let title = `*** ${(s.host.speaker_name || "PANDA").toUpperCase()} AMP *** IDLE ***`;
  if (act) {
    const np = (s.nowplaying || {})[act.source];
    const r = s.radio || {};
    if (act.source === "Bluetooth" && track && track.title) {
      title = `${track.artist ? track.artist + " - " : ""}${track.title}`;
    } else if (act.source === "Radio" && r.station) {
      title = `${r.station.name}${r.title ? " - " + r.title : ""}`;
    } else if (np && np.title) {
      title = `${np.artist ? np.artist + " - " : ""}${np.title}`;
    } else if (np && np.client) {
      title = `${act.source} - ${np.client}${np.client_ip ? " (" + np.client_ip + ")" : ""}`;
    } else {
      title = `${act.source}${act.device ? " - " + act.device : ""}`;
    }
  }
  const t = $("lcd-title");
  if (t.dataset.text !== title) {
    t.dataset.text = title;
    t.textContent = title.toUpperCase();
    t.classList.remove("scrolling");
    // Scroll it like Winamp when it doesn't fit.
    if (t.scrollWidth > t.parentElement.clientWidth) t.classList.add("scrolling");
  }

  for (const x of streams) {
    if (x.state === "running") playingSince[x.id] ??= Date.now();
    else delete playingSince[x.id];
  }
  setText("lcd-state", act ? "▶" : "■");
  if (act && act.source === "Bluetooth" && track && track.position) {
    setText("lcd-time", fmtClock(track.position));
  } else {
    setText("lcd-time", act ? fmtClock(Date.now() - playingSince[act.id]) : "--:--");
  }
  setText("lcd-codec", act ? (act.codec || act.format || "PCM").toUpperCase() : "---");
  setText("lcd-rate", act ? kHz(act.rate) : "-- kHz");
  setText("lcd-out", `OUT ${kHz(sink && sink.rate)}`);
  setText("lcd-chan", act && act.channels === 1 ? "MONO" : "STEREO");
  setText("lcd-source", act ? act.source.toUpperCase() : "NO SOURCE");
  for (const id of ["lcd-codec", "lcd-rate", "lcd-chan"]) $(id).classList.toggle("off", !act);

  // Ticker.
  const bits = [`Welcome to ${s.host.speaker_name}!`];
  bits.push(act ? `Now playing: ${title}` : "Nothing playing right now");
  bits.push(`Uptime ${fmtDuration(s.host.uptime)}`);
  if (s.cpu.temp != null) bits.push(`CPU at ${s.cpu.temp.toFixed(1)} °C`);
  bits.push("Thanks for visiting, come back soon!");
  setText("ticker", bits.join("   +++   "));

  // Streams table.
  const tbody = $("streams").tBodies[0];
  tbody.replaceChildren();
  if (!streams.length) {
    tbody.append(el("tr", {}, el("td", { colspan: 4, class: "center" },
      `Nothing is playing. Pick "${s.host.speaker_name}" in Spotify, AirPlay or Bluetooth.`)));
    return;
  }
  for (const x of streams) {
    const label = x.source + (x.device ? ` (${x.device})` : "");
    const fmt = [x.codec ? x.codec.toUpperCase() : null, x.format, kHz(x.rate),
      x.channels ? `${x.channels} ch` : null].filter(Boolean).join(", ");
    const running = x.state === "running";
    tbody.append(el("tr", {},
      el("td", {}, el("b", {}, label)),
      el("td", {}, fmt || "-"),
      el("td", { class: running ? "st-ok" : "st-dim" }, running ? "Playing" : x.state || "-"),
      el("td", {}, x.errors ?? "-")));
  }
}

function renderVolume(s) {
  const sink = s.audio && s.audio.sink;
  if (sink && !dragging.has("master-vol")) {
    $("master-vol").value = Math.round(sink.volume * 100);
    $("master-vol").dataset.id = sink.id;
    setText("master-val", `${Math.round(sink.volume * 100)}%`);
    $("master-mute").checked = sink.mute;
    $("master-mute").dataset.id = sink.id;
  }
  // One channel strip per stream.
  const mixer = $("mixer");
  const streams = (s.audio && s.audio.streams) || [];
  const keep = new Set(["master-channel"]);
  for (const x of streams) {
    const cid = `ch-${x.id}`;
    keep.add(cid);
    let ch = $(cid);
    if (!ch) {
      const slider = el("input", { type: "range", class: "vslider", min: 0, max: 100, step: 1,
        id: `vol-${x.id}`, "aria-label": `${x.source} volume` });
      slider.dataset.id = x.id;
      wireSlider(slider);
      const mute = el("input", { type: "checkbox" });
      mute.dataset.id = x.id;
      mute.addEventListener("change", () => post("/api/volume", { id: +mute.dataset.id, mute: mute.checked }).catch(oops));
      ch = el("div", { class: "channel", id: cid },
        el("div", { class: "channel-name" }, x.source),
        slider,
        el("div", { class: "channel-value", id: `val-${x.id}` }, ""),
        el("label", { class: "check" }, mute, " Mute"));
      mixer.append(ch);
    }
    if (!dragging.has(`vol-${x.id}`)) {
      $(`vol-${x.id}`).value = Math.round(x.volume * 100);
      setText(`val-${x.id}`, `${Math.round(x.volume * 100)}%`);
    }
    ch.querySelector("input[type=checkbox]").checked = x.mute;
  }
  for (const ch of [...mixer.children]) if (!keep.has(ch.id)) ch.remove();
}

function wireSlider(slider) {
  let timer = null;
  const send = () => {
    timer = null;
    post("/api/volume", { id: +slider.dataset.id, volume: slider.value / 100 }).catch(oops);
  };
  slider.addEventListener("pointerdown", () => dragging.add(slider.id));
  slider.addEventListener("input", () => {
    dragging.add(slider.id);
    const v = $(slider.id === "master-vol" ? "master-val" : `val-${slider.dataset.id}`);
    if (v) v.textContent = `${slider.value}%`;
    if (!timer) timer = setTimeout(send, 150);
  });
  slider.addEventListener("change", () => {
    if (timer) { clearTimeout(timer); send(); }
    setTimeout(() => dragging.delete(slider.id), 2500);
  });
}

function renderBluetooth(s) {
  const bt = s.bt || {};
  const btn = $("pair-btn");
  if (bt.error) {
    setText("pair-text", `Bluetooth is not available: ${bt.error}`);
    btn.disabled = true;
    return;
  }
  btn.disabled = !!bt.forgetting;
  btn.classList.toggle("active", !!bt.window);
  setText("pair-btn", bt.window ? "Stop pairing" : "Start pairing");
  $("pair-progress").hidden = !bt.window;
  if (bt.forgetting) {
    setText("pair-text", "Forgetting all devices...");
  } else if (bt.window) {
    setText("pair-text", `Pairing mode! Look for "${bt.alias}" on your phone. ${bt.window_left} s left.`);
    $("pair-fill").style.width = `${Math.round((bt.window_left / 120) * 100)}%`;
  } else {
    setText("pair-text", "The speaker is hidden. Press the button (or S2 on the board) to let a new phone pair for 2 minutes.");
  }

  const tbody = $("bt-devices").tBodies[0];
  tbody.replaceChildren();
  const devs = (bt.devices || []).filter((d) => d.paired);
  if (!devs.length) {
    tbody.append(el("tr", {}, el("td", { colspan: 4, class: "center" }, "No paired devices.")));
  }
  for (const d of devs) {
    const forget = el("button", { class: "btn", type: "button" }, "Forget");
    forget.addEventListener("click", async () => {
      if (!await dialog(`Forget "${d.name}"? It will have to pair again.`)) return;
      post("/api/bt", { cmd: "forget", path: d.path }).catch(oops);
    });
    tbody.append(el("tr", {},
      el("td", {}, el("b", {}, d.name || "(unnamed)")),
      el("td", { class: "hide-narrow" }, d.address),
      el("td", { class: d.connected ? "st-ok" : "st-dim" }, d.connected ? "Connected" : "Paired"),
      el("td", {}, forget)));
  }
  $("forget-all").disabled = !devs.length || !!bt.forgetting;

  const tr = $("bt-track");
  if (bt.track && bt.track.title) {
    tr.hidden = false;
    tr.replaceChildren(el("b", {}, "Track info (AVRCP): "),
      [bt.track.artist, bt.track.title, bt.track.album].filter(Boolean).join(" — "),
      bt.track.status ? ` [${bt.track.status}]` : "");
  } else {
    tr.hidden = true;
  }
}

function renderSystem(s) {
  setText("sys-board", `${s.host.board || "?"} (${s.cpu.cores}x CPU) — hostname ${s.host.hostname}`);
  setText("sys-os", `Alpine Linux ${s.host.alpine}, kernel ${s.host.kernel}`);
  setText("sys-uptime", fmtDuration(s.host.uptime));
  const cpu = s.cpu.percent ?? 0;
  $("cpu-fill").style.width = `${Math.min(100, cpu)}%`;
  setText("sys-cpu", `${cpu.toFixed(1)}%  (load ${s.cpu.load.map((x) => x.toFixed(2)).join(" ")})`);
  const used = s.mem.total - s.mem.available;
  $("mem-fill").style.width = `${Math.round((used / s.mem.total) * 100)}%`;
  setText("sys-mem", `${Math.round(used / 1024)} MB of ${Math.round(s.mem.total / 1024)} MB in use (root fs in RAM: ${Math.round(s.mem.shmem / 1024)} MB)`);
  setText("sys-temp", s.cpu.temp != null ? `${s.cpu.temp.toFixed(1)} °C` : "n/a");

  const sink = s.audio && s.audio.sink;
  setText("sys-audio", sink
    ? `${sink.description || sink.name}: ${sink.format} ${kHz(sink.rate)}, quantum ${sink.quantum || "?"}, xruns ${sink.errors ?? "?"}`
    : "no output found");

  const net = s.net || {};
  const parts = [];
  for (const [ifn, l] of Object.entries(net.links || {})) {
    const addrs = (net.addrs || []).filter((a) => a.if === ifn).map((a) => a.addr);
    if (l.state !== "up" && !addrs.length) continue;
    parts.push(`${ifn} ${l.state}${l.speed && +l.speed > 0 ? ` ${l.speed} Mb/s` : ""}: ${addrs.join(", ") || "no address"}`);
  }
  setText("sys-net", parts.join(" | ") || "-");

  const sy = s.sync || {};
  setText("sys-sync", sy.leap
    ? `${sy.leap === "Normal" ? "Synchronized" : sy.leap}, ${sy.offset} (stratum ${sy.stratum}, ${sy.reference})`
    : sy.raw || sy.error || "-");

  const bt = s.bt || {};
  const n = (bt.devices || []).filter((d) => d.paired).length;
  setText("sys-bt", bt.error ? bt.error : `${s.host.bt_controller}, ${n} paired`);
}

function renderServices(s) {
  const tbody = $("svc-table").tBodies[0];
  const restartable = new Set(["speaker-soloist", "speaker-shairport", "panda-bt", "bluetooth", "avahi-daemon", "chronyd"]);
  tbody.replaceChildren();
  for (const [name, state] of Object.entries(s.services || {})) {
    if (name === "error") continue;
    const ok = state === "started";
    const cell = el("td", {});
    if (restartable.has(name)) {
      const b = el("button", { class: "btn", type: "button" }, "Restart");
      b.addEventListener("click", async () => {
        b.disabled = true;
        try { await post("/api/service", { name }); } catch (e) { oops(e); }
        setTimeout(() => { b.disabled = false; }, 3000);
      });
      cell.append(b);
    }
    tbody.append(el("tr", {},
      el("td", {}, el("span", { class: `led ${ok ? "on" : state === "stopped" ? "" : "warn"}` }), " ", name),
      el("td", { class: ok ? "st-ok" : "st-bad" }, state),
      cell));
  }
}

function renderSave(s) {
  const l = s.lbu;
  if (!l) setText("lbu-text", "Checking for unsaved changes...");
  else if (l.error) setText("lbu-text", `Can't check: ${l.error}`);
  else if (l.changes) setText("lbu-text", `${l.changes} unsaved change(s): ${l.files.join(", ")}`);
  else setText("lbu-text", "All settings are saved to the card.");
}

// --- live connection --------------------------------------------------------------
function setLive(state) {
  const led = $("live-led");
  led.className = `led ${state === "live" ? "on" : state === "poll" ? "warn" : ""}`;
  setText("live-text", state === "live" ? "Live (server-sent events)" : state === "poll" ? "Polling..." : "Offline. Retrying...");
}

function connect() {
  if (!window.EventSource) return poll();
  const es = new EventSource("/api/events");
  es.onopen = () => setLive("live");
  es.onmessage = (e) => {
    try { render(JSON.parse(e.data)); setLive("live"); }
    catch (err) { console.error(err); setText("live-text", `Script error: ${err.message}`); }
  };
  es.onerror = () => setLive("off");
}

async function poll() {
  try {
    const r = await fetch("/api/status");
    render(await r.json());
    setLive("poll");
  } catch { setLive("off"); }
  setTimeout(poll, 3000);
}

// --- actions ----------------------------------------------------------------------
function wireActions() {
  wireSlider($("master-vol"));
  $("master-mute").addEventListener("change", () =>
    post("/api/volume", { id: +$("master-mute").dataset.id, mute: $("master-mute").checked }).catch(oops));

  $("pair-btn").addEventListener("click", () => {
    const open = !(last && last.bt && last.bt.window);
    post("/api/bt", { cmd: "pair", open }).catch(oops);
  });

  $("forget-all").addEventListener("click", async () => {
    if (!await dialog("Forget ALL paired devices? Every phone will have to pair again.")) return;
    post("/api/bt", { cmd: "forget-all" }).catch(oops);
  });

  $("restart-audio").addEventListener("click", async () => {
    if (!await dialog("Restart the whole audio stack? Playback stops for a few seconds.")) return;
    post("/api/service", { name: "audio" }).catch(oops);
  });

  $("log-refresh").addEventListener("click", loadLog);
  $("log-name").addEventListener("change", loadLog);

  $("name-input").addEventListener("input", () => { $("name-input").dataset.edited = "1"; });
  $("name-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("name-input").value.trim();
    if (!await dialog(`Rename the speaker to "${name}"? Spotify, AirPlay and Bluetooth restart (about 30 s).`)) return;
    try {
      const r = await post("/api/name", { name });
      delete $("name-input").dataset.edited;
      info(r.result === "unchanged" ? "That's already the name." : "Renaming... the new name shows up in about 30 seconds.");
    } catch (err) { oops(err); }
  });

  $("radio-form").addEventListener("submit", (e) => {
    e.preventDefault();
    radioSearch({ q: $("radio-q").value, tag: $("radio-tag").value,
      country: $("radio-country").value, order: $("radio-order").value });
  });
  $("radio-top").addEventListener("click", () => radioSearch({ order: "votes" }));
  $("radio-popular").addEventListener("click", () => radioSearch({ order: "clickcount" }));
  $("radio-favs").addEventListener("click", () => radioFavorites(true));
  $("radio-stop").addEventListener("click", () => post("/api/radio", { cmd: "stop" }).catch(oops));
  radioFavorites(false);

  for (const id of ["tone-bass", "tone-treble", "tone-cap"]) {
    const el_ = $(id);
    el_.addEventListener("pointerdown", () => dragging.add(id));
    el_.addEventListener("input", () => {
      if (last && last.tone) showTone(last.tone);
      clearTimeout(toneTimer);
      toneTimer = setTimeout(sendTone, 250);
    });
    el_.addEventListener("change", () => setTimeout(() => dragging.delete(id), 2500));
  }
  $("tone-reset").addEventListener("click", () => {
    $("tone-bass").value = 0; $("tone-treble").value = 0;
    if (last && last.tone) showTone(last.tone);
    sendTone();
  });

  $("key-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("key-input");
    const key = input.value.trim();
    if (!key) return;
    if (!await dialog("Replace the Spotify API key? Spotify Connect restarts.")) return;
    try {
      const r = await post("/api/spotify-key", { key });
      input.value = "";
      if (r.ok) info("New key saved. Spotify Connect is running again.");
      else oops(new Error(`Key saved, but Spotify Connect is ${r.result}. Check the soloist log.`));
    } catch (err) { oops(err); }
  });

  $("save-btn").addEventListener("click", async () => {
    $("save-btn").disabled = true;
    try {
      const r = await post("/api/system", { action: "save" });
      info(r.ok ? "Settings saved to the card." : "Saving failed. See the messages log.");
    } catch (err) { oops(err); }
    $("save-btn").disabled = false;
  });

  $("reboot-btn").addEventListener("click", async () => {
    if (!await dialog("Are you sure you want to restart the speaker? Unsaved changes are lost.", { title: "Shut Down Panda" })) return;
    try {
      await post("/api/system", { action: "reboot" });
      info("It is now safe to... wait about a minute. The page reconnects by itself.");
    } catch (err) { oops(err); }
  });
}

async function loadLog() {
  const name = $("log-name").value;
  setText("log-text", "Loading...");
  try {
    const r = await fetch(`/api/logs?name=${encodeURIComponent(name)}`);
    const d = await r.json();
    setText("log-text", d.error || d.text || "(empty)");
    $("log-text").scrollTop = $("log-text").scrollHeight;
  } catch (err) { setText("log-text", String(err)); }
}

wireActions();
connect();
