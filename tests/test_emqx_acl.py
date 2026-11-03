"""Tests for the production EMQX ACL (deploy/emqx-acl.conf, WOR-1092).

The broker is anonymous, so the ACL is the only thing that stops a browser on
the /mqtt websocket from publishing. These tests pin the rule list, the
mount in compose.production.yml and the restart guard in deploy/vscc-up.sh.
The rendering test needs `docker compose` and skips without it.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
ACL = ROOT / "deploy" / "emqx-acl.conf"
PROD = ROOT / "compose.production.yml"
UP = ROOT / "deploy" / "vscc-up.sh"
MOUNT = "./deploy/emqx-acl.conf:/opt/emqx/etc/acl.conf:ro"

EXPECTED = [
    '{deny, all, subscribe, ["$SYS/#", {eq, "#"}, {eq, "+/#"}]}',
    '{allow, {listener, "tcp:default"}, all, ["#"]}',
    '{allow, all, subscribe, ["#"]}',
    "{deny, all}",
]


def _rules() -> list[str]:
    """The ACL's rules in order, comments dropped and whitespace collapsed."""
    lines = [line.split("%%", 1)[0] for line in ACL.read_text().splitlines()]
    body = " ".join(lines)
    rules = [" ".join(rule.split()) + "}" for rule in body.split("}.")]
    return [rule for rule in rules if rule != "}"]


def test_acl_rules_are_exactly_the_reviewed_list() -> None:
    assert _rules() == EXPECTED


def test_acl_ends_with_deny_all() -> None:
    # independent of authorization.no_match and of EMQX_SECURITY_PROFILE
    assert _rules()[-1] == "{deny, all}"


def test_only_the_internal_listener_may_publish() -> None:
    for rule in _rules():
        if rule.startswith("{allow,") and not re.match(r"\{allow, [^,]+, subscribe,", rule):
            assert rule.startswith('{allow, {listener, "tcp:default"},'), rule


def test_acl_trusts_nothing_a_client_chooses() -> None:
    # anonymous broker: username and client id are whatever the client sends
    for rule in _rules():
        assert "username" not in rule and "clientid" not in rule, rule


def test_production_overlay_mounts_the_acl_read_only() -> None:
    text = PROD.read_text()
    emqx = text.split("\n  emqx:\n", 1)[1].split("\n  timescaledb:\n", 1)[0]
    assert f"- {MOUNT}" in emqx


def test_emqx_never_restarts_without_the_worker() -> None:
    # WOR-840: the worker does not reconnect to a restarted broker
    restarts = [
        line for line in UP.read_text().splitlines()
        if re.search(r"\brestart\b", line) and not line.lstrip().startswith(("#", "echo"))
    ]
    emqx = [line for line in restarts if re.search(r"\bemqx\b", line)]
    assert emqx, "vscc-up.sh no longer restarts EMQX on an outdated ACL"
    for line in emqx:
        assert re.search(r"\bworker\b", line), line


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_rendered_production_config_binds_the_acl() -> None:
    env = {**os.environ, "POSTGRES_PASSWORD": "unused", "MONITOR_IP": "192.0.2.1"}
    proc = subprocess.run(
        ["docker", "compose", "-p", "vscc", "-f", "docker-compose.yml",
         "-f", "compose.production.yml", "config", "--format", "json"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0 and "is not a docker command" in proc.stderr:
        pytest.skip("docker compose plugin not installed")
    assert proc.returncode == 0, proc.stderr
    config: dict[str, Any] = json.loads(proc.stdout)
    binds = [
        v for v in config["services"]["emqx"]["volumes"]
        if v.get("target") == "/opt/emqx/etc/acl.conf"
    ]
    assert len(binds) == 1
    assert binds[0]["type"] == "bind"
    assert binds[0]["read_only"] is True
    assert Path(binds[0]["source"]) == ACL
