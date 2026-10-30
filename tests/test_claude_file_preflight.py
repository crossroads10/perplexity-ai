import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/claude_file_preflight.py"
spec = importlib.util.spec_from_file_location("file_preflight", SCRIPT)
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


@pytest.fixture
def fixture(tmp_path):
    path = tmp_path / "source.py"
    path.write_text("first = 1\nsecond = 2\n")
    state = tmp_path / "state"
    def invoke(phase, tool, *, file=path, session="one", agent=None, response=None, **inputs):
        event = {"hook_event_name": phase, "tool_name": tool, "session_id": session,
                 "tool_use_id": "call-" + tool, "tool_input": {"file_path": str(file), **inputs},
                 "tool_response": response, "agent_id": agent}
        return hook.process(event, state).get("hookSpecificOutput", {})
    def read(*, partial=False, **kwargs):
        text = kwargs.pop("content", path.read_text())
        return invoke("PostToolUse", "Read", response={"type": "text", "file": {
            "content": text.splitlines()[0] if partial else text,
            "startLine": 1, "numLines": 1 if partial else len(text.splitlines())}}, **kwargs)
    return path, state, invoke, read


def allowed(result):
    assert "permissionDecision" not in result, "Hook must neither grant permission nor deny a valid proposal"


def test_read_returns_disk_hash_and_edit_checks_actual_output(fixture):
    path, state, invoke, read = fixture
    snapshot = json.loads(read()["additionalContext"])
    assert snapshot["sha256"] == hook.digest(path.read_bytes())
    assert snapshot["bytes"] == len(path.read_bytes()) and snapshot["complete_read"]
    assert snapshot['read_range'] == {'start_line': 1, 'end_line': 2}
    assert snapshot['tool_use_id'] == 'call-Read'
    allowed(invoke("PreToolUse", "Edit", old_string="first = 1", new_string="first = 3"))
    path.write_text("first = 3\nsecond = 2\n")
    updated = json.loads(invoke("PostToolUse", "Edit")["additionalContext"])
    assert updated["sha256"] == hook.digest(path.read_bytes())
    allowed(invoke("PreToolUse", "Edit", old_string="first = 3", new_string="first = 4"))
    assert os.stat(state / "snapshots.sqlite3").st_mode & 0o777 == 0o600


@pytest.mark.parametrize("tool", ["Edit", "Write"])
def test_existing_file_requires_read_and_rejects_stale_hash(fixture, tool):
    path, _, invoke, read = fixture
    args = {"old_string": "first = 1", "new_string": "first = 3"} if tool == "Edit" else {"content": "replacement"}
    assert invoke("PreToolUse", tool, **args)["permissionDecision"] == "deny"
    read()
    path.write_text("first = 1\nsecond = 9\n")
    assert invoke("PreToolUse", tool, **args)["permissionDecision"] == "deny"
    read()
    allowed(invoke("PreToolUse", tool, **args))


@pytest.mark.parametrize("old,replace_all,passes", [
    ("missing", False, False), ("=", False, False), ("=", True, True),
    ("", False, False), ("first = 1", False, True),
])
def test_edit_matching(fixture, old, replace_all, passes):
    _, _, invoke, read = fixture
    read()
    result = invoke("PreToolUse", "Edit", old_string=old, new_string="replacement", replace_all=replace_all)
    assert (result.get("permissionDecision") != "deny") == passes


def test_partial_read_blocks_whole_file_write_and_unseen_edit(fixture):
    _, _, invoke, read = fixture
    assert not json.loads(read(partial=True)["additionalContext"])["complete_read"]
    assert invoke("PreToolUse", "Write", content="first = 3")["permissionDecision"] == "deny"
    assert invoke("PreToolUse", "Edit", old_string="second = 2", new_string="x")["permissionDecision"] == "deny"
    allowed(invoke("PreToolUse", "Edit", old_string="first = 1", new_string="first = 3"))


def test_unknown_or_truncated_read_invalidates_snapshot(fixture):
    _, _, invoke, read = fixture
    read()
    with pytest.raises(ValueError, match="differs from disk"):
        read(content="first = 1\n... truncated ...")
    assert invoke("PreToolUse", "Write", content="replacement")["permissionDecision"] == "deny"
    with pytest.raises(ValueError, match="Unrecognized Read"):
        invoke("PostToolUse", "Read", response="unknown schema")


