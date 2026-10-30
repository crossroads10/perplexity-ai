#!/usr/bin/env python3
"""Local Claude Code file preflight. No file content is persisted or transmitted.

Install per project with --install PROJECT, or pass hooks_config() as CLI settings.
Successful checks leave Claude Code's normal permission decisions in place.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import stat
import sys
import tempfile
import time


def digest(data):
    return hashlib.sha256(data).hexdigest()


def context(event, value):
    return {"hookSpecificOutput": {"hookEventName": event,
                                  "additionalContext": json.dumps(value, ensure_ascii=False)}}


def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
        "permissionDecision": "deny", "permissionDecisionReason": "File preflight: " + reason}}


def read_file(path):
    # Never follow a final-component symlink or block on a FIFO/device.
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Only regular files are supported")
        data = stream.read()
    text = data.decode("utf-8")
    if "\x00" in text:
        raise ValueError("Only UTF-8 text files are supported")
    return data, text.replace("\r\n", "\n")


def read_coverage(response, text):
    """Validate actual Read output against disk, rather than trusting input.limit."""
    file = response.get("file", {}) if isinstance(response, dict) else {}
    content = file.get("content")
    start, count = file.get("startLine"), file.get("numLines")
    if (not isinstance(content, str) or type(start) is not int or type(count) is not int
            or start < 1 or count < 0):
        raise ValueError("Unrecognized Read result; no verified snapshot was recorded")
    content = content.replace("\r\n", "\n")
    expected = "\n".join(text.split("\n")[start - 1:start - 1 + count])
    if content.rstrip("\n") != expected.rstrip("\n"):
        raise ValueError("Read result differs from disk or is truncated; Read the file again")
    complete = start == 1 and content.rstrip("\n") == text.rstrip("\n")
    return complete, start, start + count


def process(event, state_dir):
    phase, tool = event.get("hook_event_name"), event.get("tool_name")
    if tool not in {"Read", "Edit", "Write"}:
        return {}
    if phase not in {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied"}:
        return {}
    if phase == "PreToolUse" and tool == "Read":
        return {}
    inputs = event.get("tool_input", {})
    raw_path = inputs.get("file_path")
    if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
        raise ValueError("An absolute file_path is required")
    # Resolve parent aliases, but leave the final symlink for O_NOFOLLOW to reject.
    target = Path(raw_path)
    path = str(target.parent.resolve() / target.name)
    if not event.get("session_id") or not event.get("tool_use_id"):
        raise ValueError("Session and tool use IDs are required")
    actor = digest(json.dumps([event["session_id"], event.get("agent_id"),
                              event.get("transcript_path")]).encode())
    call = event["tool_use_id"]
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    database = state_dir / "snapshots.sqlite3"
    fd = os.open(database, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    db = sqlite3.connect(database, timeout=5)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS snapshots (actor TEXT, path TEXT, sha TEXT, "
                   "complete INTEGER, first_line INTEGER, end_line INTEGER, PRIMARY KEY(actor,path))")
        db.execute("CREATE TABLE IF NOT EXISTS pending (actor TEXT, call TEXT, path TEXT, sha TEXT, "
                   "complete INTEGER, PRIMARY KEY(actor,call))")
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT sha,complete,first_line,end_line FROM snapshots "
                              "WHERE actor=? AND path=?", (actor, path)).fetchone()
        if phase in {"PostToolUseFailure", "PermissionDenied"}:
            db.execute("DELETE FROM pending WHERE actor=? AND call=?", (actor, call))
            return {}
        if phase == "PreToolUse":
            try:
                data, text = read_file(path)
            except FileNotFoundError:
                if tool != "Write" or previous is not None:
                    return deny("File disappeared or is missing. Read it again before editing.")
                data, text = None, ""
            if data is not None and (previous is None or previous[0] != digest(data)):
                return deny("File has changed or has no verified Read in this session. Read it again.")
            if tool == "Write":
                proposed = inputs.get("content")
                if not isinstance(proposed, str):
                    return deny("Write.content must be a string.")
                if data is not None and not previous[1]:
                    return deny("Write would replace a file that was only partially read. Use Edit or Read the whole file first.")
                complete = True
            else:
                old, new, all_matches = inputs.get("old_string"), inputs.get("new_string"), inputs.get("replace_all", False)
                if not isinstance(old, str) or not old or not isinstance(new, str) or type(all_matches) is not bool:
                    return deny("Edit requires a nonempty old_string, a new_string, and a boolean replace_all.")
                count = text.count(old)
                if count == 0 or (count != 1 and not all_matches):
                    return deny(f"Edit.old_string has {count} exact matches; Read again and supply a unique match, or explicit replace_all.")
                observed = "\n".join(text.split("\n")[previous[2] - 1:previous[3] - 1])
                if not previous[1] and (old not in observed or all_matches):
                    return deny("Edit extends beyond the verified Read range. Read the required content first.")
                proposed = text.replace(old, new, -1 if all_matches else 1)
                complete = bool(previous[1])
            # Claude Code preserves CRLF for existing files; allow either line-ending
            # representation when checking PostToolUse, without weakening read hashes.
            expected = digest(proposed.replace("\r\n", "\n").encode())
            db.execute("INSERT OR REPLACE INTO pending VALUES (?,?,?,?,?)",
                       (actor, call, path, expected, complete))
            return context(phase, {"file_preflight": "passed", "file_path": path,
                                   "base_sha256": digest(data) if data is not None else None})
        if tool == "Read":
            # A failed/malformed Read must never leave an older authorization usable.
            db.execute("DELETE FROM snapshots WHERE actor=? AND path=?", (actor, path))
            data, text = read_file(path)
            complete, first, end = read_coverage(event.get("tool_response"), text)
            if previous and previous[0] == digest(data) and previous[1]:
                complete = True
        else:
            planned = db.execute("SELECT path,sha,complete FROM pending WHERE actor=? AND call=?",
                                 (actor, call)).fetchone()
            db.execute("DELETE FROM pending WHERE actor=? AND call=?", (actor, call))
            db.execute("DELETE FROM snapshots WHERE actor=? AND path=?", (actor, path))
            data, text = read_file(path)
            if not planned or planned[0] != path or planned[1] != digest(text.encode()):
                raise ValueError("Post-write content differs from the checked proposal; Read before further edits")
            # A partial edit may shift line ranges. Require a fresh Read before the
            # next change instead of carrying incorrect coordinates forward.
            if not planned[2]:
                return context(phase, {"file_path": path, "sha256": digest(data),
                    "bytes": len(data), "complete_read": False, "next_step": "Read before further edits"})
            complete, first, end = True, 1, len(text.split("\n")) + 1
        db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?)",
                   (actor, path, digest(data), complete, first, end))
        return context(phase, {"file_path": path, "sha256": digest(data),
                               "bytes": len(data), "complete_read": bool(complete),
                               "read_range": {"start_line": first, "end_line": max(first, end - 1)},
                               "tool_use_id": call})
    finally:
        # Commit invalidation even when a post-hook fails. Contents stay on disk;
        # this database holds only hashes and read coverage, never file text.
        db.commit()
        db.close()


def hooks_config():
    command = shlex.join([sys.executable, str(Path(__file__).resolve())])
    return {"hooks": {phase: [{"matcher": matcher,
        "hooks": [{"type": "command", "command": command, "timeout": 10}]}]
        for phase, matcher in [("PreToolUse", "Edit|Write"), ("PostToolUse", "Read|Edit|Write"),
                               ("PostToolUseFailure", "Edit|Write"), ("PermissionDenied", "Edit|Write")]}}


def install(project):
    path = project.resolve() / ".claude/settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_bytes() if path.exists() else None
    settings = json.loads(original) if original is not None else {}
    for phase, entries in hooks_config()["hooks"].items():
        current = settings.setdefault("hooks", {}).setdefault(phase, [])
        for entry in entries:
            if entry not in current:
                current.append(entry)
    updated = (json.dumps(settings, ensure_ascii=False, indent=2) + "\n").encode()
    if original == updated:
        return path
    if original is not None:
        backup = path.with_name(path.name + f".preflight-{time.time_ns()}.bak")
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(original)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(updated)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", type=Path)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".claude/bridge-preflight")
    args = parser.parse_args()
    if args.install:
        print(install(args.install))
        return
    event = {}
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("Hook input must be an object")
        result = process(event, args.state_dir)
    except Exception as exc:
        # Fail closed on preflight failures; post-hooks cannot undo an executed tool.
        if not isinstance(event, dict) or not event or event.get("hook_event_name") == "PreToolUse":
            result = deny(f"Cannot verify file ({type(exc).__name__}). Read the file and retry.")
        else:
            result = context(event.get("hook_event_name", "PostToolUse"),
                             {"file_preflight": "unverified", "reason": str(exc), "next_step": "Read the file again"})
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
