"""Verify native gateway SSE, response receipts and a complete long context."""
import argparse
import json
import secrets
import time
from pathlib import Path

import httpx


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--tool-results',action='store_true',help='Use 60 synthetic tool results to exercise per-result checkpoints')
    args=parser.parse_args()
    settings=json.loads((Path.home()/'.claude/settings.json').read_text())['env']
    headers={'x-api-key':settings.get('ANTHROPIC_AUTH_TOKEN') or settings['ANTHROPIC_API_KEY'],
             'anthropic-version':'2023-06-01'}
    base=settings['ANTHROPIC_BASE_URL'].rstrip('/')
    body={'model':'claude-opus-5-5','max_tokens':1024,
          'metadata':{'user_id':'native_protocol_fixture_'+secrets.token_hex(12)},
          'messages':[{'role':'user','content':'Reply exactly NATIVE_GATEWAY_OK'}]}
    report={}
    with httpx.Client(timeout=300,trust_env=False) as client:
        count=client.post(base+'/v1/messages/count_tokens',headers=headers,json=body)
        report['count_tokens_ok']=count.status_code==200 and count.json().get('input_tokens',0)>0
        frames=[]; timing=[]; start=time.monotonic()
        with client.stream('POST',base+'/v1/messages',headers=headers,json={**body,'stream':True}) as response:
            response.raise_for_status()
            report['capability_headers']={key:response.headers.get(key) for key in
                ['x-bridge-backend','x-ignored-params','x-usage-estimated','x-token-estimator']}
            for line in response.iter_lines():
                if line.startswith('data: '):
                    frames.append(json.loads(line[6:])); timing.append(round(time.monotonic()-start,3))
        message=next(f['message'] for f in frames if f.get('type')=='message_start')
        texts={}
        for frame in frames:
            if frame.get('type')=='content_block_delta' and frame['delta'].get('type')=='text_delta':
                index=frame['index']
                texts[index]=texts.get(index,'')+frame['delta']['text']
        text=list(texts.values())[-1] if texts else ''
        report['sse']={'types':[f['type'] for f in frames],'arrival_seconds':timing,
                       'text_correct':text=='NATIVE_GATEWAY_OK','ping_first':frames[0]['type']=='ping',
                       'finished':frames[-1]['type']=='message_stop'}
        repeated=client.post(base+'/v1/messages',headers=headers,json=body)
        repeated.raise_for_status()
        report['receipt_same_message_id']=repeated.json().get('id')==message['id']
        report['receipt_same_text_blocks']=[b['text'] for b in repeated.json()['content'] if b['type']=='text']==list(texts.values())
        print(json.dumps({'phase':'native_sse_and_receipt','report':report}),flush=True)
        markers={k:secrets.token_hex(12) for k in ['begin','middle','end']}
        messages=[]
        records=60 if args.tool_results else 240
        for i in range(records):
            marker={0:'begin',records//2:'middle',records-1:'end'}.get(i)
            text='Record '+str(i)+': '+('ordinary fixture context. '*22)
            if marker: text+='\nACCEPTANCE_MARKER '+marker+'='+markers[marker]
            if args.tool_results:
                messages.extend([
                    {'role':'assistant','content':[{'type':'tool_use','id':f'fixture_read_{i}',
                        'name':'Read','input':{'file_path':f'/synthetic/{i}.txt'}}]},
                    {'role':'user','content':[{'type':'tool_result','tool_use_id':f'fixture_read_{i}','content':text}]}])
            else:
                messages.append({'role':'user' if i%2==0 else 'assistant','content':text})
        messages.append({'role':'user','content':'Return a JSON object with keys begin, middle, end containing the exact ACCEPTANCE_MARKER values from the history. No tools.'})
        schema={'type':'object','properties':{k:{'type':'string'} for k in markers},'required':list(markers),'additionalProperties':False}
        body={**body,'messages':messages,'output_config':{'format':{'type':'json_schema','schema':schema}}}
        start=time.monotonic()
        response=client.post(base+'/v1/messages',headers=headers,json=body)
        report['long_context']={'status':response.status_code,'messages':len(messages),
                                'tool_results':records if args.tool_results else 0,
                                'request_bytes':len(json.dumps(body).encode()),'elapsed_s':round(time.monotonic()-start,2)}
        if response.is_success:
            payload=response.json()
            answer=json.loads(payload['content'][0]['text'])
            report['long_context'].update(verified=answer==markers,expected=markers,answer=answer,usage_estimated=payload['usage'])
        else:
            report['long_context'].update(verified=False,error=response.json())
    report['verified']=all([report['count_tokens_ok'],report['sse']['text_correct'],report['sse']['ping_first'],
                            report['sse']['finished'],report['receipt_same_message_id'],report['receipt_same_text_blocks'],report['long_context']['verified']])
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False),flush=True)
    if not report['verified']: raise SystemExit(1)


if __name__=='__main__': main()
