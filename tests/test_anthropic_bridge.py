import asyncio
import copy
import json
import re

import pytest

from perplexity.server import anthropic, tool_bridge
from perplexity.server.tool_bridge import BridgeConfig, ToolBridge
from tests.conftest import request_for

BODY = {'model':'perplexity-search','max_tokens':4096,'messages':[{'role':'user','content':'Use the fixture tool.'}],
        'tools':[{'name':'Read','input_schema':{'type':'object','properties':{'file_path':{'type':'string'}},'required':['file_path'],'additionalProperties':False}}]}
CALL = {'type':'tool_calls','calls':[{'name':'Read','arguments':{'file_path':'/fixture'}}]}


@pytest.fixture
async def bridge(api_runtime, monkeypatch, tmp_path):
    bridge = ToolBridge(api_runtime, BridgeConfig(account_id='one-account',allowed_tools=['*'],receipt_path=str(tmp_path/'receipts.db')))
    monkeypatch.setattr(tool_bridge,'_bridge',bridge)
    yield bridge
    await bridge.close()


def set_responses(api_runtime, answers):
    calls = []
    async def upstream(client, **params):
        index = len(calls)
        calls.append(params)
        answer = answers[min(index,len(answers)-1)]
        yield {'answer': answer if isinstance(answer,str) else json.dumps(answer), 'status':'COMPLETED',
               '_follow_up':{'backend_uuid':'backend-'+str(index),'attachments':[]}}
    api_runtime.transport = upstream
    return calls


async def request(body, **kwargs):
    return await asyncio.wait_for(anthropic.messages(request_for('/v1/messages',body,**kwargs)), 3)


async def events(response):
    return [json.loads(part.split('data: ',1)[1]) async for part in response.body_iterator]


@pytest.mark.asyncio
async def test_native_tool_lifecycle_keeps_system_error_references_and_controls(bridge, api_runtime):
    seen = set_responses(api_runtime,[CALL,{'type':'final','text':'The read was denied.'}])
    body = copy.deepcopy(BODY)
    body['system']=[{'type':'text','text':'Project instructions','cache_control':{'type':'ephemeral'}}]
    body['tool_choice']={'type':'auto','disable_parallel_tool_use':True}
    first = await request(body)
    payload = json.loads(first.body)
    assert payload['stop_reason']=='tool_use'
    call = payload['content'][0]
    body['messages'].extend([{'role':'assistant','content':[call]}, {'role':'user','content':[
        {'type':'tool_result','tool_use_id':call['id'],'is_error':True,'content':[
            {'type':'text','text':'Permission denied'},{'type':'tool_reference','tool_name':'mcp__fixture__read'}]},
        {'type':'text','text':'Do not retry that read.'}]}, {'role':'system','content':'Keep the denial in mind.'}])
    second = await request(body)
    assert second.status_code==200
    assert json.loads(second.body)['content'][0]['text']=='The read was denied.'
    context=json.loads(seen[1]['query'].split('Client conversation (ordered, complete):\n')[1])
    assert context['system']==body['system']
    assert context['messages']==body['messages']
    assert 'parallel_tool_calls=false' in seen[1]['query']


@pytest.mark.asyncio
async def test_malformed_proposal_repaired_without_dispatching_partial_calls(bridge,api_runtime):
    bad='{"type":"tool_calls","calls":[{"name":"Read","arguments":{"file_path":"/WRONG"}},'
    seen=set_responses(api_runtime,[bad,CALL])
    response=await request({**BODY,'stream':True})
    frames=await events(response)
    tools=[e['content_block'] for e in frames if e['type']=='content_block_start']
    assert len(seen)==2 and len(tools)==1
    assert tools[0]['name']=='Read'
    arguments=''.join(e['delta']['partial_json'] for e in frames if e['type']=='content_block_delta')
    assert json.loads(arguments)=={'file_path':'/fixture'}
    assert frames[-1]['type']=='message_stop'
    assert not bridge.runs


@pytest.mark.asyncio
async def test_exhausted_repair_is_native_error_never_empty_success(bridge,api_runtime):
    seen=set_responses(api_runtime,['not JSON'])
    response=await request({**BODY,'stream':True})
    frames=await events(response)
    assert len(seen)==bridge.config.format_retries+1
    assert [f['type'] for f in frames]==['ping','error']
    assert 'after correction' in frames[-1]['error']['message']


@pytest.mark.asyncio
async def test_native_heartbeat_before_model_finishes(bridge,api_runtime):
    release=asyncio.Event()
    async def upstream(client,**params):
        await release.wait()
        yield {'answer':'{"type":"final","text":"done"}','status':'COMPLETED',
               '_follow_up':{'backend_uuid':'heartbeat','attachments':[]}}
    api_runtime.transport=upstream
    response=await request({**BODY,'stream':True})
    first=await asyncio.wait_for(anext(response.body_iterator),.1)
    assert 'event: ping' in first
    release.set()
    assert (await events(response))[-1]['type']=='message_stop'


