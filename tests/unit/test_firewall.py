"""firewall.nft: valid syntax (checked in a throwaway user+network namespace) and the policy it states."""
import os
import re
import shutil
import subprocess

import pytest

from tests.conftest import PANDA

NFT = os.path.join(PANDA, "firewall.nft")


def nft_check(path):
    if not shutil.which("nft") or not shutil.which("unshare"):
        pytest.skip("nft or unshare missing")
    r = subprocess.run(["unshare", "-rn", "nft", "-c", "-f", path], capture_output=True, text=True, timeout=30)
    if r.returncode != 0 and "Operation not permitted" in r.stderr and "Error: Could not process rule" not in r.stderr:
        pytest.skip("no user namespaces here")
    return r


def test_the_ruleset_is_valid_nftables():
    r = nft_check(NFT)
    assert r.returncode == 0, r.stderr


def test_a_broken_ruleset_is_caught_by_the_check(tmp_path):
    bad = tmp_path / "bad.nft"
    bad.write_text(open(NFT).read().replace("policy drop;", "policy dropp;", 1))
    assert nft_check(str(bad)).returncode != 0


def test_default_policy_is_drop_for_input_and_forward():
    text = open(NFT).read()
    for chain in ("input", "forward"):
        m = re.search(r"chain %s \{(.*?)\n\t\}" % chain, text, re.S)
        assert m and "policy drop;" in m.group(1), chain


def test_only_local_networks_are_accepted():
    text = open(NFT).read()
    sets = dict(re.findall(r"set (lan4|lan6) \{.*?elements = \{([^}]*)\}", text, re.S))
    assert set(sets) == {"lan4", "lan6"}
    for nets in sets.values():
        assert "0.0.0.0/0" not in nets and "::/0" not in nets
    assert "10.0.0.0/8" in sets["lan4"] and "192.168.0.0/16" in sets["lan4"] and "fe80::/10" in sets["lan6"]
    assert not re.search(r"(?m)^\s*tcp dport \d+ accept", text)          # no service is opened to everyone


def test_port_80_is_redirected_to_the_unprivileged_web_page():
    assert re.search(r"tcp dport 80 redirect to :8080", open(NFT).read())
