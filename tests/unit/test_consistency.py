"""Drift checks between files that must agree (the class of bug that hid the Restart button)."""
import glob
import os
import re

import pytest

from tests.conftest import ETC, PANDA, ROOT, load_module

INITD = os.path.join(ETC, "init.d")
# Services that come with Alpine packages, not from this repo.
EXTERNAL = {"net", "localmount", "dbus", "bluetooth", "avahi-daemon", "chronyd", "sshd", "crond",
            "modules", "hwdrivers", "mdev", "udev", "udev-settle", "networking", "modloop",
            "bootmisc", "hostname", "root", "sysfs", "devfs", "dev", "procfs", "mtab"}


def read(*parts):
    with open(os.path.join(*parts)) as f:
        return f.read()


@pytest.fixture(scope="module")
def web():
    return load_module("panda-web.py")


@pytest.fixture(scope="module")
def admin():
    return read(PANDA, "web-admin.sh")


def case_names(text, label):
    """The names in the `case "${2:-}" in` block of web-admin.sh under `label)`."""
    m = re.search(r"^%s\)\n\tcase [^\n]*in\n(.*?)\n\tesac" % re.escape(label), text, re.S | re.M)
    assert m, f"no case block under {label}) in web-admin.sh"
    names = set()
    for line in m.group(1).splitlines():
        head = line.strip().split(")", 1)[0]
        if head and head != "*":
            names.update(head.split("|"))
    return names


# --- panda-web <-> web-admin.sh -----------------------------------------------------------

def test_restartable_is_a_subset_of_services(web):
    assert web.RESTARTABLE <= set(web.SERVICES)


def test_every_restartable_service_is_accepted_by_web_admin(web, admin):
    accepted = case_names(admin, "restart")
    missing = web.RESTARTABLE - accepted
    assert not missing, f"web-admin.sh restart refuses {sorted(missing)}"


def test_web_admin_only_restarts_services_the_web_page_offers(web, admin):
    extra = case_names(admin, "restart") - web.RESTARTABLE
    assert not extra, f"web-admin.sh restarts {sorted(extra)} but panda-web does not offer them"


def test_every_service_we_own_has_an_init_script(web):
    for name in web.SERVICES:
        if name in EXTERNAL:
            continue
        assert os.path.exists(os.path.join(INITD, name)), f"no init script for {name}"


def test_logs_known_to_the_web_page_are_accepted_by_web_admin(web, admin):
    accepted = case_names(admin, "logs")
    assert set(web.LOGS) <= accepted, sorted(set(web.LOGS) - accepted)


def test_log_dropdown_matches_logs(web):
    html = read(PANDA, "web", "index.html")
    block = re.search(r'<select id="log-name">(.*?)</select>', html, re.S).group(1)
    options = set(re.findall(r"<option[^>]*>([^<]+)</option>", block))
    assert options == set(web.LOGS)


# --- the API surface the clients use ---------------------------------------------------------

def api_paths_used(text):
    return set(re.findall(r"/api/[A-Za-z0-9/_-]+", text))


def test_every_api_path_the_clients_use_exists_in_panda_web():
    served = api_paths_used(read(PANDA, "panda-web.py"))
    for client in ("web/app.js", "panda-lcd.py"):
        used = api_paths_used(read(PANDA, client))
        missing = {p for p in used if p not in served and not any(p.startswith(s + "/") for s in served)}
        assert not missing, f"{client} uses {sorted(missing)}, which panda-web does not serve"


def test_every_element_id_app_js_looks_up_exists_in_index_html():
    js = read(PANDA, "web", "app.js")
    html = read(PANDA, "web", "index.html")
    ids_in_html = set(re.findall(r'\bid="([^"]+)"', html))
    used = set(re.findall(r"\$\(\s*[\"']([A-Za-z0-9_-]+)[\"']\s*\)", js))
    used |= set(re.findall(r"getElementById\(\s*[\"']([^\"']+)[\"']", js))
    assert used, "no $('id') lookups found: the pattern is out of date"
    missing = used - ids_in_html
    assert not missing, f"app.js looks up ids that index.html lacks: {sorted(missing)}"


def snapshot_keys():
    src = read(PANDA, "panda-web.py")
    body = re.search(r"snap = \{(.*?)\n        \}", src, re.S).group(1)
    return set(re.findall(r'^\s+"(\w+)":', body, re.M))


