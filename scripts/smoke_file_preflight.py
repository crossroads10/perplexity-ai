"""Exercise real Claude Code file hooks against a scripted localhost model.

No Perplexity/model calls. All tool execution is confined to a fresh fixture.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time

from claude_file_preflight import hooks_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--retry-probe", action="store_true", help="Return two HTTP 429 responses before the first model reply")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="preflight-", dir=args.output_dir.resolve()))
    target, created = root / "source.py", root / "created.txt"
    target.write_text("first = 1\nsecond = 2\n")
    calls = [("Read", {"file_path": str(target)}),
             ("Edit", {"file_path": str(target), "old_string": "first = 1", "new_string": "first = 3"}),
             ("Edit", {"file_path": str(target), "old_string": "first = 3", "new_string": "first = 4"}),
             ("Read", {"file_path": str(target), "limit": 1}),
             ("Write", {"file_path": str(target), "content": "first = 4\n"}),
             ("Read", {"file_path": str(target)}),
             ("Write", {"file_path": str(target), "content": "first = 4\nsecond = 9\n"}),
             ("Write", {"file_path": str(created), "content": "created safely\n"})]
    requests = []
    retry_requests, retry_times = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            if "count_tokens" in self.path:
                data = b'{"input_tokens":1000}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if args.retry_probe and len(retry_requests) < 2:
                retry_requests.append(body)
                retry_times.append(time.monotonic())
                data = b'{"type":"error","error":{"type":"rate_limit_error","message":"Synthetic queue busy"}}'
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Retry-After", "1")
                self.send_header("X-Should-Retry", "true")
                self.end_headers()
                self.wfile.write(data)
                return
            if args.retry_probe and not requests:
                retry_times.append(time.monotonic())
            index = len(requests)
            requests.append(body)
            if index == 2:
                assert target.read_text() == "first = 3\nsecond = 2\n"
                target.write_text("first = 3\nsecond = 9\n")
            if index < len(calls):
                name, inputs = calls[index]
                block = {"type": "tool_use", "id": f"toolu_fixture_{index}", "name": name, "input": inputs}
                reason = "tool_use"
            else:
                block, reason = {"type": "text", "text": "fixture complete"}, "end_turn"
            payload = {"id": f"msg_fixture_{index}", "type": "message", "role": "assistant",
                       "model": body["model"], "content": [block], "stop_reason": reason,
                       "stop_sequence": None, "usage": {"input_tokens": 1000, "output_tokens": 50}}
            if not body.get("stream"):
                data = json.dumps(payload).encode()
                content_type = "application/json"
            else:
                empty = {**block, "input": {}} if reason == "tool_use" else {"type": "text", "text": ""}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])} if reason == "tool_use" else {"type": "text_delta", "text": block["text"]}
                events = [{"type": "message_start", "message": {**payload, "content": [], "stop_reason": None}},
                          {"type": "content_block_start", "index": 0, "content_block": empty},
                          {"type": "content_block_delta", "index": 0, "delta": delta},
                          {"type": "content_block_stop", "index": 0},
                          {"type": "message_delta", "delta": {"stop_reason": reason, "stop_sequence": None}, "usage": {"output_tokens": 50}},
                          {"type": "message_stop"}]
                data = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
                content_type = "text/event-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    settings = hooks_config()
    for entries in settings["hooks"].values():
        for entry in entries:
            entry["hooks"][0]["command"] += " --state-dir " + shlex.quote(str(root / "state"))
    settings_path = root / "settings.json"
    settings_path.write_text(json.dumps(settings))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_")) and k != "CLAUDECODE"}
    env.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}", ANTHROPIC_API_KEY="fixture-only",
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    try:
        result = subprocess.run(["claude", "-p", "Run the isolated file preflight fixture.",
            "--setting-sources", "", "--settings", str(settings_path), "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--tools", "Read,Edit,Write",
            "--allowedTools", "Read,Edit,Write", "--model", "claude-sonnet-4-5",
            "--max-turns", "12", "--no-session-persistence", "--output-format", "stream-json", "--verbose"],
            cwd=root, env=env, capture_output=True, text=True, timeout=90)
    finally:
        server.shutdown()
        server.server_close()
    (root / "claude.jsonl").write_text(result.stdout)
    (root / "stderr.log").write_text(result.stderr)
    (root / "requests.json").write_text(json.dumps(requests, ensure_ascii=False, indent=2))
    results = {}
    if requests:
        for message in requests[-1]["messages"]:
            for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    results[block["tool_use_id"]] = block
    checks = {"exit_success": result.returncode == 0, "model_turns": len(requests) == 9,
        "all_results": len(results) == 8,
        "stale_edit_denied": results.get("toolu_fixture_2", {}).get("is_error") is True,
        "partial_write_denied": results.get("toolu_fixture_4", {}).get("is_error") is True,
        "valid_calls_succeeded": all(i in (2, 4) or (f"toolu_fixture_{i}" in results and not results[f"toolu_fixture_{i}"].get("is_error")) for i in range(8)),
        "hash_returned_to_model": '"sha256"' in json.dumps(requests).replace('\\"', '"'),
        "final_file": target.read_text() == "first = 4\nsecond = 9\n",
        "new_file": created.exists() and created.read_text() == "created safely\n"}
    if args.retry_probe:
        checks.update(retried_twice=len(retry_requests) == 2,
                      retry_after_respected=len(retry_times) == 3 and all(b-a >= .95 for a,b in zip(retry_times, retry_times[1:])),
                      retry_body_preserved=bool(requests) and all(r == requests[0] for r in retry_requests))
    summary = {"verified": all(checks.values()), "checks": checks, "project": str(root)}
    if args.retry_probe:
        summary['retry_intervals_s'] = [round(b-a,3) for a,b in zip(retry_times,retry_times[1:])]
    (root / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    if not summary["verified"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