def test_session_and_subagent_snapshots_are_separate(fixture):
    _, _, invoke, read = fixture
    read()
    for identity in ({"session": "two"}, {"agent": "child"}):
        assert invoke("PreToolUse", "Write", content="x", **identity)["permissionDecision"] == "deny"


def test_new_write_allowed_but_deleted_known_file_is_not(fixture):
    path, _, invoke, read = fixture
    new = path.with_name("new.py")
    allowed(invoke("PreToolUse", "Write", file=new, content="created\n"))
    new.write_text("created\n")
    assert json.loads(invoke("PostToolUse", "Write", file=new)["additionalContext"])["complete_read"]
    read()
    path.unlink()
    assert invoke("PreToolUse", "Write", content="replacement")["permissionDecision"] == "deny"


def test_post_write_mismatch_invalidates_snapshot(fixture):
    path, _, invoke, read = fixture
    read()
    allowed(invoke("PreToolUse", "Write", content="expected\n"))
    path.write_text("unexpected\n")
    with pytest.raises(ValueError, match="differs from the checked proposal"):
        invoke("PostToolUse", "Write")
    assert invoke("PreToolUse", "Write", content="another")["permissionDecision"] == "deny"


def test_failure_cleans_pending_and_never_records_success(fixture):
    _, _, invoke, read = fixture
    read()
    allowed(invoke("PreToolUse", "Write", content="expected"))
    assert invoke("PostToolUseFailure", "Write") == {}
    with pytest.raises(ValueError, match="checked proposal"):
        invoke("PostToolUse", "Write")


def test_cli_preflight_errors_fail_closed_without_tracebacks(tmp_path):
    for data in ("malformed", "[]", "null", json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Write"})):
        result = subprocess.run([sys.executable, str(SCRIPT), "--state-dir", str(tmp_path)],
                                input=data, text=True, capture_output=True, check=True)
        assert not result.stderr
        assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_install_preserves_settings_and_is_idempotent(tmp_path):
    settings = {"permissions": {"allow": ["Read"]}, "hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "existing-hook"}]}]}}
    path = tmp_path / ".claude/settings.local.json"
    path.parent.mkdir()
    original = json.dumps(settings).encode()
    path.write_bytes(original)
    hook.install(tmp_path)
    result = json.loads(path.read_text())
    assert result["permissions"] == settings["permissions"]
    assert result["hooks"]["PreToolUse"][0] == settings["hooks"]["PreToolUse"][0]
    once = path.read_bytes()
    hook.install(tmp_path)
    assert path.read_bytes() == once
    backups = list(path.parent.glob("*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == original


def test_crlf_read_and_edit_hashes_raw_file_bytes(fixture):
    path, _, invoke, read = fixture
    path.write_bytes(b"first = 1\r\nsecond = 2\r\n")
    snapshot = json.loads(read()["additionalContext"])
    assert snapshot["sha256"] == hook.digest(path.read_bytes())
    allowed(invoke("PreToolUse", "Edit", old_string="first = 1", new_string="first = 3"))
    path.write_bytes(b"first = 3\r\nsecond = 2\r\n")
    assert json.loads(invoke("PostToolUse", "Edit")["additionalContext"])["sha256"] == hook.digest(path.read_bytes())


def test_symlink_and_fifo_preflight_fail_closed(fixture):
    path, state, _, _ = fixture
    link = path.with_name("link")
    link.symlink_to(path)
    fifo = path.with_name("pipe")
    os.mkfifo(fifo)
    for target in (link, fifo):
        event = {"session_id": "one", "tool_use_id": "edit", "hook_event_name": "PreToolUse",
                 "tool_name": "Write", "tool_input": {"file_path": str(target), "content": "bad"}}
        result = subprocess.run([sys.executable, str(SCRIPT), "--state-dir", str(state)],
            input=json.dumps(event), text=True, capture_output=True, check=True, timeout=3)
        assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert path.read_text() == "first = 1\nsecond = 2\n"
