import asyncio
import copy
import json
import re

import pytest

from perplexity.server import oai, tool_bridge
from perplexity.server.tool_bridge import BridgeConfig, ToolBridge, normalize_attachments, validate_history
from tests.conftest import request_for

SOURCE = "test_connector_" + "a" * 32
BODY = {"model": "perplexity-search", "stream": False,
        "messages": [{"role": "user", "content": "Read the test file"}],
        "tools": [{"type": "function", "function": {"name": "Read", "parameters": {
            "type": "object", "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"], "additionalProperties": False}}}]}
CALL = {"name": "Read", "arguments": {"file_path": "/fixture"}}


@pytest.fixture
async def bridge(api_runtime, monkeypatch):
    value = ToolBridge(api_runtime, BridgeConfig(SOURCE, "one-account", "s" * 40, allowed_tools=["*"], transport="mcp", max_runs=16, max_waiting=0))
    monkeypatch.setattr(tool_bridge, "_bridge", value)
    try:
        yield value
    finally:
        await value.close()


async def accepted(bridge, body=None, calls=None):
    body = copy.deepcopy(body or BODY)
    run, pending = await bridge.prepare(body)
    receipt = await bridge.submit_calls(run.id, calls or [CALL])
    payload = bridge.payload(run, await pending)
    message = payload["choices"][0]["message"]
    body["messages"].append(message)
    for call in message["tool_calls"]:
        body["messages"].append({"role": "tool", "tool_call_id": call["id"], "content": "real nonce"})
    return run, receipt, body


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True, None])
async def test_full_oai_multiple_generations_and_permission_error(bridge, api_runtime, stream):
    prompts = []
    async def upstream(client, **params):
        assert params["sources"] == [SOURCE]
        query = params["query"]
        prompts.append(query)
        run_id = re.search(r"run_id=(\S+)", query)[1]
        if len(prompts) < 3:
            receipt = await bridge.submit_calls(run_id, [CALL])
            assert receipt["status"] == "queued"
            assert "NOT executed" in receipt["message"]
            # No client execution result is needed to finish this generation.
            await asyncio.Event().wait()
        yield {"answer": "Observed: nonce2", "status": "COMPLETED",
               "_follow_up": {"backend_uuid": "real-backend", "attachments": []}}
    api_runtime.transport = upstream
    body = copy.deepcopy(BODY)
    body["stream"] = stream
    if stream is None:
        body.pop("stream")
    for step in (1, 2, 3):
        response = await oai.oai_chat_completions(request_for('/v1/chat/completions', body))
        if stream:
            chunks = [json.loads(part[6:]) async for part in response.body_iterator if part.startswith('data: {')]
            assert all("error" not in c for c in chunks)
            finish = chunks[-1]["choices"][0]["finish_reason"]
            message = chunks[0]["choices"][0]["delta"]
            for tc in message.get("tool_calls", []):
                tc.pop("index")
        else:
            assert response.status_code == 200, response.body
            choice = json.loads(response.body)["choices"][0]
            message, finish = choice["message"], choice["finish_reason"]
        assert not bridge.runs, "Finished generations must immediately release capacity"
        if step < 3:
            assert finish == "tool_calls"
            call = message["tool_calls"][0]
            body["messages"].extend([message, {"role": "tool", "tool_call_id": call["id"],
                "content": "Permission denied by user" if step == 1 else "nonce2", "is_error": step == 1}])
        else:
            assert finish == "stop" and message["content"] == "Observed: nonce2"
    assert 'Permission denied by user' in prompts[1]
    assert '"is_error":true' in prompts[1]
    assert 'nonce2' in prompts[2]
    assert len(set(re.search(r"run_id=(\S+)", q)[1] for q in prompts)) == 3


@pytest.mark.asyncio
async def test_parallel_results_survive_restart_and_user_metadata(bridge, api_runtime):
    run, _, body = await accepted(bridge, calls=[CALL, {"name": "Read", "arguments": {"file_path": "/other"}}])
    await bridge.release(run)
    assert (await api_runtime.wait(run.job["id"]))["state"] == "cancelled"
    body["messages"][-2:] = reversed(body["messages"][-2:])
    body["messages"].append({"role": "user", "content": "Continue with the actual results."})
    fresh = ToolBridge(api_runtime, bridge.config)
    try:
        next_run, _ = await fresh.prepare(body)
        assert next_run.id != run.id
        assert 'real nonce' in next_run.job['payload']['query']
    finally:
        await fresh.close()


@pytest.mark.asyncio
async def test_mcp_batch_retries_are_idempotent_even_after_release(bridge):
    run, receipt, _ = await accepted(bridge)
    await bridge.release(run)
    assert await bridge.submit_calls(run.id, [CALL]) == receipt
    with pytest.raises(ValueError, match="different batch"):
        await bridge.submit_calls(run.id, [{**CALL, "arguments": {"file_path": "/changed"}}])
    assert not bridge.runs


