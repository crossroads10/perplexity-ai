"""Measure real gateway status latency and verify Claude Code receives it."""
import argparse
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import httpx


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase',choices=['before','after'],required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--claude-probe',action='store_true')
    args=parser.parse_args()
    root=args.output_dir.resolve();root.mkdir(parents=True,exist_ok=True)
    fixture=root/'http-fixture.json'
    if not fixture.exists():
        fixture.write_text(json.dumps({'nonce':'BENCH_'+secrets.token_hex(12),'context':'Synthetic context for latency measurement. '*1200}))
    data=json.loads(fixture.read_text())
    config=json.loads((Path.home()/'.claude/settings.json').read_text())['env']
    body={'model':'claude-opus-5-5','max_tokens':512,'stream':True,
          'metadata':{'user_id':'progress_'+args.phase+'_'+secrets.token_hex(12)},
          'messages':[{'role':'user','content':data['context']+'\nReply with only '+data['nonce']}]}
    headers={'x-api-key':config.get('ANTHROPIC_AUTH_TOKEN') or config['ANTHROPIC_API_KEY'],'anthropic-version':'2023-06-01'}
    started=time.monotonic(); frames=[]; first_text=None;first_status=None;first_answer=None;texts={}
    with httpx.Client(timeout=300,trust_env=False) as client:
        with client.stream('POST',config['ANTHROPIC_BASE_URL'].rstrip('/')+'/v1/messages',headers=headers,json=body) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith('data: '): continue
                event=json.loads(line[6:]); elapsed=round(time.monotonic()-started,3)
                frames.append({'elapsed_s':elapsed,'event':event})
                if event.get('type')=='content_block_delta' and event.get('delta',{}).get('type')=='text_delta':
                    text=event['delta']['text'];texts[event['index']]=texts.get(event['index'],'')+text
                    if first_text is None:first_text=elapsed
                    if '[桥接状态]' in text:
                        if first_status is None:first_status=elapsed
                        print(json.dumps({'phase':args.phase,'elapsed_s':elapsed,'status':text},ensure_ascii=False),flush=True)
                    elif data['nonce'] in texts[event['index']] and first_answer is None:first_answer=elapsed
    duration=round(time.monotonic()-started,3)
    report={'phase':args.phase,'first_text_s':first_text,'first_status_s':first_status,'first_answer_s':first_answer,
            'total_s':duration,'verified':any(data['nonce'] in text for text in texts.values()) and frames[-1]['event']['type']=='message_stop',
            'text_blocks':texts}
    if args.phase=='after': report['verified']=report['verified'] and first_status is not None and first_status<first_answer
    db=Path(__file__).resolve().parents[1]/'data/webui_sessions.sqlite3'
    with sqlite3.connect(db) as connection:
        row=connection.execute('select id,created_at,started_at,finished_at,snapshot_json from chat_jobs where payload_json like ? order by created_at desc limit 1',('%'+data['nonce']+'%',)).fetchone()
        if row:
            report['job_id']=row[0]
            report['queue_s']=round(row[2]-row[1],3)
            report['transport_events']=json.loads(row[4]).get('transport_events',[])
    (root/(args.phase+'-http-frames.json')).write_text(json.dumps(frames,ensure_ascii=False,indent=2))
    (root/(args.phase+'-http-summary.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False),flush=True)
    if not report['verified']:raise SystemExit(1)
    if not args.claude_probe:return
    project=root/('claude-'+secrets.token_hex(5));project.mkdir(mode=0o700)
    nonce='READ_'+secrets.token_hex(12)
    (project/'fixture.txt').write_text(nonce+'\n')
    cmd=[shutil.which('claude'),'--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
         '--disallowedTools','Agent,Task','--allowedTools','Read','--max-turns','5','--no-chrome',
         '--no-session-persistence','--output-format','stream-json','--include-partial-messages','--verbose','-p',
         '请仅调用 Read 工具读取当前目录的 fixture.txt，然后原样回复文件内容。这是实际工具验收，请不要猜测内容，不要调用其他工具。']
    env=os.environ.copy();env.pop('CLAUDECODE',None);env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC']='1'
    started=time.monotonic();status_time=None;tool_time=None;result=None;tool_ids=[];returned=[]
    with (project/'trace.jsonl').open('w') as trace,(project/'stderr.log').open('w') as stderr:
        proc=subprocess.Popen(cmd,cwd=project,env=env,stdout=subprocess.PIPE,stderr=stderr,text=True)
        timer=threading.Timer(300,proc.kill);timer.start()
        try:
            for line in proc.stdout:
                trace.write(line);trace.flush()
                event=json.loads(line);elapsed=round(time.monotonic()-started,3)
                delta=event.get('event',{}).get('delta',{})
                if delta.get('type')=='text_delta' and '[桥接状态]' in delta.get('text',''):
                    if status_time is None:status_time=elapsed
                    print(json.dumps({'phase':'claude','elapsed_s':elapsed,'status':delta['text']},ensure_ascii=False),flush=True)
                for block in event.get('message',{}).get('content',[]):
                    if not isinstance(block,dict):continue
                    if block.get('type')=='tool_use':
                        tool_ids.append(block['id'])
                        if tool_time is None:tool_time=elapsed
                    if block.get('type')=='tool_result':returned.append(block['tool_use_id'])
                if event.get('type')=='result':result=event
            code=proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:proc.kill();proc.wait()
    cli={'verified':bool(code==0 and result and not result.get('is_error') and nonce in result.get('result','') and
         tool_ids and set(tool_ids)<=set(returned) and status_time is not None and tool_time is not None and status_time<tool_time),
         'first_visible_status_s':status_time,'first_tool_s':tool_time,'total_s':round(time.monotonic()-started,3),
         'tool_calls':len(tool_ids),'tool_results':len(returned),'project':str(project),'result':result.get('result') if result else None}
    (project/'summary.json').write_text(json.dumps(cli,ensure_ascii=False,indent=2))
    print(json.dumps(cli,ensure_ascii=False),flush=True)
    if not cli['verified']:raise SystemExit(1)


if __name__=='__main__':main()
