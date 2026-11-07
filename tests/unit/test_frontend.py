"""The web page, in headless Chromium against the demo's fake API.

Needs chromium (skipped without). The page is the real index.html/app.js/style.css; the fake
API is docs/demo/mock.js, followed by a wrapper that poisons the text fields the speaker does
not control (station names, SSIDs, track titles, device names...).
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from tests.conftest import PANDA, ROOT

CHROMIUM = shutil.which("chromium-browser") or shutil.which("chromium") or shutil.which("google-chrome")
pytestmark = pytest.mark.skipif(not CHROMIUM, reason="no chromium")

PAYLOADS = [
    '<img src=x onerror=window.__xss++>',
    '"><svg onload=window.__xss++>',
    "'</script><script>window.__xss++</script>",
    "<a href=javascript:window.__xss++>x</a>",
    "&lt;b&gt;&amp;",
    "a" * 3000,
]
POISON_KEYS = ["name", "ssid", "title", "artist", "album", "alias", "tags", "client", "client_ip", "description",
               "speaker_name", "board_name", "board_blurb", "version", "message", "country", "reference"]


def build(tmp_path, payload, extra_js=""):
    web = os.path.join(PANDA, "web")
    html = open(os.path.join(web, "index.html")).read()
    css = open(os.path.join(web, "style.css")).read()
    js = open(os.path.join(web, "app.js")).read()
    mock = open(os.path.join(ROOT, "docs", "demo", "mock.js")).read()
    poison = """
(() => {
  window.__xss = 0;
  window.__errors = [];
  window.addEventListener("error", (e) => window.__errors.push(String(e.message)));
  window.addEventListener("unhandledrejection", (e) => window.__errors.push("rejection: " + String(e.reason)));
  const PAYLOAD = %s;
  const KEYS = new Set(%s);
  const walk = (v) => {
    if (Array.isArray(v)) return v.map(walk);
    if (v && typeof v === "object") {
      const o = {};
      for (const [k, x] of Object.entries(v)) o[k] = (KEYS.has(k) && typeof x === "string") ? PAYLOAD : walk(x);
      return o;
    }
    return v;
  };
  const f0 = window.fetch;
  window.fetch = async (url, opts) => {
    const r = await f0(url, opts);
    const j = await r.json();
    return new Response(JSON.stringify(walk(j)), { status: 200, headers: { "Content-Type": "application/json" } });
  };
})();
""" % (json.dumps(payload).replace("</", "<\\/"), json.dumps(POISON_KEYS))
    probe = """
