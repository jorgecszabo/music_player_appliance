// Panda Control Panel. Vanilla JS, no build step.
// Live status over server-sent events (/api/events), actions as JSON POSTs.
// Strings from devices (Bluetooth names, track titles) are only ever set via
// textContent, never as HTML.
"use strict";

const $ = (id) => document.getElementById(id);
let last = null;          // latest status snapshot
let dragging = new Set(); // sliders the user is moving (don't overwrite them)
// LCD play clock: the server's elapsed time for the active stream, advanced
// locally between status updates (every 2 s) so the digits tick each second.
const clock = { id: null, base: 0, t0: 0, playing: false, timer: null };

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

// The hostname for a speaker name; the same rule as web-admin.sh set-name.
const hostOf = (name) => name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
function showHost() {
  const h = hostOf($("name-input").value.trim());
  setText("name-host", h ? `http://${h}.local/` : "");
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
  setText("board-blurb", s.host.board_blurb || `an upcycled ${s.host.board_name || "computer"}`);
  setText("board-badge-text", s.host.board_badge || "");
  $("board-badge").hidden = !s.host.board_badge;
  document.querySelectorAll(".sn").forEach((e) => { e.textContent = name; });
  document.title = `${name} Control Panel`;
  if (document.activeElement !== $("name-input") && !$("name-input").dataset.edited) {
    $("name-input").value = name;
    showHost();
  }
  setText("hits", String(s.hits).padStart(6, "0"));
  setText("updated", new Date(s.time * 1000).toLocaleString());
  renderNowPlaying(s);
  renderVolume(s);
  renderBluetooth(s);
  renderNetwork(s);
  renderSystem(s);
  renderServices(s);
  renderSave(s);
  renderSpotify(s);
  renderTone(s);
  renderRadio(s);
  renderUpdate(s);
}

// --- system update (OTA) -----------------------------------------------------------
let otaBusy = false;      // an upload from this page is running

function fmtWhen(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function renderUpdate(s) {
  const o = s.ota;
  const body = $("ota-table").tBodies[0];
  if (!o || o.error) {
    body.replaceChildren(el("tr", {}, el("td", { colspan: 5, class: "center" },
      o ? `Can't read the update slots: ${o.error}` : "Checking...")));
    return;
  }
  const key = JSON.stringify([o.slots, o.booted, o.trial, o.confirmed]);
  if (body.dataset.key !== key) {
    body.dataset.key = key;
    body.replaceChildren(...["a", "b"].map((slot) => {
      const i = o.slots[slot];
      const running = slot === o.booted;
      const status = running ? (o.trial === slot ? "Running (on trial)" : "Running")
        : !i ? "Empty" : o.trial === slot ? "Starts next" : "Standby";
      const btn = !running && i
        ? el("button", { class: "btn", type: "button", onclick: () => otaSwitch(slot, i.version) },
          o.trial === slot ? "Restart now" : "Switch...")
        : "";
      return el("tr", { class: running ? "slot-run" : "" },
        el("td", {}, slot.toUpperCase()),
        el("td", {}, i ? i.version || "?" : "-"),
        el("td", { class: "hide-narrow" }, i ? fmtWhen(i.installed || i.built) : "-"),
        el("td", {}, status),
        el("td", {}, btn));
    }));
  }
  let notice = "";
  if (o.rolled_back) notice = "The last update didn't start up properly, so the speaker went back to the version it had before. Your settings are as they were.";
  else if (o.trial && o.trial === o.booted) notice = "This version is on trial: it is kept once the speaker has been running fine for a minute. If it doesn't come up properly, the speaker restarts into the previous version.";
  else if (o.trial) notice = `Version ${(o.slots[o.trial] || {}).version || "?"} starts at the next restart.`;
  setText("ota-notice", notice);
  $("ota-notice").hidden = !notice;
  const u = s.upload || {};
  if (!otaBusy && u.active) otaProgress(u);   // an upload from another browser
}

function otaProgress(u, sentFraction) {
  $("ota-progress").hidden = false;
  let frac = 0, text = "";
  if (u.stage === "copy" || u.stage === "verified") {
    frac = u.total ? (u.done || 0) / u.total : 0;
    text = u.stage === "verified" ? `Signature OK (version ${u.version || "?"}). Copying files...`
      : `Copying files... ${Math.round(frac * 100)}%`;
  } else {
    frac = sentFraction !== undefined ? sentFraction : u.total ? (u.received || 0) / u.total : 0;
    text = `Sending the update... ${Math.round(frac * 100)}%`;
  }
  $("ota-fill").style.width = `${Math.min(100, frac * 100).toFixed(1)}%`;
  setText("ota-detail", text);
}

function otaUpload(file) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", "/api/ota/upload");
    x.setRequestHeader("Content-Type", "application/x-panda-ota");
    x.setRequestHeader("X-Panda-Panel", "1");
    x.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const u = (last && last.upload) || {};
      // The device verifies while the file streams in: show its own progress once it has some.
      if (u.active && (u.stage === "copy" || u.stage === "verified")) otaProgress(u);
      else otaProgress({ stage: "upload" }, e.loaded / e.total);
    };
    x.onload = () => {
      let d = {};
      try { d = JSON.parse(x.responseText); } catch (e) { /* not JSON */ }
      if (x.status === 200 && d.ok) resolve(d);
      else reject(new Error(d.error || `HTTP ${x.status}`));
    };
    x.onerror = () => reject(new Error("The connection to the speaker was lost."));
    x.send(file);
  });
}