@pytest.mark.asyncio
async def test_batch_validation_is_atomic_and_wildcard_is_not_server_authority(bridge):
    run, pending = await bridge.prepare(copy.deepcopy(BODY))
    for calls in ([CALL, {"name": "Bash", "arguments": {"command": "pwd"}}],
                  [CALL, {"name": "Read", "arguments": {}}]):
        with pytest.raises(ValueError):
            await bridge.submit_calls(run.id, calls)
        assert not pending.done()
    await bridge.submit_calls(run.id, [CALL])
    assert len((await pending)["calls"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,parallel,calls", [("none", True, [CALL]), ("auto", False, [CALL, CALL])])
async def test_client_tool_controls_enforced(bridge, choice, parallel, calls):
    run, pending = await bridge.prepare({**copy.deepcopy(BODY), "tool_choice": choice, "parallel_tool_calls": parallel})
    with pytest.raises(ValueError):
        await bridge.submit_calls(run.id, calls)
    assert not pending.done()


@pytest.mark.asyncio
async def test_generation_timeout_releases_resources(bridge, api_runtime):
    bridge.config.run_timeout = .02
    run, pending = await bridge.prepare(copy.deepcopy(BODY))
    item = await asyncio.wait_for(pending, 1)
    assert "timed out" in str(item["error"])
    await asyncio.sleep(.03)
    assert not bridge.runs
    assert (await api_runtime.wait(run.job["id"]))["state"] == "cancelled"


@pytest.mark.asyncio
async def test_stream_cancel_before_first_iteration_releases_job(bridge, api_runtime):
    task = asyncio.create_task(bridge.response({**copy.deepcopy(BODY), "stream": True}))
    while not bridge.runs:
        await asyncio.sleep(.001)
    run = next(iter(bridge.runs.values()))
    while not run.job:
        await asyncio.sleep(.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not bridge.runs
    assert (await api_runtime.wait(run.job["id"]))["state"] == "cancelled"


@pytest.mark.asyncio
async def test_mcp_auth_surface_and_actionable_rejection(bridge):
    async def rpc(body, secret=None):
        return await tool_bridge.bridge_mcp(request_for('/bridge/x/mcp', body,
            params={"secret": secret or bridge.config.secret}, token=False))
    assert (await rpc({"id": 1, "method": "tools/list"}, "wrong")).status_code == 404
    tool = json.loads((await rpc({"id": 1, "method": "tools/list"})).body)["result"]["tools"][0]
    assert tool["name"] == "submit_client_tool_calls"
    assert tool["annotations"]["readOnlyHint"] is False
    response = await rpc({"id": 2, "method": "tools/call", "params": {
        "name": tool["name"], "arguments": {"run_id": "missing", "calls": [CALL]}}})
    result = json.loads(response.body)["result"]
    assert result["isError"] and 'Unknown' in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_external_schema_reference_rejected_before_submission(bridge):
    body = copy.deepcopy(BODY)
    body["tools"][0]["function"]["parameters"] = {"$ref": "https://example.com/schema"}
    with pytest.raises(ValueError, match="External"):
        await bridge.prepare(body)
    assert not bridge.runs


@pytest.mark.asyncio
async def test_full_history_over_200_messages_is_not_truncated(bridge):
    body = copy.deepcopy(BODY)
    body["messages"] = [{"role": "user", "content": f"turn-{i}"} for i in range(250)]
    run, _ = await bridge.prepare(body)
    assert 'turn-0' in run.job['payload']['query'] and 'turn-249' in run.job['payload']['query']


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "foreign"])
def test_invalid_tool_result_pairings_rejected(mutation):
    messages = [{"role": "user", "content": "Read"}, {"role": "assistant", "content": None,
        "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "real"}]
    if mutation == 'missing':
        messages.pop()
    elif mutation == 'duplicate':
        messages.append(messages[-1])
    else:
        messages[-1]['tool_call_id'] = 'foreign'
    with pytest.raises(ValueError):
        validate_history(messages)


def test_images_are_attached_once_and_not_inlined_in_history():
    part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}}
    messages = [{"role": "user", "content": [part, part]}]
    parsed, history = normalize_attachments(messages)
    assert len(parsed[0]['content']) == 1
    assert parsed[0]['content'][0]['type'] == 'input_file'
    assert 'base64' not in json.dumps(history)
    assert 'Attached file:' in json.dumps(history)


@pytest.mark.asyncio
async def test_pinned_missing_account_does_not_fall_back(api_runtime):
    with pytest.raises(Exception):
        await api_runtime.submit(query="test", account_id="missing", search_sources=[SOURCE])
    assert not api_runtime.test_upstream.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,code,finish", [
    ('{"type":"tool_calls","calls":[{"name":"Read","arguments":{"file_path":"/fixture"}}]}', 200, "tool_calls"),
    ('{"type":"final","text":"hello"}', 200, "stop"),
    ('I ran a tool successfully!', 502, None),
    ('{"type":"tool_calls","calls":[{"name":"Bash","arguments":{}}]}', 502, None),
])
async def test_json_model_codec_and_errors_before_stream_headers(bridge, api_runtime, answer, code, finish):
    bridge.config.transport = "json"
    async def upstream(client, **params):
        assert params['sources'] == []
        yield {"answer": answer, "status": "COMPLETED",
               "_follow_up": {"backend_uuid": "real-backend", "attachments": []}}
    api_runtime.transport = upstream
    response = await oai.oai_chat_completions(request_for('/v1/chat/completions', {**copy.deepcopy(BODY), 'stream':True}))
    assert response.status_code == code
    if finish:
        chunks = [json.loads(part[6:]) async for part in response.body_iterator if part.startswith('data: {')]
        assert chunks[-1]['choices'][0]['finish_reason'] == finish
        assert chunks[-1]['usage']['prompt_tokens'] > 0
    assert not bridge.runs


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", [
    "{}", "```json\n{}\n```", "```JSON\r\n{}\r\n```", "```\n{}\n```",
    "  ```json \n{}\n```  ", "```json\n{}\n```\n[1][22]",
    "```JSON{}```", "```{}```", "```json {} ```",
])
async def test_json_fences_preserve_complete_object_and_source_literals(bridge, api_runtime, wrapper):
    bridge.config.transport = "json"
    bridge.config.format_retries = 0
    envelope = json.dumps({"type": "final", "text": "a[1] and ```json stay literal"})
    async def upstream(client, **params):
        yield {"answer": wrapper.format(envelope), "status": "COMPLETED",
               "_follow_up": {"backend_uuid": "fences", "attachments": []}}
    api_runtime.transport = upstream
    payload = json.loads((await bridge.response(copy.deepcopy(BODY))).body)
    assert payload["choices"][0]["message"]["content"] == "a[1] and ```json stay literal"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    '```json\n{"type":"final","text":"ok"}\n``` trailing',
    '```json\n{"type":"final","text":"ok"}',
    '{"type":"final","text":"ok"}\n{}',
    'before ```json\n{"type":"final","text":"ok"}\n```',
])
async def test_incomplete_or_embedded_fences_are_rejected(bridge, api_runtime, answer):
    bridge.config.transport = "json"
    bridge.config.format_retries = 0
    async def upstream(client, **params):
        yield {"answer": answer, "status": "COMPLETED",
               "_follow_up": {"backend_uuid": "invalid-fences", "attachments": []}}
    api_runtime.transport = upstream
    with pytest.raises(Exception, match="valid bridge response"):
        await bridge.response(copy.deepcopy(BODY))


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", [0, 1])
async def test_json_sources_stay_disabled_during_format_repair(bridge, api_runtime, threshold):
    bridge.config.transport = "json"
    bridge.config.context_file_threshold_bytes = threshold
    seen = []
    async def upstream(client, **params):
        seen.append(params)
        if len(seen) == 1:
            answer = "invalid [1]"
        else:
            envelope = {"type": "final", "text": "ok"}
            run = next(iter(bridge.runs.values()))
            if run.context_ack:
                envelope["context_ack"] = run.context_ack
            if run.context_checks:
                envelope['context_checks'] = run.context_checks
            if run.request_text_checks:
                envelope['request_text_checks'] = run.request_text_checks
            answer = json.dumps(envelope)
        yield {"answer": answer, "status": "COMPLETED",
               "_follow_up": {"backend_uuid": "repair-sources", "attachments": []}}
    api_runtime.transport = upstream
    await bridge.response(copy.deepcopy(BODY))
    assert len(seen) == 2
    assert all(p["sources"] == [] for p in seen)
    assert all("Do not add citation markers" in p["query"] for p in seen)
    assert "Validation error:" in seen[1]["query"]