setTimeout(() => {
  const r = document.documentElement;
  r.dataset.probeXss = String(window.__xss);
  r.dataset.probeErrors = JSON.stringify(window.__errors);
  r.dataset.probeBad = String(document.querySelectorAll('img[src="x"], svg[onload], a[href^="javascript"], script:not([data-ok])').length);
  r.dataset.probeText = document.body.innerText.length > 100 ? "yes" : "no";
}, 3500);
"""
    assert html.count('<link rel="stylesheet" href="style.css">') == 1 and html.count('<script src="app.js" defer></script>') == 1
    html = html.replace('<link rel="stylesheet" href="style.css">', f"<style>{css}</style>")
    html = html.replace('<script src="app.js" defer></script>', f"<script data-ok>{mock}</script><script data-ok>{poison}</script>")
    html = html.replace("</body>", f"<script data-ok>{js}</script><script data-ok>{extra_js}</script><script data-ok>{probe}</script></body>")
    page = tmp_path / "page.html"
    page.write_text(html)
    return page


def render(page):
    r = subprocess.run([CHROMIUM, "--headless", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=8000", "--dump-dom",
                        f"file://{page}"], capture_output=True, text=True, timeout=90)
    dom = r.stdout
    attrs = {k.lower(): v for k, v in re.findall(r'data-probe-(xss|errors|bad|text)="([^"]*)"', dom)}
    return dom, attrs


@pytest.mark.parametrize("payload", PAYLOADS, ids=lambda p: p[:20])
def test_untrusted_text_never_becomes_markup_or_script(tmp_path, payload):
    dom, attrs = render(build(tmp_path, payload))
    assert attrs, "the page did not finish (no probe output)"
    assert attrs["xss"] == "0", "a payload ran"
    assert attrs["bad"] == "0", "a payload became an element"
    assert attrs["text"] == "yes"


def test_page_loads_without_script_errors(tmp_path):
    dom, attrs = render(build(tmp_path, "Plain Name"))
    assert attrs["errors"].replace("&quot;", '"') == "[]", attrs["errors"]
    assert attrs["xss"] == "0"


def test_radio_restart_button_follows_the_player_state(tmp_path):
    """Hidden while the radio works and while it is starting; visible when it is down or stuck."""
    cases = {"ok": {"available": True, "playing": False},
             "starting": {"available": False, "state": "starting", "message": "The radio is starting up."},
             "stuck": {"available": False, "state": "stuck", "message": "The radio isn't responding."},
             "stopped": {"available": False, "state": "stopped", "message": "The radio has stopped."}}
    expect = {"ok": ("hidden", "Radio off"), "starting": ("hidden", "Radio starting"),
              "stuck": ("visible", "Radio not responding"), "stopped": ("visible", "Radio stopped")}
    for name, radio in cases.items():
        js = """
(() => {
  const f0 = window.fetch;
  window.fetch = async (url, opts) => {
    const r = await f0(url, opts);
    const j = await r.json();
    if (String(url).includes("/api/status")) j.radio = %s;
    return new Response(JSON.stringify(j), { status: 200, headers: { "Content-Type": "application/json" } });
  };
})();
""" % json.dumps(radio)
        probe = """
setTimeout(() => {
  const b = document.getElementById("radio-restart");
  document.documentElement.dataset.probeRestart = b.hidden ? "hidden" : "visible";
  document.documentElement.dataset.probeStation = document.getElementById("radio-station").textContent;
}, 3500);
"""
        page = build(tmp_path, "x", extra_js="")
        text = page.read_text().replace("<script data-ok>\n(() => {\n  window.__xss = 0;", f"<script data-ok>{js}</script><script data-ok>\n(() => {{\n  window.__xss = 0;", 1)
        text = text.replace("</body>", f"<script data-ok>{probe}</script></body>")
        # the poison wrapper turns the radio message into a payload: not wanted here
        text = text.replace("const KEYS = new Set(", "const KEYS = new Set([]); const _unused = (")
        page.write_text(text)
        dom, _ = render(page)
        got = {k: v for k, v in re.findall(r'data-probe-(restart|station)="([^"]*)"', dom)}
        assert got.get("restart") == expect[name][0], (name, got)
        assert got.get("station", "").startswith(expect[name][1]), (name, got)


def test_services_table_offers_restart_exactly_for_the_servers_list(tmp_path):
    page = build(tmp_path, "x")
    probe = """
setTimeout(() => {
  const rows = [...document.querySelectorAll("#svc-table tbody tr")];
  document.documentElement.dataset.probeRows = JSON.stringify(rows.map((r) => [r.cells[0].textContent.trim(), !!r.querySelector("button")]));
}, 3500);
"""
    page.write_text(page.read_text().replace("</body>", f"<script data-ok>{probe}</script></body>"))
    dom, _ = render(page)
    rows = json.loads(re.search(r'data-probe-rows="([^"]*)"', dom).group(1).replace("&quot;", '"'))
    mock = open(os.path.join(ROOT, "docs", "demo", "mock.js")).read()
    restartable = set(re.search(r"restartable: \[([^\]]*)\]", mock).group(1).replace('"', "").replace(" ", "").split(","))
    assert rows, "no services listed"
    for name, has_button in rows:
        assert has_button == (name in restartable), (name, has_button)