async function otaInstall() {
  const file = $("ota-file").files[0];
  if (!file) return;
  if (!/\.pota$/i.test(file.name)) return oops(new Error("That isn't an update file (.pota)."));
  if (!await dialog(`Install ${file.name} (${(file.size / 1048576).toFixed(0)} MB)? The running version isn't touched; the new one starts after a restart.`, { title: "Panda Update Wizard", icon: "i", kind: "info" })) return;
  otaBusy = true;
  $("ota-install").disabled = $("ota-browse").disabled = true;
  setText("ota-step", "Please wait while Setup installs the update. This takes a few minutes.");
  otaProgress({ stage: "upload" }, 0);
  try {
    const r = await otaUpload(file);
    $("ota-fill").style.width = "100%";
    setText("ota-detail", `Version ${r.version} installed in slot ${r.slot.toUpperCase()}.`);
    setText("ota-step", "Setup has finished installing the update.");
    if (await dialog(`Setup has finished installing version ${r.version}. You must restart the speaker before the new version takes effect. Do you want to restart now?`, { title: "Panda Update Wizard", icon: "i", kind: "info" })) {
      await otaActivate(r.slot);
    }
  } catch (err) {
    setText("ota-step", "The update was not installed. Nothing has changed.");
    setText("ota-detail", "");
    $("ota-progress").hidden = true;
    oops(err);
  } finally {
    otaBusy = false;
    $("ota-browse").disabled = false;
    $("ota-install").disabled = !$("ota-file").files[0];
  }
}

async function otaActivate(slot) {
  try {
    await post("/api/ota", { cmd: "activate", slot });
    info("Restarting... The page reconnects by itself in a few minutes.");
  } catch (err) { oops(err); }
}

async function otaSwitch(slot, version) {
  const o = last && last.ota;
  if (o && o.trial === slot) return otaActivate(slot);
  if (!await dialog(`Switch to version ${version} (slot ${slot.toUpperCase()})? The speaker restarts. Your settings are kept.`, { title: "System Update" })) return;
  otaActivate(slot);
}

// --- radio ------------------------------------------------------------------------
let radioList = [];       // stations shown in the table
let radioFavs = new Set();