@pytest.mark.asyncio
async def test_receipt_survives_restart_with_identical_tool_ids(bridge,api_runtime,monkeypatch):
    seen=set_responses(api_runtime,[CALL])
    body={**BODY,'metadata':{'user_id':'user_fixture_session_one'}}
    first=json.loads((await request(body)).body)
    await bridge.close()
    fresh=ToolBridge(api_runtime,bridge.config)
    monkeypatch.setattr(tool_bridge,'_bridge',fresh)
    try:
        second=json.loads((await request(body)).body)
        assert first==second and len(seen)==1
        third=json.loads((await request({**body,'metadata':{'user_id':'user_fixture_session_two'}})).body)
        assert third['content'][0]['id']!=first['content'][0]['id'] and len(seen)==2
    finally:
        await fresh.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('visible',[False,True])
async def test_parallel_retry_consumers_and_disconnect_are_independent(bridge,api_runtime,visible):
    bridge.config.visible_progress=visible
    seen=set_responses(api_runtime,[CALL])
    body={**BODY,'stream':True,'metadata':{'user_id':'session_parallel'}}
    a,b=await asyncio.gather(request(body),request(body))
    # Close one stream after it starts. The other must retain its own reference.
    await anext(a.body_iterator)
    await a.body_iterator.aclose()
    frames=await events(b)
    assert frames[-1]['type']=='message_stop' and len(seen)==1
    assert not bridge.requests and not bridge.runs


@pytest.mark.asyncio
async def test_explicit_key_conflict_and_unscoped_requests_not_cached(bridge,api_runtime):
    seen=set_responses(api_runtime,[CALL])
    headers=[(b'idempotency-key',b'fixture-turn')]
    assert (await request(BODY,headers=headers)).status_code==200
    different={**BODY,'messages':[{'role':'user','content':'Different input'}]}
    assert (await request(different,headers=headers)).status_code==409
    await request(BODY)
    await request(BODY)
    assert len(seen)==3


@pytest.mark.asyncio
async def test_no_parallel_override_or_server_tool_silent_drop(bridge,api_runtime):
    set_responses(api_runtime,[{'type':'tool_calls','calls':CALL['calls']*2}])
    body={**BODY,'tool_choice':{'type':'auto','disable_parallel_tool_use':True}}
    assert (await request(body)).status_code==502
    response=await request({**BODY,'tools':[{'type':'web_search_20250305','name':'web_search'}]})
    assert response.status_code==400 and b'web_search_20250305' in response.body


@pytest.mark.asyncio
async def test_output_schema_is_validated_and_corrected(bridge,api_runtime):
    seen=set_responses(api_runtime,[{'type':'final','text':'{"ok":"wrong type"}'},{'type':'final','text':'{"ok":true}'}])
    body={**BODY,'output_config':{'format':{'type':'json_schema','schema':{'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok']}}}}
    response=await request(body)
    assert json.loads(json.loads(response.body)['content'][0]['text'])=={'ok':True}
    assert len(seen)==2


def test_attachments_redacted_and_pdf_not_dropped():
    body={**BODY,'messages':[{'role':'user','content':[{'type':'text','text':'Read this'},
        {'type':'document','source':{'type':'base64','media_type':'application/pdf','data':'cGRm'}},
        {'type':'image','source':{'type':'base64','media_type':'image/png','data':'aW1hZ2U='}}]}]}
    normalized,context=anthropic.normalize_request(body)
    assert len([p for p in normalized['messages'][0]['content'] if p['type']=='input_file'])==2
    assert 'base64' not in json.dumps(context) and 'aW1hZ2U=' not in json.dumps(context)


@pytest.mark.asyncio
async def test_count_tokens_is_authenticated_and_explicitly_estimated(bridge):
    response=await anthropic.count_tokens(request_for('/v1/messages/count_tokens',BODY))
    assert response.status_code==200 and response.headers['x-usage-estimated']=='true'
    assert json.loads(response.body)['input_tokens']>0
    assert (await anthropic.count_tokens(request_for('/v1/messages/count_tokens',BODY,token=False))).status_code==401


@pytest.mark.asyncio
@pytest.mark.parametrize('visible',[False,True])
async def test_native_stream_cancel_before_iteration_releases_request(bridge,api_runtime,visible):
    bridge.config.visible_progress=visible
    response=await request({**BODY,'stream':True,'metadata':{'user_id':'cancel-before-start'}})
    async def receive():
        return {'type':'http.disconnect'}
    async def send(message):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await response({'type':'http','asgi':{'spec_version':'2.4'}},receive,send)
    assert not bridge.requests and not bridge.runs


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [
    {'tools':{}}, {'metadata':'bad'}, {'output_config':[]},
    {'tools':[{'name':'bad','description':None}]},
    {'output_config':{'format':{'type':'json_schema','schema':{'type':'unknown'}}}},
])
async def test_malformed_native_request_is_client_error(bridge,invalid):
    response = await request({**BODY,**invalid})
    assert response.status_code == 400
    assert not bridge.requests and not bridge.runs


