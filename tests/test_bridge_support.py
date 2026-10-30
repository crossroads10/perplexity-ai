import asyncio
import copy
import json
import re

import pytest

from perplexity.server import anthropic, tool_bridge
from perplexity.server.bridge_support import AdmissionQueue, annotated_context, estimate_tokens, repair_feedback
from perplexity.server.job_store import JobError
from perplexity.server.tool_bridge import BridgeConfig, ToolBridge, canonical
from tests.conftest import request_for
from tests.test_anthropic_bridge import BODY, CALL, set_responses, events


@pytest.mark.parametrize('text,expected', [('abcd' * 100, 100), ('中文' * 100, 200), ('abcd中文', 3), ('😀', 1)])
def test_estimator_has_explicit_language_rules(text, expected):
    assert estimate_tokens(text) == expected


def test_context_boundaries_preserve_each_tool_result_and_all_client_metadata():
    original = {'system': [{'type': 'text', 'text': 'System rules'}], 'tools': [{'name': 'Edit'}], 'messages': [
        {'role': 'user', 'content': 'First'}, {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'read', 'name': 'Read', 'input': {}}]},
        {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'read', 'content': '  spaces\nraw[1]\r\n', 'is_error': False},
                                    {'type': 'text', 'text': 'Latest user instruction'}]}]}
    saved = copy.deepcopy(original)
    text, checks = annotated_context(original, canonical)
    assert original == saved
    assert len(checks) == 4  # begin, middle, one result, end
    assert dict(re.findall(r'CONTEXT_CHECKPOINT (c\d+)=(\w+)', text)) == checks
    assert canonical(original['messages'][-1]['content'][0]) in text
    assert 'Latest user instruction' in text and 'System rules' in text
    assert text.count('"tools":') == 1
    assert 'BEGIN_UNTRUSTED_TOOL_OUTPUT' in text
    metadata = json.loads(next(line.removeprefix('TOOL_RESULT_METADATA ') for line in text.splitlines()
                               if line.startswith('TOOL_RESULT_METADATA ')))
    import hashlib
    serialized = canonical(original['messages'][-1]['content'][0]['content']).encode()
    assert metadata['tool_use_id'] == 'read'
    assert metadata['serialized_content_bytes'] == len(serialized)
    assert metadata['serialized_content_sha256'] == hashlib.sha256(serialized).hexdigest()


def test_repair_feedback_does_not_echo_full_bad_answer():
    answer = '{"type":"final",broken ' + ('sensitive tail ' * 10000)
    try:
        json.loads(answer)
    except json.JSONDecodeError as exc:
        feedback = repair_feedback(exc, answer)
    assert len(feedback) < 500 and 'Invalid JSON' in feedback
    assert ('sensitive tail ' * 100) not in feedback
    feedback = repair_feedback(ValueError('Unexpected text after the JSON response'),
                               canonical({'type': 'final', 'text': 'x' * 20000}) + 'UNWANTED FOOTER')
    assert 'UNWANTED FOOTER' in feedback and len(feedback) < 300


@pytest.mark.asyncio
async def test_queue_fifo_cancel_and_overflow():
    queue = AdmissionQueue(lambda: 1, 2, 1)
    await queue.acquire()
    order = []
    async def waiter(number):
        await queue.acquire()
        order.append(number)
    a = asyncio.create_task(waiter(1))
    b = asyncio.create_task(waiter(2))
    await asyncio.sleep(0)
    with pytest.raises(JobError) as error:
        await queue.acquire()
    assert error.value.status_code == 429 and error.value.details['retry_after'] == 1
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    await queue.release()
    await asyncio.wait_for(b, 1)
    assert order == [2] and queue.active == 1 and not queue.waiters
    await queue.release()
    assert queue.active == 0


@pytest.mark.asyncio
async def test_queue_wait_timeout_and_shutdown_release_waiters():
    queue = AdmissionQueue(lambda: 1, 2, .01)
    await queue.acquire()
    with pytest.raises(JobError, match='queue wait timed out'):
        await queue.acquire()
    assert not queue.waiters
    pending = asyncio.create_task(queue.acquire())
    await asyncio.sleep(0)
    await queue.close()
    with pytest.raises(JobError, match='stopping'):
        await pending
    assert not queue.waiters
    await queue.release()


@pytest.fixture
async def bridge(api_runtime, monkeypatch):
    value = ToolBridge(api_runtime, BridgeConfig(account_id='one-account', allowed_tools=['*'], queue_timeout=.03))
    monkeypatch.setattr(tool_bridge, '_bridge', value)
    yield value
    await value.close()


@pytest.mark.asyncio
async def test_auto_capacity_is_bound_account_not_pool_size(bridge, api_runtime):
    assert bridge.config.max_runs is None and bridge.capacity() == 8
    api_runtime.pool.update_concurrency({'max_concurrency': 2}, 'one-account')
    assert bridge.capacity() == 2
    bridge.config.max_runs = 16
    assert bridge.capacity() == 16


@pytest.mark.asyncio
async def test_unknown_bridge_model_never_falls_back_or_leaks_capacity(bridge, api_runtime):
    response = await anthropic.messages(request_for('/v1/messages', {**BODY, 'model': 'not-a-real-model'}))
    assert response.status_code == 400
    assert not api_runtime.test_upstream.calls
    assert bridge.waiting.active == 0 and not bridge.runs