function renderRadio(s) {
  const r = s.radio || {};
  const on = !!r.playing;
  $("radio-led").classList.toggle("on", on);
  $("radio-stop").disabled = !on;
  $("radio-restart").hidden = !!r.available || r.state === "starting";
  if (!r.available) {
    setText("radio-station", { starting: "Radio starting...", stuck: "Radio not responding" }[r.state] || "Radio stopped");
    setText("radio-title", r.message || "Restarting the radio should bring it back.");
  } else if (on) {
    setText("radio-station", (r.station && r.station.name) || "Radio");
    const fmt = [r.codec && r.codec.toUpperCase(), r.bitrate ? `${Math.round(r.bitrate / 1000)} kbps` : null,
      r.rate ? kHz(r.rate) : null].filter(Boolean).join(" / ");
    setText("radio-title", r.paused ? "Paused" : r.buffering ? "Buffering..." : (r.title || fmt || "Playing"));
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

let radioQuery = null;    // last search (for paging)

async function radioSearch(params, page = 0) {
  radioQuery = params;
  setText("radio-status", "Searching...");
  try {
    const r = await fetch(`/api/radio/search?${new URLSearchParams({ ...params, page })}`);
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    drawStations(d.stations, "No stations found. Try another search!");
    const first = d.page * 40 + 1;
    setText("radio-status", d.stations.length ? `Stations ${first}-${first + d.stations.length - 1}${d.more ? "+" : ""}` : "");
    $("radio-pager").hidden = !(d.page > 0 || d.more);
    $("radio-prev").disabled = d.page === 0;
    $("radio-next").disabled = !d.more;
    setText("radio-page", `Page ${d.page + 1}`);
    $("radio-pager").dataset.page = d.page;
  } catch (e) { setText("radio-status", ""); oops(e); }
}

// Suggestions: genres from radio-browser's tag list as you type, countries once.
let tagTimer = null;
async function suggestTags() {
  const q = $("radio-tag").value.trim();
  try {
    const d = await (await fetch(`/api/radio/tags?${new URLSearchParams({ q })}`)).json();
    const list = $("radio-tag-list");
    list.replaceChildren(...(d.tags || []).map((t) =>
      el("option", { value: t.name, label: `${t.count} stations` })));
  } catch { /* suggestions are optional */ }
}

async function loadCountries() {
  try {
    const d = await (await fetch("/api/radio/countries")).json();
    $("radio-country-list").replaceChildren(...(d.countries || []).map((c) =>
      el("option", { value: c.code, label: `${c.name} (${c.count})` })));
  } catch { /* optional */ }
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

// Output choice and both caps as the page has them: the server's values, except
// while the user is changing them. The "Max volume" slider edits the cap of the
// selected output (onboard: hardware gain steps; USB: dB, software).
let toneSel = null;

function renderTone(s) {
  const t = s.tone;
  if (!t) return;
  if (!toneSel || !(toneTimer || dragging.has("tone-cap") || dragging.has("output") || dragging.has("out-mode"))) {
    toneSel = { output: t.output, cap: t.cap, usb_cap: t.usb_cap, usb_profile: t.usb_profile };
  }
  for (const [id, v] of [["tone-bass", t.bass], ["tone-treble", t.treble]]) {
    if (dragging.has(id) || toneTimer) continue;
    $(id).value = v;
  }
  const radio = $(`out-${toneSel.output}`);
  if (radio) radio.checked = true;
  setupCap(t);
  renderOutputs(s, t);
  showTone(t);
}

function setupCap(t) {
  const usb = toneSel.output === "usb";
  const cap = $("tone-cap");
  cap.min = usb ? t.usb_cap_min : 0;
  cap.max = usb ? 0 : t.cap_max;
  if (!dragging.has("tone-cap")) cap.value = usb ? toneSel.usb_cap : toneSel.cap;
  setText("tone-cap-label", usb ? "Max volume (USB)" : "Max volume");
  document.querySelectorAll(".cap-row").forEach((e) => { e.hidden = !usb && !t.available; });
}

function renderOutputs(s, t) {
  const a = s.audio || {};
  const outs = a.outputs || [];
  const usb = outs.find((o) => o.usb);
  $("out-onboard-line").hidden = !t.onboard;
  setText("out-usb-name", usb ? `USB: ${usb.description || usb.name}` : "USB audio (none plugged in)");
  renderUsbModes(a.usb_card);
  const cur = outs.find((o) => o.name === a.output);
  let msg = cur ? `Playing through: ${cur.description || cur.name}.` : "No audio output found.";
  if (toneSel.output === "usb" && !usb && t.onboard) {
    msg += " No USB audio card is plugged in: the built-in jack plays until one is.";
  }
  setText("out-status", msg);
}

// The USB card's output modes (ACP profiles), "Automatic" first.
function renderUsbModes(card) {
  $("out-mode-line").hidden = !card;
  if (!card) return;
  const sel = $("out-mode");
  const opts = [["auto", "Automatic"]].concat(card.profiles.map((p) =>
    [p.name, p.description + (p.available === "no" ? " (unavailable)" : "")]));
  const key = JSON.stringify(opts);
  if (sel.dataset.key !== key) {
    sel.textContent = "";
    for (const [v, label] of opts) {
      const o = document.createElement("option");
      o.value = v; o.textContent = label;
      sel.appendChild(o);
    }
    sel.dataset.key = key;
  }
  if (!dragging.has("out-mode")) {
    // A saved mode this card doesn't have: shown as Automatic (it's left alone).
    sel.value = opts.some(([v]) => v === toneSel.usb_profile) ? toneSel.usb_profile : "auto";
  }
  const cur = card.profiles.find((p) => p.name === card.profile);
  sel.title = cur ? `Now: ${cur.description}` : "";
}

function showTone(t) {
  const sign = (v) => (v > 0 ? "+" : "") + v;
  setText("tone-bass-val", `${sign(+$("tone-bass").value)} dB`);
  setText("tone-treble-val", `${sign(+$("tone-treble").value)} dB`);
  const v = +$("tone-cap").value;
  setText("tone-cap-val", `${toneSel && toneSel.output === "usb" ? v : capDb(t, v)} dB`);
}

let toneTimer = null;
function sendTone() {
  toneTimer = null;
  if (!toneSel) return;
  post("/api/audio", { bass: +$("tone-bass").value, treble: +$("tone-treble").value,
    cap: toneSel.cap, usb_cap: toneSel.usb_cap, output: toneSel.output,
    usb_profile: toneSel.usb_profile }).catch(oops);
}

// board.conf SPOTIFY=no: the board has no Spotify, so the page doesn't mention it.
function sourcesText(s, last) {
  return (s.host.spotify === false ? ["AirPlay", "Bluetooth"] : ["Spotify", "AirPlay", "Bluetooth"])
    .join(", ").replace(/, (\w+)$/, ` ${last} $1`);
}

function renderSpotify(s) {
  const off = s.host.spotify === false;
  $("spotify-group").hidden = off;
  $("log-soloist").hidden = off;
  $("log-soloist").disabled = off;
  if (off && $("log-name").value === "soloist") $("log-name").value = "shairport";
  setText("name-where", sourcesText(s, "and"));
  if (off) return;
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

function clockNow() {
  return clock.base + (clock.playing ? (performance.now() - clock.t0) / 1000 : 0);
}

// Take the server's time for the active stream. Updates arrive a little late,
// so a time slightly behind ours is that delay, not a step back: keep ours
// unless it's off by a second or more (seek, new track, phone's correction).
function setClock(act) {
  const known = act && typeof act.elapsed === "number";
  const playing = !!known && act.playing !== false;
  if (!known) {
    Object.assign(clock, { id: null, base: 0, playing: false });
  } else {
    const ours = clock.id === act.id ? clockNow() : null;
    const keep = ours !== null && playing && clock.playing &&
      act.elapsed <= ours && ours - act.elapsed < 1;
    if (!keep) Object.assign(clock, { id: act.id, base: act.elapsed, t0: performance.now(), playing });
  }
  setText("lcd-state", !act ? "■" : known && !playing ? "❚❚" : "▶");
  $("lcd-time").classList.toggle("paused", !!act && known && !playing);
  tickClock();
}

function tickClock() {
  clearTimeout(clock.timer);
  clock.timer = null;
  if (clock.id === null) return setText("lcd-time", "--:--");
  const t = clockNow();
  setText("lcd-time", fmtClock(t * 1000));
  // Wake just after the next whole second of play time.
  if (clock.playing) clock.timer = setTimeout(tickClock, (1 - (t % 1)) * 1000 + 15);
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

  setClock(act);
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
      `Nothing is playing. Pick "${s.host.speaker_name}" in ${sourcesText(s, "or")}.`)));
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

// --- Network Connections ------------------------------------------------------------
let wifiNets = [];        // last scan
let wifiSel = null;       // selected SSID in the list
let wifiScanning = false;
let wifiScannedOnce = false;

// dBm -> 0..5 bars
function bars(dbm) {
  if (dbm == null) return 0;
  return dbm >= -55 ? 5 : dbm >= -63 ? 4 : dbm >= -70 ? 3 : dbm >= -78 ? 2 : dbm >= -86 ? 1 : 0;
}
function barsEl(n) {
  const b = el("span", { class: "bars small", "aria-hidden": "true" });
  for (let i = 0; i < 5; i++) b.append(el("i", { class: i < n ? "lit" : "" }));
  return b;
}
const SEC = { open: "Unsecured", owe: "Enhanced Open", wpa: "WPA", wpa2: "WPA2", wpa3: "WPA3",
              "wpa2/wpa3": "WPA2/WPA3", wep: "WEP (not supported)", enterprise: "802.1X (not supported)" };
function band(freq) { return freq > 5900 ? "6 GHz" : freq > 4900 ? "5 GHz" : "2.4 GHz"; }
const WIFI_STATE = { disconnected: "Not connected", inactive: "Not connected (no saved network in range)",
  scanning: "Looking for saved networks...", authenticating: "Authenticating...", associating: "Connecting...",
  associated: "Connecting...", "4way_handshake": "Checking the network key...",
  group_handshake: "Checking the network key...", interface_disabled: "Adapter disabled",
  starting: "Starting the adapter...", off: "Wi-Fi is off" };

function renderNetwork(s) {
  // Ethernet: any interface that isn't a Wi-Fi adapter.
  const net = s.net || {};
  const w = s.wifi || {};
  const wifiIfs = new Set((w.adapters || []).map((a) => a.name));
  const lan = Object.entries(net.links || {}).filter(([n]) => !wifiIfs.has(n) && !n.startsWith("wl"));
  const lanUp = lan.find(([, l]) => l.state === "up");
  if (lanUp) {
    const [n, l] = lanUp;
    const a = (net.addrs || []).filter((x) => x.if === n && x.family === "inet").map((x) => x.addr.split("/")[0]);
    setText("lan-text", `Connected${l.speed && +l.speed > 0 ? `, ${l.speed} Mb/s` : ""} (${n}) — ${a.join(", ") || "getting an address..."}`);
  } else {
    setText("lan-text", lan.length ? "Network cable unplugged" : "No Ethernet adapter");
  }
  document.querySelector(".conn-icon.lan").classList.toggle("off", !lanUp);

  // Wi-Fi status line
  const c = w.connection;
  const lit = c ? bars(c.signal) : 0;
  $("wifi-bars").querySelectorAll("i").forEach((b, i) => b.classList.toggle("lit", i < lit));
  let text;
  if (w.error && !w.adapters) text = `Wi-Fi is not available: ${w.error}`;
  else if (!(w.adapters || []).length) text = "No wireless adapter found. Plug in a USB Wi-Fi adapter.";
  else if (w.pending) text = `Connecting to ${w.pending}...${w.pending_tries ? ` (retry ${w.pending_tries})` : ""}`;
  else if (c) {
    const a4 = (net.addrs || []).find((x) => x.if === w.iface && x.family === "inet");
    const ip = c.ip || (a4 ? a4.addr.split("/")[0] : "");
    text = `Connected to ${c.ssid} (${band(c.freq)}, ${c.signal ?? "?"} dBm${c.speed ? `, ${c.speed} Mb/s` : ""}) — ${ip || "getting an address..."}`;
  } else if (!(w.saved || []).length && ["disconnected", "inactive", "scanning"].includes(w.state)) {
    text = "Not connected. Pick a network below and click Connect.";
  } else text = WIFI_STATE[w.state] || w.state || "-";
  if (w.error && w.adapters) text += ` [${w.error}]`;
  setText("wifi-text", text);
  const act = (w.adapters || []).find((a) => a.active);
  const bandText = act && act.bands && act.bands.length && !act.bands.every((b) => act.description.includes(b))
    ? ` — ${act.bands.join(" + ")}` : "";
  setText("wifi-adapter", act ? `Using: ${act.description}${bandText}` : "");
  $("wifi-refresh").disabled = !w.iface || wifiScanning;
  $("wifi-other").disabled = !w.iface;
  if (w.iface && !wifiScannedOnce && location.hash === "#network") wifiScan();

  // Preferred networks
  const tbody = $("wifi-saved").tBodies[0];
  const key = JSON.stringify([w.saved, c && c.ssid]);
  if (tbody.dataset.key !== key) {
    tbody.dataset.key = key;
    tbody.replaceChildren();
    if (!(w.saved || []).length) tbody.append(el("tr", {}, el("td", { colspan: 3, class: "center" }, "No saved networks.")));
    for (const n of w.saved || []) {
      const forget = el("button", { class: "btn", type: "button" }, "Forget");
      forget.addEventListener("click", async () => {
        const here = c && c.ssid === n.ssid;
        if (!await dialog(`Forget "${n.ssid}"? The speaker won't connect to it any more.` +
            (here ? " It's connected now: if you're using this page over that network, you'll lose the page." : ""),
            { title: "Wireless Network Connection" })) return;
        post("/api/wifi", { cmd: "forget", ssid: n.ssid }).catch(oops);
      });
      tbody.append(el("tr", {},
        el("td", {}, el("b", {}, n.ssid), c && c.ssid === n.ssid ? el("span", { class: "st-ok" }, " (connected)") : ""),
        el("td", {}, SEC[n.security] || n.security),
        el("td", {}, forget)));
    }
  }

  // Advanced: adapter choice, country, adapter list (don't clobber while editing)
  const sel = $("wifi-adapter-sel");
  const opts = [["auto", "Automatic (prefer USB)"], ["onboard", "Onboard only"], ["usb", "USB only"], ["off", "Off"]]
    .concat((w.adapters || []).map((a) => [a.name, `${a.name}: ${a.description}`]));
  const okey = JSON.stringify([opts, w.adapter_setting]);
  if (sel.dataset.key !== okey && document.activeElement !== sel) {
    sel.dataset.key = okey;
    sel.replaceChildren(...opts.map(([v, t]) => el("option", { value: v }, t)));
    sel.value = w.adapter_setting || "auto";
  }
  if (document.activeElement !== $("wifi-country") && !$("wifi-country").dataset.edited) {
    $("wifi-country").value = w.country || "";
  }
  const props = $("wifi-adapters");
  const pkey = JSON.stringify(w.adapters);
  if (props.dataset.key !== pkey) {
    props.dataset.key = pkey;
    props.replaceChildren(...(w.adapters || []).map((a) => el("tr", {},
      el("th", {}, `${a.name}${a.active ? " (in use)" : ""}`),
      el("td", {}, `${a.description}; driver ${a.driver || "?"}; ${(a.bands || []).join(" + ") || "?"}; MAC ${a.mac}`))));
  }
}

function drawWifiList() {
  const box = $("wifi-list");
  box.replaceChildren();
  if (!wifiNets.length) {
    box.append(el("div", { class: "empty" }, wifiScanning ? "Searching..." : "No networks found."));
  }
  for (const n of wifiNets) {
    const item = el("div", { class: `item${n.ssid === wifiSel ? " sel" : ""}`, role: "option", tabindex: "0",
                             "aria-selected": n.ssid === wifiSel ? "true" : "false" },
      barsEl(bars(n.signal)),
      el("span", {}, el("b", {}, n.ssid), n.saved ? " (saved)" : ""),
      el("span", { class: "sec" }, n.security === "open" ? "Unsecured" : `\u{1F512} ${SEC[n.security] || n.security}`),
      el("span", { class: "band" }, (n.bands || [band(n.freq)]).join("/")));
    item.title = `${n.signal} dBm, ${n.freq} MHz, ${n.bssid}`;
    const pick = () => { wifiSel = n.ssid; drawWifiList(); };
    item.addEventListener("click", pick);
    item.addEventListener("dblclick", () => { pick(); wifiConnectSelected(); });
    item.addEventListener("keydown", (e) => { if (e.key === "Enter") { pick(); wifiConnectSelected(); } });
    box.append(item);
  }
  $("wifi-connect").disabled = !wifiNets.some((n) => n.ssid === wifiSel);
}

async function wifiScan() {
  if (wifiScanning) return;
  wifiScanning = wifiScannedOnce = true;
  $("wifi-refresh").disabled = true;
  setText("wifi-scan-text", "searching...");
  drawWifiList();
  try {
    const r = await fetch("/api/wifi/scan");
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    wifiNets = d.networks || [];
    setText("wifi-scan-text", `${wifiNets.length} found at ${new Date().toLocaleTimeString()}. Double-click one to connect.`);
  } catch (err) {
    setText("wifi-scan-text", `couldn't scan (${err.message}).`);
  }
  wifiScanning = false;
  $("wifi-refresh").disabled = false;
  drawWifiList();
}

// The "Connect to Wireless Network" dialog. Resolves {ssid, password, hidden} or null.
function wifiDialog({ ssid = "", needKey = true, other = false, saved = false } = {}) {
  return new Promise((resolve) => {
    const f = $("wifi-form");
    $("wifi-ssid").value = ssid;
    $("wifi-ssid").readOnly = !other;
    $("wifi-pw").value = "";
    $("wifi-pw").type = "password";
    $("wifi-show").checked = false;
    $("wifi-hidden").checked = other;
    $("wifi-hidden-line").hidden = !other;
    $("wifi-pw-line").hidden = !needKey;
    $("wifi-show").parentElement.hidden = !needKey;
    const intro = other
      ? "Type the name and key of the network. Leave the key empty for an unsecured network."
      : !needKey ? `"${ssid}" is unsecured: anyone nearby can see what goes over it. Connect anyway?`
      : saved ? `"${ssid}" is saved. Type a new network key, or leave it empty to use the saved one.`
      : `The network "${ssid}" requires a network key (WPA password).`;
    setText("wifi-modal-text", intro);
    $("wifi-modal").hidden = false;
    (other ? $("wifi-ssid") : needKey ? $("wifi-pw") : $("wifi-ok")).focus();
    const done = (v) => {
      $("wifi-modal").hidden = true;
      f.onsubmit = $("wifi-cancel").onclick = $("wifi-show").onchange = null;
      document.onkeydown = null;
      $("wifi-pw").value = "";   // don't keep the key in the page
      resolve(v);
    };
    $("wifi-show").onchange = () => { $("wifi-pw").type = $("wifi-show").checked ? "text" : "password"; };
    f.onsubmit = (e) => {
      e.preventDefault();
      const pw = $("wifi-pw").value;
      const name = $("wifi-ssid").value;
      if (!name) return;
      if (needKey && pw && (pw.length < 8 || pw.length > 64)) {
        setText("wifi-modal-text", "The network key must be 8 to 63 characters.");
        return;
      }
      if (needKey && !pw && !saved && !other) {
        setText("wifi-modal-text", "Please type the network key.");
        return;
      }
      done({ ssid: name, password: needKey ? pw : "", hidden: $("wifi-hidden").checked });
    };
    $("wifi-cancel").onclick = () => done(null);
    document.onkeydown = (e) => { if (e.key === "Escape") done(null); };
  });
}

async function wifiConnect(opts) {
  const w = (last && last.wifi) || {};
  const res = await wifiDialog(opts);
  if (!res) return;
  const viaWifi = w.connection && w.connection.ip && location.hostname === w.connection.ip;
  if (viaWifi && w.connection.ssid !== res.ssid &&
      !await dialog(`You're using this page over the Wi-Fi network "${w.connection.ssid}". Switching networks will disconnect this page; find the speaker again on "${res.ssid}" (http://${last.host.hostname}.local/).`,
        { title: "Wireless Network Connection" })) return;
  try {
    await post("/api/wifi", { cmd: "connect", ...res });
    wifiSel = null;
    drawWifiList();
    info(`Connecting to "${res.ssid}"... The status above shows when it's done. It's saved once it connects; if it fails, the speaker goes back to its saved networks.`);
  } catch (err) { oops(err); }
}

function wifiConnectSelected() {
  const n = wifiNets.find((x) => x.ssid === wifiSel);
  if (!n) return;
  if (n.security === "wep" || n.security === "enterprise") {
    return oops(new Error(`"${n.ssid}" uses ${SEC[n.security]}. Only unsecured, WPA2 and WPA3 personal networks are supported.`));
  }
  wifiConnect({ ssid: n.ssid, needKey: !["open", "owe"].includes(n.security), saved: n.saved });
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
  const restartable = new Set(s.restartable || []);
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

  $("name-input").addEventListener("input", () => { $("name-input").dataset.edited = "1"; showHost(); });
  $("name-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("name-input").value.trim();
    const host = hostOf(name);
    if (!host) return oops(new Error("The name needs a letter or a digit."));
    if (!await dialog(`Rename the speaker to "${name}" and restart it?\n\n` +
      `${sourcesText(last || { host: {} }, "and")} will show "${name}".\n` +
      `Its network name becomes "${host}": http://${host}.local/\n\n` +
      `The speaker restarts now (about a minute). Open the new address afterwards.`, { title: "Rename Panda" })) return;
    try {
      const r = await post("/api/name", { name });
      delete $("name-input").dataset.edited;
      info(r.result === "unchanged" ? "That's already the name." : `Restarting as "${host}"... in about a minute the speaker is at http://${host}.local/`);
    } catch (err) { oops(err); }
  });

  $("radio-form").addEventListener("submit", (e) => {
    e.preventDefault();
    radioSearch({ q: $("radio-q").value, tag: $("radio-tag").value,
      country: $("radio-country").value, order: $("radio-order").value });
  });
  $("radio-top").addEventListener("click", () => radioSearch({ order: "votes" }));
  $("radio-popular").addEventListener("click", () => radioSearch({ order: "clickcount" }));
  $("radio-favs").addEventListener("click", () => { $("radio-pager").hidden = true; radioFavorites(true); });
  $("radio-prev").addEventListener("click", () => radioQuery && radioSearch(radioQuery, Math.max(0, +$("radio-pager").dataset.page - 1)));
  $("radio-next").addEventListener("click", () => radioQuery && radioSearch(radioQuery, +$("radio-pager").dataset.page + 1));
  $("radio-tag").addEventListener("input", () => { clearTimeout(tagTimer); tagTimer = setTimeout(suggestTags, 200); });
  $("radio-tag").addEventListener("focus", () => { if (!$("radio-tag-list").children.length) suggestTags(); }, { once: true });
  $("radio-country").addEventListener("focus", () => { if (!$("radio-country-list").children.length) loadCountries(); }, { once: true });
  $("radio-restart").addEventListener("click", async (e) => {
    const b = e.currentTarget;
    b.disabled = true;
    try { await post("/api/service", { name: "speaker-radio" }); } catch (err) { oops(err); }
    setTimeout(() => { b.disabled = false; }, 5000);
  });
  $("radio-stop").addEventListener("click", () => post("/api/radio", { cmd: "stop" }).catch(oops));
  radioFavorites(false);

  for (const id of ["tone-bass", "tone-treble", "tone-cap"]) {
    const el_ = $(id);
    el_.addEventListener("pointerdown", () => dragging.add(id));
    el_.addEventListener("input", () => {
      if (id === "tone-cap" && toneSel) toneSel[toneSel.output === "usb" ? "usb_cap" : "cap"] = +el_.value;
      if (last && last.tone) showTone(last.tone);
      clearTimeout(toneTimer);
      toneTimer = setTimeout(sendTone, 250);
    });
    el_.addEventListener("change", () => setTimeout(() => dragging.delete(id), 2500));
  }
  for (const r of document.querySelectorAll('input[name="output"]')) {
    r.addEventListener("change", () => {
      if (!toneSel || !last || !last.tone) return;
      dragging.add("output");
      toneSel.output = r.value;
      renderTone(last);
      clearTimeout(toneTimer);
      sendTone();
      setTimeout(() => dragging.delete("output"), 2500);
    });
  }
  $("out-mode").addEventListener("change", () => {
    if (!toneSel) return;
    dragging.add("out-mode");
    toneSel.usb_profile = $("out-mode").value;
    clearTimeout(toneTimer);
    sendTone();
    setTimeout(() => dragging.delete("out-mode"), 4000);
  });
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

  $("reset-btn").addEventListener("click", async () => {
    let chk;
    try { chk = await post("/api/system", { action: "factory-reset-check" }); } catch (e) { return oops(e); }
    if (/no factory overlay|damaged/.test(chk.result || "")) return oops(new Error(chk.result));
    if (!await dialog("Factory reset? This forgets ALL saved settings: Bluetooth phones, the Spotify API key, tone, radio favorites and the speaker name.", { title: "Factory Reset" })) return;
    if (!await dialog("Really? This can't be undone. The speaker restarts with factory settings.", { title: "Factory Reset", icon: "X", kind: "err" })) return;
    try {
      const r = await post("/api/system", { action: "factory-reset" });
      if (r.ok) info("Resetting... The speaker restarts in a moment with factory settings.");
      else oops(new Error(r.result || "failed"));
    } catch (err) { oops(err); }
  });

  $("wifi-refresh").addEventListener("click", wifiScan);
  $("wifi-connect").addEventListener("click", wifiConnectSelected);
  $("wifi-other").addEventListener("click", () => wifiConnect({ other: true }));
  $("wifi-country").addEventListener("input", () => { $("wifi-country").dataset.edited = "1"; });
  $("wifi-settings").addEventListener("submit", async (e) => {
    e.preventDefault();
    const country = $("wifi-country").value.trim().toUpperCase();
    if (country && !/^[A-Z]{2}$/.test(country)) return oops(new Error("The country is a two-letter code, like US or DE."));
    try {
      await post("/api/wifi", { cmd: "settings", country, adapter: $("wifi-adapter-sel").value });
      delete $("wifi-country").dataset.edited;
      info("Wi-Fi settings applied and saved to the card.");
    } catch (err) { oops(err); }
  });
  window.addEventListener("hashchange", () => {
    if (location.hash === "#network" && last && last.wifi && last.wifi.iface) wifiScan();
  });

  $("ota-browse").addEventListener("click", () => $("ota-file").click());
  $("ota-file").addEventListener("change", () => {
    const f = $("ota-file").files[0];
    $("ota-file-name").value = f ? f.name : "";
    $("ota-install").disabled = !f || otaBusy;
  });
  $("ota-install").addEventListener("click", otaInstall);

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
