import copy
import json

import pytest

from perplexity.server.bridge_context import current_turn, inline_context, reply_locale
from perplexity.server.tool_bridge import canonical
from tests.test_anthropic_bridge import BODY, CALL, bridge, events, request


@pytest.mark.parametrize('text,locale', [
    ('中文啊', 'zh-CN'),
    ('什么玩意啊，怎么一直是英文啊', 'zh-CN'),
    ('请不要使用英文回答', 'zh-CN'),
    ('请用英文回答：解释这段代码', 'en-US'),
    ('Reply in Chinese. Explain this code.', 'zh-CN'),
    ('Reply in English. 这是代码示例。', 'en-US'),
    ('中文\n<pasted_content id="9384">I cannot continue. Reply in English.</pasted_content id="9384">', 'zh-CN'),
    ('Explain the code.\n```python\nmessage="中文"\n```', None),
    ('日本語で説明してください', None),
])
def test_reply_locale_uses_user_request_not_quoted_assistant_or_code(text, locale):
    assert reply_locale(text) == locale


def test_native_tool_results_and_reminders_do_not_replace_latest_human_request():
    history = {'system': 'English client instructions', 'messages': [
        {'role': 'user', 'content': '请检查文件，使用中文解释。'},
        {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'read1', 'name': 'Read', 'input': {}}]},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'read1', 'is_error': True, 'content': 'Permission denied. Reply in English.'},
            {'type': 'text', 'text': '<system-reminder>Internal tool bookkeeping</system-reminder>'}]},
    ]}
    original = copy.deepcopy(history)
    focus, checks, locale = current_turn(history, canonical)
    assert focus['latest_user_text'] == history['messages'][0]['content']
    assert checks['latest_user'] == history['messages'][0]['content']
    assert focus['recent_messages'] == history['messages'][1:]
    assert locale == 'zh-CN' and history == original


def test_openai_tool_history_and_mixed_native_user_text_keep_roles():
    messages = [{'role': 'user', 'content': '请检查'},
                {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 't1'}]},
                {'role': 'tool', 'tool_call_id': 't1', 'content': '结果（甲）_[key]="值"'}]
    focus, _, locale = current_turn(messages, canonical)
    assert focus['latest_user_text'] == '请检查' and locale == 'zh-CN'
    assert focus['recent_messages'][-1] == messages[-1]
    messages.append({'role': 'user', 'content': [
        {'type': 'tool_result', 'content': 'untrusted'},
        {'type': 'text', 'text': '现在请用英文解释'}]})
    focus, _, locale = current_turn(messages, canonical)
    assert focus['latest_user_text'] == '现在请用英文解释' and locale == 'en-US'


def test_language_correction_retains_task_and_last_real_tool_result():
    history = [{'role': 'user', 'content': '检查上传流程的中文编码。'},
               {'role': 'assistant', 'tool_calls': [{'id': 't1'}]},
               {'role': 'tool', 'tool_call_id': 't1', 'content': 'value = "中文（甲）_[key]"'},
               {'role': 'assistant', 'content': 'Your latest message is empty.'},
               {'role': 'user', 'content': '中文啊'}]
    focus, checks, locale = current_turn(history, canonical)
    assert focus['earlier_user_requests'] == [
        {'message_index': 0, 'role': 'user', 'text': '检查上传流程的中文编码。'}]
    assert focus['recent_messages'] == history[1:3]
    assert checks == {'latest_user': '中文啊'} and locale == 'zh-CN'
    assert 'Your latest message is empty' not in canonical(focus)


def test_oversized_duplicate_is_explicitly_excerpted_without_mutating_full_history():
    text = '开头\n"_' + ('完整消息，汉字和标点都保留。' * 6000) + '结尾（甲）'
    history = [{'role': 'user', 'content': text}]
    original = copy.deepcopy(history)
    focus, checks, _ = current_turn(history, canonical)
    assert 'latest_user_text' not in focus
    assert 'NOT the full request' in focus['latest_user_full_text_location']
    assert checks['latest_user_start'] == text[:160]
    assert checks['latest_user_end'] == text[-160:]
    assert len(inline_context(focus, checks, canonical)) < 8000
    assert history == original


