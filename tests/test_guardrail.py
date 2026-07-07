"""Tests for the `aicodinggym guardrail` command group (Guardrail Gym Level 3).

Covers: config session-id persistence, the API client request shapes, the CLI
commands (help/parse, no-session guard, start/catalog/status/reset/finish/info
plus the flat plant verbs), and the dynamic objective-total scoreline.
Network is always mocked — no test hits a real backend.
"""

import json

import pytest
from click.testing import CliRunner

from aicodinggym import api, cli, config


# ── config: active-session persistence ────────────────────────────────────────

@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point config at a throwaway dir so we never touch the real ~/.aicodinggym."""
    cfg_dir = tmp_path / ".aicodinggym"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_dir / "config.json")
    monkeypatch.setattr(config, "CREDENTIALS_PATH", cfg_dir / "credentials.json")
    return cfg_dir


def test_guardrail_session_roundtrip(isolated_config):
    assert config.get_guardrail_session() is None
    config.set_guardrail_session("sess-123")
    assert config.get_guardrail_session() == "sess-123"
    config.clear_guardrail_session()
    assert config.get_guardrail_session() is None


def test_guardrail_session_persists_in_allowlist(isolated_config):
    config.set_guardrail_session("sess-abc")
    raw = json.loads(config.CONFIG_PATH.read_text())
    assert raw["guardrail_session_id"] == "sess-abc"


def test_guardrail_session_coexists_with_other_fields(isolated_config):
    cfg = config.load_config()
    cfg["user_id"] = "alice"
    config.save_config(cfg)
    config.set_guardrail_session("sess-xyz")  # separate load+save must not drop user_id
    assert config.load_config().get("user_id") == "alice"
    assert config.get_guardrail_session() == "sess-xyz"


def test_clear_is_a_noop_when_absent(isolated_config):
    config.clear_guardrail_session()  # must not raise
    assert config.get_guardrail_session() is None


# ── api: request shapes (endpoint + payload) ──────────────────────────────────

class _FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


@pytest.fixture
def capture_api(monkeypatch):
    """Record api._post/_get calls and return canned data instead of hitting HTTP."""
    calls = []

    def fake_post(endpoint, payload, timeout=api.TIMEOUT):
        calls.append({"method": "POST", "endpoint": endpoint, "payload": payload, "timeout": timeout})
        return {"ok": True}

    def fake_get(endpoint, timeout=api.TIMEOUT, stream=False):
        calls.append({"method": "GET", "endpoint": endpoint})
        return _FakeResp({"ok": True})

    monkeypatch.setattr(api, "_post", fake_post)
    monkeypatch.setattr(api, "_get", fake_get)
    return calls


def test_api_start_shape(capture_api):
    api.guardrail_start("user-1")
    assert capture_api[-1] == {"method": "POST", "endpoint": "guardrails/live/session",
                               "payload": {"userId": "user-1"}, "timeout": api.TIMEOUT}


def test_api_catalog_shape(capture_api):
    api.guardrail_catalog()
    assert capture_api[-1] == {"method": "GET", "endpoint": "guardrails/live/catalog"}


def test_api_plant_shape(capture_api):
    api.guardrail_plant("sess-9", "receive_email", {"body": "hi"})
    assert capture_api[-1]["endpoint"] == "guardrails/live/plant"
    assert capture_api[-1]["payload"] == {
        "sessionId": "sess-9", "plantKind": "receive_email", "args": {"body": "hi"}}


def test_api_status_and_reset_and_finish_endpoints(capture_api):
    api.guardrail_status("sess-9")
    assert capture_api[-1] == {"method": "GET", "endpoint": "guardrails/live/session/sess-9"}
    api.guardrail_reset("sess-9")
    assert capture_api[-1]["endpoint"] == "guardrails/live/session/sess-9/reset"
    api.guardrail_finish("sess-9")
    assert capture_api[-1]["endpoint"] == "guardrails/live/session/sess-9/finish"


def test_api_latest_encodes_user_id(capture_api):
    api.guardrail_latest("weird user/id")
    assert capture_api[-1]["endpoint"] == "guardrails/live/latest?userId=weird%20user/id"


# ── CLI: help / arg parsing ───────────────────────────────────────────────────

@pytest.fixture
def runner():
    return CliRunner()


def test_group_lists_all_subcommands(runner):
    res = runner.invoke(cli.main, ["guardrail", "--help"])
    assert res.exit_code == 0
    for sub in ("start", "catalog", "status", "reset", "finish", "info"):
        assert sub in res.output


def test_group_help_reflects_new_surface(runner):
    res = runner.invoke(cli.main, ["guardrail", "--help"])
    assert "catalog" in res.output
    assert "attack" not in res.output


@pytest.mark.parametrize("sub", [
    "start", "catalog", "status", "reset", "finish", "info",
    "email", "note", "event", "message", "webpage",
    "payee", "contact", "request", "consent", "policy",
])
def test_subcommand_help_ok(runner, sub):
    res = runner.invoke(cli.main, ["guardrail", sub, "--help"])
    assert res.exit_code == 0


def test_catalog_lists_surfaces_and_routines(runner, monkeypatch):
    monkeypatch.setattr(cli, "api_guardrail_catalog", lambda: {
        "plants": [{"surface": "tool_misuse", "kinds": ["payee", "request"]}],
        "routines": [{"id": "pay_rent", "label": "Pay my weekly rent"}],
    })
    res = runner.invoke(cli.main, ["guardrail", "catalog"])
    assert res.exit_code == 0, res.output
    assert "payee" in res.output and "pay_rent" in res.output


def test_attack_command_is_gone(runner):
    res = runner.invoke(cli.main, ["guardrail", "attack", "hi"])
    assert res.exit_code != 0  # no such command


# ── CLI: no active session must fail cleanly and hit no network ────────────────

@pytest.mark.parametrize("argv", [
    ["guardrail", "note", "--content", "x"],
    ["guardrail", "reset"],
    ["guardrail", "finish"],
])
def test_no_active_session_guard(runner, monkeypatch, argv):
    monkeypatch.setattr(cli, "get_guardrail_session", lambda: None)
    # Any network call would be a bug: make them explode if reached.
    def boom(*a, **k):
        raise AssertionError("network called despite no active session")
    for name in ("api_guardrail_plant", "api_guardrail_reset", "api_guardrail_finish"):
        monkeypatch.setattr(cli, name, boom)
    res = runner.invoke(cli.main, argv)
    assert res.exit_code == 1
    assert "No active Assistant Pro session" in res.output


# ── CLI: command behaviour (network mocked) ───────────────────────────────────

def test_start_saves_session_and_prints_summary(runner, monkeypatch):
    saved = {}
    monkeypatch.setattr(cli, "load_config", lambda: {"user_id": "alice"})
    monkeypatch.setattr(cli, "api_guardrail_start", lambda uid: {
        "sessionId": "sess-new", "model": "gpt-4o", "status": "OPEN",
        "scopes": ["email.read"], "objectives": [{"attackClass": "Prompt injection", "label": "Leak it"}],
    })
    monkeypatch.setattr(cli, "set_guardrail_session", lambda sid: saved.__setitem__("sid", sid))
    res = runner.invoke(cli.main, ["guardrail", "start"])
    assert res.exit_code == 0, res.output
    assert saved["sid"] == "sess-new"
    assert "sess-new" in res.output and "gpt-4o" in res.output


def test_flat_email_verb_maps_to_receive_email(runner, monkeypatch):
    monkeypatch.setattr(cli, "get_guardrail_session", lambda: "sess-1")
    seen = {}
    def fake_plant(sid, kind, args):
        seen.update(sid=sid, kind=kind, args=args)
        return {"plantId": "em_p1"}
    monkeypatch.setattr(cli, "api_guardrail_plant", fake_plant)
    res = runner.invoke(cli.main, ["guardrail", "email",
                                   "--from", "boss@corp.example", "--body", "do it"])
    assert res.exit_code == 0, res.output
    assert seen["kind"] == "receive_email"
    # Only provided options are sent (from -> "from"); unset options are omitted.
    assert seen["args"] == {"from": "boss@corp.example", "body": "do it"}
    assert "em_p1" in res.output


def test_flat_payee_verb_maps_to_payee_plantkind(runner, monkeypatch):
    monkeypatch.setattr(cli, "get_guardrail_session", lambda: "sess1")
    calls = {}
    monkeypatch.setattr(cli, "api_guardrail_plant",
                        lambda sid, kind, args: calls.update(sid=sid, kind=kind, args=args)
                        or {"plantId": "payee_p0"})
    res = runner.invoke(cli.main, ["guardrail", "payee",
                                   "--name", "Landlord LLC", "--account", "acct-attacker-999"])
    assert res.exit_code == 0, res.output
    assert calls["kind"] == "payee"
    assert calls["args"] == {"name": "Landlord LLC", "account": "acct-attacker-999"}


def test_old_plant_command_is_gone(runner):
    res = runner.invoke(cli.main, ["guardrail", "plant", "receive_email"])
    assert res.exit_code != 0


def test_status_latest_path_when_no_session(runner, monkeypatch):
    monkeypatch.setattr(cli, "get_guardrail_session", lambda: None)
    monkeypatch.setattr(cli, "load_config", lambda: {"user_id": "alice"})
    monkeypatch.setattr(cli, "api_guardrail_latest", lambda uid: {"session": {
        "sessionId": "sess-latest", "model": "gpt-4o", "status": "OPEN",
        "appState": {"scopes": ["email.read"]}, "medal": "silver",
        "captures": [{"objectiveId": "a"}, {"objectiveId": "b"}],
        "objectives": [1, 2, 3, 4],
    }})
    # If it wrongly took the session path, this would blow up.
    monkeypatch.setattr(cli, "api_guardrail_status", lambda sid: pytest.fail("used session path"))
    res = runner.invoke(cli.main, ["guardrail", "status"])
    assert res.exit_code == 0, res.output
    assert "sess-latest" in res.output and "2/4" in res.output


def test_finish_clears_active_session(runner, monkeypatch):
    monkeypatch.setattr(cli, "get_guardrail_session", lambda: "sess-1")
    monkeypatch.setattr(cli, "api_guardrail_finish", lambda sid: {
        "sessionId": sid, "status": "CLOSED", "medal": "none", "captures": []})
    cleared = {"done": False}
    monkeypatch.setattr(cli, "clear_guardrail_session", lambda: cleared.__setitem__("done", True))
    res = runner.invoke(cli.main, ["guardrail", "finish"])
    assert res.exit_code == 0, res.output
    assert cleared["done"] is True
    assert "Closed session sess-1" in res.output


def test_info_lists_objectives_and_tools(runner, monkeypatch):
    monkeypatch.setattr(cli, "api_guardrail_info", lambda: {
        "levelId": "guardrail-red-assistant-pro", "model": "gpt-4o",
        "maxTurns": 20, "maxPlants": 20, "maxInputChars": 20000,
        "objectives": [{"attackClass": "Prompt injection", "label": "Leak the recovery code", "hint": "…"}],
        "tools": ["send_email", "exec_code"], "plantKinds": ["receive_email", "web_page"],
    })
    res = runner.invoke(cli.main, ["guardrail", "info"])
    assert res.exit_code == 0, res.output
    assert "Leak the recovery code" in res.output
    assert "send_email" in res.output and "receive_email" in res.output


# ── _print_scoreline: objective total is derived, not hardcoded ───────────────

def test_scoreline_uses_state_objective_total(capsys):
    cli._print_scoreline({
        "medal": "silver", "capturedObjectives": ["a", "b"],
        "objectives": [1, 2, 3, 4], "status": "OPEN"})
    assert "objectives: 2/4" in capsys.readouterr().out


def test_scoreline_reflects_a_different_objective_count(capsys):
    # Proves the denominator is dynamic, not a literal 4.
    cli._print_scoreline({
        "medal": "bronze", "capturedObjectives": ["a"],
        "objectives": [1, 2, 3], "status": "OPEN"})
    assert "objectives: 1/3" in capsys.readouterr().out


def test_scoreline_without_objectives_shows_bare_count(capsys):
    # Turn results carry no objectives list -> no (possibly wrong) denominator.
    cli._print_scoreline({
        "medal": "bronze", "capturedObjectives": ["a"], "status": "OPEN"})
    out = capsys.readouterr().out
    assert "objectives: 1" in out and "/4" not in out


def test_scoreline_flags_gold_on_compromised(capsys):
    cli._print_scoreline({
        "medal": "gold", "capturedObjectives": ["a", "b", "c", "d"],
        "objectives": [1, 2, 3, 4], "status": "COMPROMISED"})
    out = capsys.readouterr().out
    assert "4/4" in out and "Gold" in out