def test_status_keys_app_js_reads_are_in_the_snapshot():
    """Every s.<key> / last.<key> that app.js reads is a field collect() produces."""
    produced = snapshot_keys()
    assert {"radio", "services", "restartable", "audio", "host"} <= produced
    used = set(re.findall(r"\b(?:s|last)\.([a-z_]+)\b", read(PANDA, "web", "app.js")))
    used.discard("error")                      # the "no data yet" marker, not a snapshot field
    assert used, "no status reads found: the pattern is out of date"
    assert used <= produced, f"app.js reads status fields panda-web never sends: {sorted(used - produced)}"


def test_demo_mock_provides_every_snapshot_key():
    mock = read(ROOT, "docs", "demo", "mock.js")
    missing = {k for k in snapshot_keys() if k not in ("time", "hits", "upload") and not re.search(r"\b%s:" % k, mock)}
    assert not missing, f"docs/demo/mock.js lacks status fields {sorted(missing)}"


def test_demo_page_is_up_to_date():
    demo = read(ROOT, "docs", "panel-demo.html")
    for src in ("appliance/rootfs/etc/panda/web/app.js", "appliance/rootfs/etc/panda/web/style.css",
                "docs/demo/mock.js"):
        assert read(ROOT, src) in demo, f"docs/panel-demo.html is stale: rerun scripts/build-demo.sh ({src})"


# --- OpenRC dependency graph -----------------------------------------------------------------

def parse_depend():
    deps = {}
    for path in sorted(glob.glob(os.path.join(INITD, "*"))):
        name = os.path.basename(path)
        text = read(path)
        m = re.search(r"^depend\(\)\s*\{(.*?)^\}", text, re.S | re.M)
        d = {"need": set(), "use": set(), "after": set(), "before": set(), "want": set()}
        if m:
            for line in m.group(1).splitlines():
                line = line.split("#", 1)[0].strip()
                parts = line.replace("\\", " ").split()
                # continuation lines belong to the previous keyword
                if parts and parts[0] in d:
                    cur = parts[0]
                    d[cur].update(parts[1:])
                elif parts and "cur" in locals() and line:
                    d[cur].update(parts)
        deps[name] = d
    return deps


def test_openrc_references_exist():
    deps = parse_depend()
    known = set(deps) | EXTERNAL
    for name, d in deps.items():
        for kind in ("need", "use", "want"):
            unknown = d[kind] - known
            assert not unknown, f"{name}: {kind} {sorted(unknown)} is not a known service"


def test_openrc_has_no_ordering_cycles():
    """`need`, `use`, `after` and `before` all impose a start order: it must be acyclic."""
    deps = parse_depend()
    edges = {n: set() for n in deps}            # n must start after edges[n]
    for n, d in deps.items():
        edges[n] |= (d["need"] | d["use"] | d["after"]) & set(deps)
        for b in d["before"]:
            if b in edges:
                edges[b].add(n)
    state = {}

    def visit(n, path):
        if state.get(n) == 1:
            raise AssertionError("ordering cycle: " + " -> ".join(path + [n]))
        if state.get(n) == 2:
            return
        state[n] = 1
        for m in sorted(edges[n]):
            visit(m, path + [n])
        state[n] = 2
    for n in sorted(edges):
        visit(n, [])


def restart_audio_order(admin):
    body = re.search(r"^restart_audio\(\) \{(.*?)^\}", admin, re.S | re.M).group(1)
    starts = []
    for line in body.splitlines():
        for cmd in line.split(";"):
            cmd = cmd.strip()
            m = re.fullmatch(r"rc-service (\S+) (start|restart)", cmd)
            if m:
                starts.append(m.group(1))
            elif cmd == "soloist start":
                starts.append("speaker-soloist")
    return starts


def test_restart_audio_starts_dependencies_first(admin):
    deps = parse_depend()
    order = restart_audio_order(admin)
    assert "speaker-pipewire" in order and "speaker-radio" in order
    for i, svc in enumerate(order):
        needed = (deps[svc]["need"] | deps[svc]["after"]) & set(order)
        late = {n for n in needed if order.index(n) > i}
        assert not late, f"restart_audio starts {svc} before {sorted(late)}"


def test_restart_audio_restarts_every_audio_service(admin):
    order = set(restart_audio_order(admin))
    for svc in ("speaker-pipewire", "speaker-wireplumber", "speaker-shairport", "speaker-soloist",
                "speaker-radio", "speaker-arbiter", "panda-bt", "panda-audio-init"):
        assert svc in order, f"restart_audio does not start {svc}"