@pytest.mark.asyncio
async def test_upstream_model_change_rejects_tool_batch(bridge, api_runtime):
    async def upstream(client, **params):
        yield {'answer': canonical(CALL), 'status': 'COMPLETED', 'model_downgraded': True,
               '_follow_up': {'backend_uuid': 'downgraded', 'attachments': []}}
    api_runtime.transport = upstream
    response = await anthropic.messages(request_for('/v1/messages', BODY))
    assert response.status_code == 502 and 'changed the requested model' in json.loads(response.body)['error']['message']
    assert not bridge.receipts


@pytest.mark.asyncio
async def test_required_tool_repaired_after_final_and_headers_are_honest(bridge, api_runtime):
    seen = set_responses(api_runtime, [{'type': 'final', 'text': 'Not a tool'}, CALL])
    body = {**BODY, 'tool_choice': {'type': 'any'}, 'thinking': {'type': 'enabled', 'budget_tokens': 1000},
            'output_config': {'effort': 'high'}, 'system': [{'type': 'text', 'text': 'rules', 'cache_control': {'type': 'ephemeral'}}]}
    response = await anthropic.messages(request_for('/v1/messages', body))
    assert response.status_code == 200 and json.loads(response.body)['stop_reason'] == 'tool_use'
    assert len(seen) == 2 and 'REQUIRES tool_calls' in seen[1]['query']
    assert 'thinking' in response.headers['x-ignored-params'] and 'cache_control' in response.headers['x-ignored-params']
    assert 'output_config.effort' in response.headers['x-ignored-params']
    assert response.headers['x-bridge-backend'] == 'perplexity-ask'


@pytest.mark.asyncio
async def test_all_context_markers_required_before_dispatch(bridge, api_runtime):
    bridge.config.context_file_threshold_bytes = 1
    seen = []
    async def upstream(client, **params):
        seen.append(params)
        doc = next(iter(params['files'].values())).decode()
        checks = dict(re.findall(r'CONTEXT_CHECKPOINT (c\d+)=(\w+)', doc))
        ack = doc.rsplit('exact value: ', 1)[1].strip()
        assert len(checks) >= 3
        if len(seen) == 1:
            checks.pop('c1')
        run = next(iter(bridge.runs.values()))
        answer = {**CALL, 'context_ack': ack, 'context_checks': checks,
                  'request_text_checks': run.request_text_checks}
        yield {'answer': canonical(answer), 'status': 'COMPLETED', '_follow_up': {'backend_uuid': 'markers', 'attachments': []}}
    api_runtime.transport = upstream
    response = await anthropic.messages(request_for('/v1/messages', BODY))
    assert response.status_code == 200 and len(seen) == 2
    assert 'Missing or incorrect context_checks' in seen[1]['query']
    assert len(json.loads(response.body)['content']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_queue_failure_is_http_429_before_any_stream_headers(bridge, stream):
    bridge.config.max_runs = 1
    await bridge.waiting.acquire()
    try:
        response = await anthropic.messages(request_for('/v1/messages', {**BODY, 'stream': stream}))
        assert response.status_code == 429 and response.headers['retry-after'] == '1'
        assert response.headers['x-should-retry'] == 'true'
        assert response.headers['content-type'] == 'application/json'
        assert json.loads(response.body)['error']['type'] == 'rate_limit_error'
        assert not bridge.waiting.waiters and bridge.waiting.active == 1
        assert not bridge.requests
    finally:
        await bridge.waiting.release()


@pytest.mark.asyncio
async def test_cancel_queued_native_stream_reclaims_queue(bridge):
    bridge.config.max_runs = 1
    await bridge.waiting.acquire()
    try:
        pending = asyncio.create_task(anthropic.messages(request_for('/v1/messages',
            {**BODY, 'stream': True, 'metadata': {'session_id': 'cancel-queue'}})))
        for _ in range(100):
            if bridge.waiting.waiters:
                break
            await asyncio.sleep(.001)
        assert bridge.waiting.waiters
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not bridge.waiting.waiters and bridge.waiting.active == 1
        assert not bridge.requests
    finally:
        await bridge.waiting.release()


@pytest.mark.asyncio
async def test_queued_stream_proceeds_after_capacity_is_released(bridge, api_runtime):
    bridge.config.max_runs = 1
    bridge.waiting.timeout = .5
    await bridge.waiting.acquire()
    seen = set_responses(api_runtime, [CALL])
    pending = asyncio.create_task(anthropic.messages(request_for('/v1/messages', {**BODY, 'stream': True})))
    for _ in range(100):
        if bridge.waiting.waiters:
            break
        await asyncio.sleep(.001)
    assert bridge.waiting.waiters and not pending.done()
    await bridge.waiting.release()
    response = await asyncio.wait_for(pending, .5)
    assert response.status_code == 200
    frames = await events(response)
    assert frames[-1]['type'] == 'message_stop' and len(seen) == 1
    assert bridge.waiting.active == 0 and not bridge.waiting.waiters


@pytest.mark.asyncio
async def test_observability_counts_suffix_sources_and_session_errors_without_raw_id(bridge, api_runtime, caplog):
    seen = set_responses(api_runtime, [canonical(CALL) + '[1]', 'bad JSON'])
    body = {**BODY, 'metadata': {'session_id': 'private-session-id'}}
    await anthropic.messages(request_for('/v1/messages', body))
    body = {**body, 'messages': [{'role': 'user', 'content': 'different turn'}]}
    await anthropic.messages(request_for('/v1/messages', body))
    counts = next(iter(bridge.session_metrics.values()))
    assert counts['requests'] == 2 and counts['502'] == 1
    assert bridge.metrics['citation_suffixes'] == 1
    assert 'private-session-id' not in caplog.text