def test_durable_receipts_first_writer_wins_between_connections(tmp_path):
    from perplexity.server.bridge_receipts import ReceiptStore
    from perplexity.server.job_store import JobError
    path = tmp_path/'receipts.db'
    first, second = ReceiptStore(path), ReceiptStore(path)
    try:
        assert first.put('key','same',{'id':'original'}) == {'id':'original'}
        assert second.put('key','same',{'id':'different'}) == {'id':'original'}
        with pytest.raises(JobError):
            second.put('key','different',{'id':'wrong'})
        assert first.get('key','same') == {'id':'original'}
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_long_context_uploaded_complete_and_end_ack_required(bridge,api_runtime):
    seen = []
    async def upstream(client,**params):
        seen.append(params)
        files = params['files']
        assert len(files) == 1
        document = next(iter(files.values())).decode()
        assert 'BEGIN_MARKER' in document and 'LATEST_MARKER' in document
        assert 'Client conversation (ordered, complete):' in document
        ack = document.rsplit('exact value: ',1)[1].strip()
        # A valid tool proposal without receipt of the document end must never
        # reach the client. The correction returns a different, final response.
        checks = dict(re.findall(r'CONTEXT_CHECKPOINT (c\d+)=(\w+)', document))
        run = next(iter(bridge.runs.values()))
        answer = CALL if len(seen)==1 else {'type':'final','text':'complete','context_ack':ack,
                                           'context_checks':checks, 'request_text_checks':run.request_text_checks}
        yield {'answer':json.dumps(answer),'status':'COMPLETED',
               '_follow_up':{'backend_uuid':'long-'+str(len(seen)),'attachments':[]}}
    api_runtime.transport=upstream
    body={**BODY,'messages':[{'role':'user','content':'BEGIN_MARKER '+('context '*8000)+' LATEST_MARKER'}]}
    response=await request(body)
    result=json.loads(response.body)
    assert response.status_code==200 and result['stop_reason']=='end_turn'
    assert result['content']==[{'type':'text','text':'complete'}]
    assert len(seen)==2 and all(len(p['query'])<30000 for p in seen)
    assert result['usage']['input_tokens']>16000


@pytest.mark.asyncio
async def test_missing_attachment_ack_fails_without_client_tool_dispatch(bridge,api_runtime):
    set_responses(api_runtime,[CALL])
    body={**BODY,'messages':[{'role':'user','content':'context '*8000}], 'stream':True}
    response=await request(body)
    frames=await events(response)
    assert [e['type'] for e in frames]==['ping','error']
    assert 'context_ack' in frames[-1]['error']['message']


@pytest.mark.asyncio
async def test_ask_numeric_citation_suffix_unwrapped_after_complete_json(bridge,api_runtime):
    seen=set_responses(api_runtime,[json.dumps(CALL)+'[10][20][80][100]'])
    response=await request(BODY)
    assert response.status_code==200 and len(seen)==1
    assert json.loads(response.body)['content'][0]['name']=='Read'


@pytest.mark.asyncio
@pytest.mark.parametrize('suffix',[' Extra instructions','{}','[10] do this','[not a citation]'])
async def test_other_trailing_material_not_salvaged(bridge,api_runtime,suffix):
    seen=set_responses(api_runtime,[json.dumps(CALL)+suffix])
    response=await request({**BODY,'stream':True})
    frames=await events(response)
    assert len(seen)==bridge.config.format_retries+1 and [e['type'] for e in frames]==['ping','error']


@pytest.mark.asyncio
async def test_visible_progress_precedes_model_and_is_part_of_receipt(bridge,api_runtime):
    bridge.config.visible_progress=True
    release=asyncio.Event()
    calls=[]
    async def upstream(client,**params):
        calls.append(params)
        await release.wait()
        yield {'answer':json.dumps(CALL),'status':'COMPLETED',
               '_follow_up':{'backend_uuid':'visible','attachments':[]}}
    api_runtime.transport=upstream
    body={**BODY,'stream':True,'metadata':{'user_id':'visible-fixture'}}
    response=await request(body)
    first=[]
    for _ in range(4):
        first.append(json.loads((await asyncio.wait_for(anext(response.body_iterator),.2)).split('data: ',1)[1]))
    assert [e['type'] for e in first]==['ping','message_start','content_block_start','content_block_delta']
    assert '[桥接状态]' in first[-1]['delta']['text'] and '准备' in first[-1]['delta']['text']
    assert not release.is_set()
    release.set()
    frames=first+await events(response)
    starts=[e['content_block'] for e in frames if e['type']=='content_block_start']
    assert [b['type'] for b in starts]==['text','tool_use']
    status=''.join(e['delta']['text'] for e in frames if e['type']=='content_block_delta' and e['index']==0)
    assert status.count('准备本轮')==1 and '正在校验' in status
    replay=await request({**body,'stream':False})
    saved=json.loads(replay.body)
    assert saved['id']==first[1]['message']['id']
    assert saved['content'][0]['text']==status
    assert saved['content'][1]['id']==starts[1]['id']
    assert len(calls)==1