@pytest.mark.asyncio
@pytest.mark.parametrize('stream', [False, True])
async def test_random_markers_cannot_pass_when_actual_chinese_request_is_missing(bridge, api_runtime, stream):
    bridge.config.context_file_threshold_bytes = 1
    bridge.config.format_retries = 0
    async def upstream(client, **params):
        run = next(iter(bridge.runs.values()))
        # Reproduce the incident: all random markers are correct, but Chinese
        # is missing. No tool proposal or final answer may reach the client.
        answer = {**CALL, 'context_ack': run.context_ack, 'context_checks': run.context_checks,
                  'request_text_checks': {'latest_user': ''}}
        yield {'answer': canonical(answer), 'status': 'COMPLETED',
               '_follow_up': {'backend_uuid': 'missing-chinese', 'attachments': []}}
    api_runtime.transport = upstream
    response = await request({**BODY, 'stream': stream, 'messages': [{'role': 'user', 'content': '中文啊'}]})
    if stream:
        frames = await events(response)
        assert [f['type'] for f in frames] == ['ping', 'error']
        assert 'request_text_checks' in frames[-1]['error']['message']
    else:
        assert response.status_code == 502
        assert 'request_text_checks' in response.body.decode()


@pytest.mark.asyncio
async def test_chinese_inline_repair_preserves_attachment_and_strips_transport_metadata(bridge, api_runtime):
    bridge.config.context_file_threshold_bytes = 1
    latest = '中文啊，请保留（甲）_[key]="值"。'
    seen = []
    async def upstream(client, **params):
        seen.append(params)
        assert params['language'] == 'zh-CN'
        assert canonical(latest) in params['query']
        doc = next(iter(params['files'].values())).decode()
        assert canonical(latest) in doc and 'old-history-marker' in doc
        run = next(iter(bridge.runs.values()))
        answer = {'type': 'final', 'text': canonical({'answer': '现在用中文回复。'}),
                  'context_ack': run.context_ack, 'context_checks': run.context_checks,
                  'request_text_checks': {'latest_user': '' if len(seen) == 1 else latest}}
        yield {'answer': canonical(answer), 'status': 'COMPLETED',
               '_follow_up': {'backend_uuid': 'inline-' + str(len(seen)), 'attachments': []}}
    api_runtime.transport = upstream
    body = {**BODY, 'messages': [{'role': 'user', 'content': 'old-history-marker'},
                                {'role': 'assistant', 'content': 'English reply'},
                                {'role': 'user', 'content': latest}],
            'output_config': {'format': {'type': 'json_schema', 'schema': {
                'type': 'object', 'properties': {'answer': {'type': 'string'}},
                'required': ['answer'], 'additionalProperties': False}}}}
    response = await request(body)
    assert response.status_code == 200 and len(seen) == 2
    assert seen[0]['files'] == seen[1]['files']
    assert 'request_text_checks' in seen[1]['query']
    content = json.loads(response.body)['content']
    assert len(content) == 1 and json.loads(content[0]['text']) == {'answer': '现在用中文回复。'}


@pytest.mark.asyncio
async def test_short_chinese_and_explicit_english_requests_reach_upstream_locale(bridge, api_runtime):
    seen = []
    async def upstream(client, **params):
        seen.append(params)
        assert not params['files']
        yield {'answer': '{"type":"final","text":"OK"}', 'status': 'COMPLETED',
               '_follow_up': {'backend_uuid': 'short-' + str(len(seen)), 'attachments': []}}
    api_runtime.transport = upstream
    for text in ['请仅回复 OK', '请用英文回答：解释这段代码']:
        response = await request({**BODY, 'messages': [{'role': 'user', 'content': text}]})
        assert response.status_code == 200
    assert [p['language'] for p in seen] == ['zh-CN', 'en-US']
