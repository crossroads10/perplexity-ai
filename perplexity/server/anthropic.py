"""Anthropic Messages adapter, preserving client context before Ask translation.

The existing gateway can relay this endpoint without the lossy OAI conversion.
Tool execution remains exclusively in Claude Code. Token usage is estimated.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json

import anyio
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from starlette.responses import JSONResponse, StreamingResponse

from .app import MCP_TOKEN, get_job_runtime, mcp
from .chat_input import read_json
from .job_store import JobError
from .tool_bridge import canonical, get_bridge, validate_schema
from .bridge_support import estimate_tokens


def capability_headers(body):
    ignored = [key for key in ('thinking', 'max_tokens', 'temperature', 'top_p', 'top_k',
                               'stop_sequences', 'effort') if key in body]
    if 'effort' in body.get('output_config', {}):
        ignored.append('output_config.effort')
    def has_cache(value):
        if isinstance(value, dict):
            return 'cache_control' in value or any(has_cache(v) for v in value.values())
        return isinstance(value, list) and any(has_cache(v) for v in value)
    if has_cache(body):
        ignored.append('cache_control')
    headers = {'X-Usage-Estimated': 'true', 'X-Token-Estimator': 'unicode-heuristic-v1',
               'X-Bridge-Backend': 'perplexity-ask'}
    if ignored:
        headers['X-Ignored-Params'] = ', '.join(ignored)
    return headers


def authenticate(request):
    supplied = [request.headers.get('x-api-key', ''), request.headers.get('authorization', '').removeprefix('Bearer ')]
    return any(hmac.compare_digest(value, MCP_TOKEN) for value in supplied)


def error_data(exc):
    status = getattr(exc, 'status_code', 400 if isinstance(exc, ValueError) else 502)
    kind = 'invalid_request_error' if status in (400, 409, 413, 422) else 'rate_limit_error' if status == 429 else 'api_error'
    message = str(exc) if isinstance(exc, (ValueError, JobError)) else 'Model bridge failed'
    error = {'type': kind, 'message': message}
    if status == 429:
        error['retry_after'] = getattr(exc, 'details', {}).get('retry_after', 1)
    return {'type': 'error', 'error': error}, status


def normalize_request(body):
    """Return an execution-compatible representation AND lossless prompt context.

    Native references/roles/error flags are retained in context, not flattened
    away. Image/PDF payloads use the existing bounded attachment resolver.
    """
    if not isinstance(body, dict):
        raise ValueError('Request must be an object')
    if not isinstance(body.get('tools', []), list):
        raise ValueError('tools must be a list')
    for field in ('metadata', 'output_config'):
        if field in body and not isinstance(body[field], dict):
            raise ValueError(f'{field} must be an object')
    if not isinstance(body.get('messages'), list) or not body['messages']:
        raise ValueError('messages must be a nonempty list')
    if not isinstance(body.get('model'), str) or not body['model']:
        raise ValueError('model is required')
    if 'stream' in body and type(body['stream']) is not bool:
        raise ValueError('stream must be a boolean')
    context = copy.deepcopy(body)
    attachments = {}

    def blocks(value):
        if isinstance(value, str):
            return [{'type': 'text', 'text': value}]
        if not isinstance(value, list) or any(not isinstance(b, dict) for b in value):
            raise ValueError('Message content must be text or content blocks')
        return value

    def convert(value):
        result = []
        for block in blocks(value):
            kind = block.get('type')
            if kind == 'text':
                if not isinstance(block.get('text'), str):
                    raise ValueError('text blocks require string text')
                result.append({'type': 'text', 'text': block['text']})
            elif kind in ('image', 'document'):
                source = block.get('source', {})
                if not isinstance(source, dict):
                    raise ValueError('Attachment source must be an object')
                if kind == 'document' and source.get('type') == 'text':
                    result.append({'type':'text', 'text':source.get('data', '')})
                    continue
                extensions = {'image/png':'png','image/jpeg':'jpg','image/webp':'webp','image/gif':'gif','application/pdf':'pdf'}
                media = source.get('media_type')
                if source.get('type') != 'base64' or media not in extensions or not isinstance(source.get('data'), str):
                    raise ValueError('Images/documents require supported base64 content or a text document')
                digest = hashlib.sha256(canonical(source).encode()).hexdigest()[:24]
                name = f'client-{digest}.{extensions[media]}'
                attachments[canonical(source)] = name
                result.append({'type':'input_file','filename':name,'file_data':f'data:{media};base64,'+source['data']})
            elif kind == 'tool_reference':
                if not isinstance(block.get('tool_name'), str):
                    raise ValueError('tool_reference requires tool_name')
                result.append({'type':'text','text':canonical(block)})
            elif kind in ('thinking', 'redacted_thinking'):
                # Preserve native history as data; never fabricate new signed blocks.
                result.append({'type':'text','text':canonical(block)})
            elif kind == 'compaction':
                result.append({'type':'text','text':block.get('content', '')})
            else:
                raise ValueError(f"Unsupported content block type: {kind}")
        return result

    messages = []
    if body.get('system'):
        messages.append({'role':'system','content':convert(body['system'])})
    for message in body['messages']:
        if not isinstance(message, dict) or message.get('role') not in ('user','assistant','system'):
            raise ValueError('Unsupported message role')
        role = message['role']
        content = blocks(message.get('content', ''))
        if role == 'assistant':
            calls, other = [], []
            for block in content:
                if block.get('type') == 'tool_use':
                    if not isinstance(block.get('input'), dict):
                        raise ValueError('tool_use.input must be an object')
                    calls.append({'id':block.get('id'),'type':'function','function':{
                        'name':block.get('name'),'arguments':canonical(block['input'])}})
                else:
                    other.append(block)
            converted = {'role':role,'content':convert(other)}
            if calls:
                converted['tool_calls'] = calls
            messages.append(converted)
        else:
            other = []
            for block in content:
                if block.get('type') == 'tool_result':
                    if role != 'user':
                        raise ValueError('tool_result must be in a user message')
                    if type(block.get('is_error', False)) is not bool:
                        raise ValueError('tool_result.is_error must be a boolean')
                    messages.append({'role':'tool','tool_call_id':block.get('tool_use_id'),
                        'content':convert(block.get('content', '')), 'is_error':block.get('is_error', False)})
                else:
                    other.append(block)
            if other:
                messages.append({'role':role,'content':convert(other)})

    tools = []
    for tool in body.get('tools', []):
        if not isinstance(tool, dict):
            raise ValueError('Invalid tool definition')
        kind = tool.get('type', 'custom')
        if kind != 'custom':
            raise ValueError(f"Input tag '{kind}' is unsupported by the Ask bridge; use client function tools")
        if not isinstance(tool.get('description', ''), str):
            raise ValueError('Tool description must be a string')
        fn = {'name':tool.get('name'),'description':tool.get('description',''),'parameters':tool.get('input_schema',{})}
        if tool.get('defer_loading'):
            fn['description'] += '\nThis client tool is deferred. Use the client ToolSearch tool to load it before use when ToolSearch is available.'
        tools.append({'type':'function','function':fn})
    choice = body.get('tool_choice', {'type':'auto'})
    if not isinstance(choice, dict) or choice.get('type') not in ('auto','any','none','tool'):
        raise ValueError('Invalid tool_choice')
    disable_parallel = choice.get('disable_parallel_tool_use', False)
    if type(disable_parallel) is not bool:
        raise ValueError('disable_parallel_tool_use must be a boolean')
    mapped_choice = {'auto':'auto','any':'required','none':'none'}.get(choice['type'])
    if choice['type'] == 'tool':
        mapped_choice = {'type':'function','function':{'name':choice.get('name')}}
    normalized = {'model':body['model'],'messages':messages,'tools':tools,
                  'tool_choice':mapped_choice,'parallel_tool_calls':not disable_parallel,'stream':body.get('stream',False)}
    output = body.get('output_config', {}).get('format')
    if output:
        if not isinstance(output, dict) or output.get('type') != 'json_schema' or not isinstance(output.get('schema'), dict):
            raise ValueError('output_config.format must be json_schema with a schema')
        validate_schema(output['schema'])
        try:
            Draft202012Validator.check_schema(output['schema'])
        except SchemaError as exc:
            raise ValueError('Invalid output JSON schema') from exc
        normalized['response_format'] = {'type':'json_schema','json_schema':{'schema':output['schema']}}

    def redact(value):
        if isinstance(value, dict):
            if value.get('type') in ('image','document') and isinstance(value.get('source'), dict):
                name = attachments.get(canonical(value['source']))
                if name:
                    return {**value,'source':{'type':'attachment','filename':name}}
            return {k:redact(v) for k,v in value.items()}
        if isinstance(value, list):
            return [redact(v) for v in value]
        return value
    return normalized, redact(context)


def request_identity(request, body):
    explicit = request.headers.get('idempotency-key')
    if explicit is not None:
        if not explicit.strip() or len(explicit) > 200:
            raise ValueError('Idempotency-Key must be 1..200 characters')
        return 'explicit:' + explicit
    metadata = body.get('metadata') or {}
    scope = metadata.get('session_id') or metadata.get('user_id')
    # Only session-scoped complete requests are replayable. Never cache an
    # identical unscoped prompt (the user may intend a fresh read).
    if not isinstance(scope, str) or not scope:
        return None
    stable = {k:v for k,v in body.items() if k != 'stream'}
    return 'session:' + hashlib.sha256((scope+canonical(stable)).encode()).hexdigest()


def native_payload(payload):
    choice = payload['choices'][0]
    message = choice['message']
    content = []
    if payload.get('_bridge_progress'):
        content.append({'type':'text','text':payload['_bridge_progress']})
    if message.get('content'):
        content.append({'type':'text','text':message['content']})
    for call in message.get('tool_calls', []):
        content.append({'type':'tool_use','id':call['id'],'name':call['function']['name'],
                        'input':json.loads(call['function']['arguments'])})
    if not content:
        content.append({'type':'text','text':''})
    usage = payload.get('usage', {})
    return {'id':'msg_'+payload['id'].removeprefix('chatcmpl-'),'type':'message','role':'assistant',
        'model':payload['model'],'content':content,
        'stop_reason':'tool_use' if choice['finish_reason']=='tool_calls' else 'end_turn','stop_sequence':None,
        'usage':{'input_tokens':usage.get('prompt_tokens',0),'output_tokens':usage.get('completion_tokens',0)}}


class NativeStream(StreamingResponse):
    def __init__(self, iterator, cleanup, headers=None):
        self.cleanup = cleanup
        super().__init__(iterator, media_type='text/event-stream', headers={
            'Cache-Control':'no-cache','X-Accel-Buffering':'no', **(headers or {})})

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()
                await self.cleanup()


async def wait_connected(request, pending):
    """One consumer can disconnect without cancelling a shared generation."""
    work = asyncio.ensure_future(pending)
    if work.done():
        return work.result()
    stopped = asyncio.Event()
    async def disconnected():
        while not stopped.is_set() and not await request.is_disconnected():
            try:
                await asyncio.wait_for(stopped.wait(), .1)
            except asyncio.TimeoutError:
                pass
    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait({work, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if work not in done:
            raise JobError('Client disconnected', 'client_disconnected', 499)
        return work.result()
    finally:
        stopped.set()
        watcher.cancel()
        work.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(watcher, work, return_exceptions=True)


@mcp.custom_route('/v1/messages', methods=['POST'])
async def messages(request):
    if not authenticate(request):
        return JSONResponse({'type':'error','error':{'type':'authentication_error','message':'Invalid API key'}},status_code=401)
    handle = None
    released = False
    async def cleanup():
        nonlocal released
        if handle is not None and not released:
            released = True
            with anyio.CancelScope(shield=True):
                await bridge.release_request(handle)
    try:
        body = await read_json(request)
        normalized, context = normalize_request(body)
        bridge = get_bridge(await get_job_runtime())
        if bridge is None or bridge.config.transport != 'json':
            raise JobError('Anthropic endpoint requires the JSON model bridge', 'bridge_disabled', 503)
        handle = await bridge.acquire(normalized, identity=request_identity(request, body), native_context=context)
        # Bound admission before sending HTTP 200, so capacity/queue failures
        # retain their HTTP 429 + Retry-After semantics through the gateway.
        await wait_connected(request, bridge.wait_admission(handle))
        if not body.get('stream', False):
            try:
                payload = await wait_connected(request, asyncio.shield(handle['task']))
                return JSONResponse(native_payload(payload),headers=capability_headers(body))
            finally:
                await cleanup()
        async def generate():
            def event(kind, data):
                return 'event: '+kind+'\ndata: '+json.dumps(data,ensure_ascii=False)+'\n\n'
            try:
                # Native ping is useful before any content, and does not commit
                # a successful assistant message if generation subsequently fails.
                yield event('ping', {'type':'ping'})
                progress = handle.get('progress')
                if progress:
                    start = {'id':'msg_'+handle['response_id'].removeprefix('chatcmpl-'),
                        'type':'message','role':'assistant','model':body['model'],'content':[],
                        'stop_reason':None,'stop_sequence':None,
                        'usage':{'input_tokens':estimate_tokens(canonical(context)),'output_tokens':0}}
                    yield event('message_start',{'type':'message_start','message':start})
                    yield event('content_block_start',{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})
                    emitted = 0
                    while True:
                        # Clear before reading/yielding: a stage can arrive
                        # while the consumer is sending the previous delta.
                        progress.changed.clear()
                        text = progress.text
                        if len(text)>emitted:
                            yield event('content_block_delta',{'type':'content_block_delta','index':0,
                                'delta':{'type':'text_delta','text':text[emitted:]}})
                            emitted = len(text)
                        if handle['task'].done():
                            break
                        waiter = asyncio.create_task(progress.changed.wait())
                        try:
                            done,_ = await asyncio.wait({handle['task'],waiter},timeout=5,
                                                        return_when=asyncio.FIRST_COMPLETED)
                            if not done:
                                yield event('ping',{'type':'ping'})
                        finally:
                            waiter.cancel()
                            with anyio.CancelScope(shield=True):
                                await asyncio.gather(waiter,return_exceptions=True)
                else:
                    while not handle['task'].done():
                        done, _ = await asyncio.wait({handle['task']},timeout=5)
                        if not done:
                            yield event('ping', {'type':'ping'})
                payload = native_payload(handle['task'].result())
                if progress:
                    yield event('content_block_stop',{'type':'content_block_stop','index':0})
                else:
                    start = {**payload,'content':[],'stop_reason':None,'usage':{**payload['usage'],'output_tokens':0}}
                    yield event('message_start', {'type':'message_start','message':start})
                for index, block in enumerate(payload['content']):
                    if progress and index==0:
                        continue
                    is_tool = block['type']=='tool_use'
                    initial = {**block,'input':{}} if is_tool else {'type':'text','text':''}
                    yield event('content_block_start',{'type':'content_block_start','index':index,'content_block':initial})
                    value = canonical(block['input']) if is_tool else block['text']
                    for offset in range(0,len(value),1024):
                        delta = {'type':'input_json_delta','partial_json':value[offset:offset+1024]} if is_tool else {'type':'text_delta','text':value[offset:offset+1024]}
                        yield event('content_block_delta',{'type':'content_block_delta','index':index,'delta':delta})
                    yield event('content_block_stop',{'type':'content_block_stop','index':index})
                yield event('message_delta',{'type':'message_delta','delta':{'stop_reason':payload['stop_reason'],'stop_sequence':None},'usage':{'output_tokens':payload['usage']['output_tokens']}})
                yield event('message_stop',{'type':'message_stop'})
            except Exception as exc:
                error, _ = error_data(exc)
                yield event('error',error)
            finally:
                with anyio.CancelScope(shield=True):
                    await cleanup()
        return NativeStream(generate(),cleanup,capability_headers(body))
    except BaseException as exc:
        await cleanup()
        if not isinstance(exc, Exception):
            raise
        data, status = error_data(exc)
        return JSONResponse(data,status_code=status,
                            headers={'Retry-After': str(data['error']['retry_after']),
                                     'X-Should-Retry':'true'} if status == 429 else None)


@mcp.custom_route('/v1/messages/count_tokens', methods=['POST'])
async def count_tokens(request):
    if not authenticate(request):
        return JSONResponse({'type':'error','error':{'type':'authentication_error','message':'Invalid API key'}},status_code=401)
    try:
        body = await read_json(request)
        _, context = normalize_request(body)
        return JSONResponse({'input_tokens':estimate_tokens(canonical(context))}, headers=capability_headers(body))
    except Exception as exc:
        data, status = error_data(exc)
        return JSONResponse(data,status_code=status)