@pytest.mark.asyncio
async def test_visible_progress_failure_is_not_a_successful_tool_reply(bridge,api_runtime):
    bridge.config.visible_progress=True
    set_responses(api_runtime,['bad JSON'])
    frames=await events(await request({**BODY,'stream':True}))
    assert frames[-1]['type']=='error'
    assert not any(e['type'] in ('message_delta','message_stop') for e in frames)
    assert not any(e.get('content_block',{}).get('type')=='tool_use' for e in frames)
    status=''.join(e['delta']['text'] for e in frames if e['type']=='content_block_delta')
    assert '请求纠正' in status


@pytest.mark.asyncio
async def test_visible_progress_never_pollutes_structured_output(bridge,api_runtime):
    bridge.config.visible_progress=True
    set_responses(api_runtime,[{'type':'final','text':'{"ok":true}'}])
    body={**BODY,'output_config':{'format':{'type':'json_schema','schema':{'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok']}}}}
    response=await request(body)
    assert json.loads(response.body)['content']==[{'type':'text','text':'{"ok":true}'}]


@pytest.mark.asyncio
async def test_transport_stage_order_and_diagnostics(bridge,api_runtime,monkeypatch):
    from perplexity.server import job_runtime
    bridge.config.visible_progress=True
    async def transport(client,progress_callback,**params):
        await progress_callback('uploading')
        await progress_callback('waiting_model')
        yield {'answer':json.dumps(CALL),'status':'COMPLETED',
               '_follow_up':{'backend_uuid':'timing','attachments':[]}}
    monkeypatch.setattr(job_runtime,'search_stream',transport)
    api_runtime.transport=transport
    frames=await events(await request({**BODY,'stream':True}))
    status=''.join(e['delta']['text'] for e in frames if e['type']=='content_block_delta' and e['index']==0)
    assert status.index('上传')<status.index('请求已发出')<status.index('模型正在返回')<status.index('正在校验')
    assert status.count('上传')==1 and status.count('请求已发出')==1
    jobs=await api_runtime.db(api_runtime.store.list,include_snapshot=True)
    stages=jobs[0]['snapshot']['transport_events']
    assert [s['stage'] for s in stages]==['uploading','waiting_model','generating','upstream_complete']
    assert [s['elapsed_s'] for s in stages]==sorted(s['elapsed_s'] for s in stages)


@pytest.mark.asyncio
async def test_transport_stage_wakes_stream_before_model_completion(bridge,api_runtime,monkeypatch):
    from perplexity.server import job_runtime
    bridge.config.visible_progress=True
    started, upload, waiting, finish = (asyncio.Event() for _ in range(4))
    async def transport(client,progress_callback,**params):
        started.set()
        await upload.wait()
        await progress_callback('uploading')
        await waiting.wait()
        await progress_callback('waiting_model')
        await finish.wait()
        yield {'answer':json.dumps(CALL),'status':'COMPLETED',
               '_follow_up':{'backend_uuid':'live-progress','attachments':[]}}
    monkeypatch.setattr(job_runtime,'search_stream',transport)
    api_runtime.transport=transport
    response=await request({**BODY,'stream':True})
    iterator=response.body_iterator
    async def until_text(expected):
        while True:
            frame=json.loads((await anext(iterator)).split('data: ',1)[1])
            if expected in frame.get('delta',{}).get('text',''):
                return
    try:
        await asyncio.wait_for(until_text('准备本轮'),.5)
        await asyncio.wait_for(started.wait(),.5)
        pending=asyncio.create_task(until_text('正在上传'))
        await asyncio.sleep(0)
        upload.set()
        await asyncio.wait_for(pending,.5)
        # Also cover a status arriving while the generator is suspended at yield.
        waiting.set()
        await asyncio.sleep(.05)
        await asyncio.wait_for(until_text('等待 Perplexity'),.5)
        assert not finish.is_set()
        finish.set()
        frames=await events(response)
        assert frames[-1]['type']=='message_stop'
    finally:
        finish.set()
        await iterator.aclose()