@pytest.mark.asyncio
async def test_explicit_capacity_admits_sixteen_and_releases_a_slot(bridge):
    assert bridge.config.max_runs == 16
    runs = [(await bridge.prepare(copy.deepcopy(BODY)))[0] for _ in range(16)]
    with pytest.raises(Exception, match="queue is full"):
        await bridge.prepare(copy.deepcopy(BODY))
    await bridge.release(runs[0])
    await bridge.prepare(copy.deepcopy(BODY))
    assert len(bridge.runs) == 16


@pytest.mark.parametrize("capacity", [True, 1.5, 0, 33])
def test_invalid_bridge_capacity_rejected(capacity):
    with pytest.raises(ValueError, match="capacity"):
        BridgeConfig(account_id="fixture", max_runs=capacity)


@pytest.mark.asyncio
@pytest.mark.parametrize("sources,expected", [(None, ["web"]), ([], []), ([SOURCE], [SOURCE])])
async def test_runtime_distinguishes_default_and_explicit_empty_sources(api_runtime, sources, expected):
    seen = []
    async def upstream(client, **params):
        seen.append(params["sources"])
        yield {"answer": "done", "status": "COMPLETED",
               "_follow_up": {"backend_uuid": "runtime-sources", "attachments": []}}
    api_runtime.transport = upstream
    job = await api_runtime.submit(query="test sources", search_sources=sources)
    final = await api_runtime.wait(job["id"])
    assert final["state"] == "completed"
    assert seen == [expected]
