"""Real Claude Code acceptance: project rules, Skill, hooks, ToolSearch and MCP.

Uses the user's normal model settings and an isolated synthetic coding project.
Only the fixture MCP server is enabled, and delegation is disabled for this test.
"""
import argparse
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path


def preflight_evidence(root, data_dir):
    """Check submitted model context; CLI stream-json omits hook context."""
    records = []
    database = data_dir/'webui_sessions.sqlite3'
    if not database.exists():
        return {'verified': False, 'reason': 'Bridge database unavailable', 'records': []}
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as conn:
        created = getattr(root.stat(), 'st_birthtime', root.stat().st_ctime)
        jobs = conn.execute('SELECT id,payload_json FROM chat_jobs WHERE created_at >= ?',
                            (int(created)-60,))
        for job_id, payload in jobs:
            payload = json.loads(payload)
            documents = list(strings(payload.get('user_content', [])))
            for file_id in payload.get('file_ids', {}).values():
                if not re.fullmatch(r'[A-Za-z0-9_-]+', file_id):
                    continue
                path = data_dir/'uploads'/(file_id+'.bin')
                if path.exists():
                    documents.append(path.read_text())
            for document in documents:
                if str(root) not in document:
                    continue
                # Attachment messages are JSONL; hook text itself can contain
                # multiple metadata objects. Only retain this fixture's data.
                candidates = [document]
                for line in document.splitlines():
                    try:
                        candidates.extend(strings(json.loads(line)))
                    except ValueError:
                        continue
                for candidate in candidates:
                    for part in candidate.split('PostToolUse:Read hook additional context: ')[1:]:
                        try:
                            value, _ = json.JSONDecoder().raw_decode(part)
                        except ValueError:
                            continue
                        if (isinstance(value, dict) and Path(value.get('file_path', '')).parent == root
                                and re.fullmatch(r'[0-9a-f]{64}', value.get('sha256', ''))
                                and value.get('tool_use_id') and value.get('read_range')):
                            records.append({'job_id': job_id, **value})
    paths = {Path(r['file_path']).name for r in records}
    return {'verified': {'pricing.py','test_pricing.py'} <= paths, 'records': records}


def cli_config(root):
    env=os.environ.copy()
    env.pop('CLAUDECODE',None)
    env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC']='1'
    env['ENABLE_TOOL_SEARCH']='true'
    base=[shutil.which('claude'),'--strict-mcp-config','--mcp-config',str(root/'fixture-mcp.json'),
          '--settings',str(root/'.claude/settings.local.json'),
          '--disallowedTools','Agent,Task','--allowedTools','Read,Edit,Write,Bash,Glob,Grep,ToolSearch,mcp__bridge_fixture__challenge',
          '--no-chrome','--max-turns','24','--output-format','stream-json','--verbose']
    return base,env


def run_cli(cmd, root, env, phase):
    events = []
    started = time.monotonic()
    with (root/f'{phase}.jsonl').open('w') as trace, (root/f'{phase}.stderr.log').open('w') as stderr:
        proc = subprocess.Popen(cmd,cwd=root,env=env,stdout=subprocess.PIPE,stderr=stderr,text=True)
        timer = threading.Timer(900,proc.kill)
        timer.start()
        try:
            for line in proc.stdout:
                trace.write(line); trace.flush()
                try:
                    event=json.loads(line)
                except ValueError:
                    continue
                events.append(event)
                if event.get('subtype')=='init':
                    print(json.dumps({'phase':phase,'tools':event.get('tools'),'mcp':event.get('mcp_servers')}),flush=True)
                for block in event.get('message',{}).get('content',[]):
                    if isinstance(block,dict) and block.get('type') in ('tool_use','tool_result'):
                        print(json.dumps({'phase':phase,'event':block['type'],'tool':block.get('name'),
                            'error':block.get('is_error',False),'elapsed_s':round(time.monotonic()-started,1)}),flush=True)
                if event.get('type')=='result':
                    print(json.dumps({'phase':phase,'result':event.get('result'), 'error':event.get('is_error')}),flush=True)
            code=proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill(); proc.wait()
    return code,events,round(time.monotonic()-started,1)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--file-preflight',action='store_true')
    parser.add_argument('--bridge-data-dir',type=Path,default=Path(__file__).resolve().parents[1]/'data')
    parser.add_argument('--resume-fixture',type=Path,help='Recheck an existing successful coding run and finish compaction')
    args=parser.parse_args()
    if args.resume_fixture:
        root=args.resume_fixture.resolve()
        summary=json.loads((root/'summary.json').read_text())
        if not (root/'summary-initial.json').exists():
            shutil.copy2(root/'summary.json',root/'summary-initial.json')
        if args.file_preflight:
            evidence=preflight_evidence(root,args.bridge_data_dir)
            (root/'preflight-evidence.json').write_text(json.dumps(evidence,indent=2))
            summary['checks']['file_preflight_metadata']=evidence['verified']
            summary['verification_note']='Hook metadata verified from submitted bridge context; initial CLI trace assertion was invalid.'
        summary['verified']=all(summary['checks'].values())
        (root/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
        if not summary['verified']:
            raise SystemExit('Coding checks failed; refusing to skip to compaction')
        project_marker=re.search(r'PROJECT_[a-f0-9]+',(root/'CLAUDE.md').read_text()).group()
        skill_marker=re.search(r'SKILL_[a-f0-9]+',(root/'.claude/skills/bridge-check/SKILL.md').read_text()).group()
        invocations=[json.loads(s) for s in (root/'.mcp-invocations.jsonl').read_text().splitlines()]
        base,env=cli_config(root)
        compact_and_resume(root,base,env,summary,invocations,project_marker,skill_marker)
        return
    root=args.output_dir.resolve()/('normal-'+secrets.token_hex(5))
    root.mkdir(parents=True,mode=0o700)
    skill_dir=root/'.claude/skills/bridge-check'
    skill_dir.mkdir(parents=True)
    project_marker='PROJECT_'+secrets.token_hex(12)
    skill_marker='SKILL_'+secrets.token_hex(12)
    (root/'CLAUDE.md').write_text(f'''This is an isolated model compatibility fixture.
Work only inside this project. Never invoke Agent or Task or delegate work.
Use {sys.executable} for Python. Do not modify test_pricing.py.
Every final answer must include the exact project marker: {project_marker}
''')
    (root/'pricing.py').write_text('def total(prices, discount_percent):\n    return sum(prices) - discount_percent\n')
    tests='''import unittest
from pricing import total
class PricingTests(unittest.TestCase):
    def test_percentage(self): self.assertEqual(total([80,20,100],25),150)
    def test_empty(self): self.assertEqual(total([],25),0)
    def test_zero(self): self.assertEqual(total([10,20],0),30)
    def test_full(self): self.assertEqual(total([200],100),0)
'''
    (root/'test_pricing.py').write_text(tests)
    (skill_dir/'SKILL.md').write_text(f'''---
name: bridge-check
description: Verify this isolated coding fixture through real client tools.
disable-model-invocation: true
---
Run this workflow now. Use the bridge_fixture MCP challenge tool to obtain a nonce,
using ToolSearch first if deferred. The nonce must come from the actual MCP result.
Read pricing.py and test_pricing.py. Run the tests and observe the initial failure.
Fix the percentage bug with Edit, then run tests again. Never change tests.
Use Write or Bash to create NOTES.md explaining the fix.
Your final answer must report the test result, the exact returned MCP nonce,
the project marker required by CLAUDE.md, and this exact skill marker: {skill_marker}.
Stay inside this project. Do not read MCP server source or its private invocation log.
Do not invoke Agent or Task or delegate work.
''')
    (root/'fixture_mcp.py').write_text('''import json,secrets
from pathlib import Path
from mcp.server.fastmcp import FastMCP
server=FastMCP("bridge_fixture")
@server.tool()
def challenge() -> str:
    """Return a fresh acceptance nonce. Invoke this tool to obtain it."""
    nonce="MCP_"+secrets.token_hex(16)
    with Path(__file__).with_name(".mcp-invocations.jsonl").open("a") as f:
        f.write(json.dumps({"nonce":nonce})+"\\n")
    return nonce
server.run(transport="stdio")
''')
    (root/'fixture_hook.py').write_text('''import json,sys
from pathlib import Path
event=json.load(sys.stdin)
with Path(__file__).with_name("hook-events.jsonl").open("a") as f:
    f.write(json.dumps({"event":event.get("hook_event_name"),"tool":event.get("tool_name")})+"\\n")
''')
    hook_command=shlex.join([sys.executable,str(root/'fixture_hook.py')])
    settings={'hooks':{'PreToolUse':[{'matcher':'','hooks':[{'type':'command','command':hook_command}]}]}}
    if args.file_preflight:
        from claude_file_preflight import hooks_config
        for phase, entries in hooks_config()['hooks'].items():
            settings['hooks'].setdefault(phase, []).extend(entries)
    settings_path=root/'.claude/settings.local.json'
    settings_path.write_text(json.dumps(settings,indent=2))
    mcp_path=root/'fixture-mcp.json'
    mcp_path.write_text(json.dumps({'mcpServers':{'bridge_fixture':{'command':sys.executable,'args':[str(root/'fixture_mcp.py')]}}}))
    base,env=cli_config(root)
    print(json.dumps({'project':str(root),'phase':'start'}),flush=True)
    code,events,elapsed=run_cli(base+['-p','/bridge-check'],root,env,'coding')
    uses={}; results=[]
    for event in events:
        for block in event.get('message',{}).get('content',[]):
            if isinstance(block,dict):
                if block.get('type')=='tool_use': uses[block['id']]=block
                if block.get('type')=='tool_result': results.append(block)
    final=next((e for e in reversed(events) if e.get('type')=='result'),{})
    final_text=final.get('result','')
    invocations=[json.loads(s) for s in (root/'.mcp-invocations.jsonl').read_text().splitlines()] if (root/'.mcp-invocations.jsonl').exists() else []
    hooks=[json.loads(s) for s in (root/'hook-events.jsonl').read_text().splitlines()] if (root/'hook-events.jsonl').exists() else []
    check=subprocess.run([sys.executable,'-m','unittest','-v'],cwd=root,capture_output=True,text=True)
    (root/'independent-tests.log').write_text(check.stdout+check.stderr)
    tool_names={b['name'] for b in uses.values()}
    paired={b.get('tool_use_id') for b in results}
    failed=any(r.get('is_error') and 'FAIL' in str(r.get('content')) and 'unittest' in uses.get(r.get('tool_use_id'),{}).get('input',{}).get('command','') for r in results)
    checks={'successful_exit':code==0 and bool(final) and not final.get('is_error'),
        'project_rules_loaded':project_marker in final_text,'skill_loaded':skill_marker in final_text,
        'mcp_invoked':'mcp__bridge_fixture__challenge' in tool_names and bool(invocations),
        'mcp_nonce_in_final':any(i['nonce'] in final_text for i in invocations),
        'tool_search_invoked':'ToolSearch' in tool_names,
        'native_edit':'Edit' in tool_names,'read':'Read' in tool_names,'initial_failure_seen':failed,
        'tests_pass':check.returncode==0,'tests_unchanged':(root/'test_pricing.py').read_text()==tests,
        'notes_created':(root/'NOTES.md').exists(),'hook_ran':bool(hooks),
        'all_tools_returned':set(uses)<=paired,'no_delegation':not tool_names & {'Agent','Task'}}
    if args.file_preflight:
        evidence=preflight_evidence(root,args.bridge_data_dir)
        (root/'preflight-evidence.json').write_text(json.dumps(evidence,indent=2))
        checks['file_preflight_metadata']=evidence['verified']
    summary={'verified':all(checks.values()),'checks':checks,'tools':sorted(tool_names),'tool_use_count':len(uses),
        'tool_result_count':len(results),'elapsed_s':elapsed,'session_id':final.get('session_id'),
        'project':str(root),'final':final}
    (root/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps(summary,ensure_ascii=False),flush=True)
    if not summary['verified']: raise SystemExit(1)
    compact_and_resume(root,base,env,summary,invocations,project_marker,skill_marker)


def compact_and_resume(root,base,env,summary,invocations,project_marker,skill_marker):
    # A real harness /compact, followed by a resumed turn, tests preservation of
    # a nonce that is absent from the new user prompt and from project rules.
    session=summary['session_id']
    compact_code,compact_events,compact_elapsed=run_cli(base+['--resume',session,'-p',
        '/compact Preserve the MCP nonce, project and skill markers, and the actual test result.'],root,env,'compact')
    resume_code,resume_events,resume_elapsed=run_cli(base+['--resume',session,'-p',
        'Without calling any tools, repeat the exact MCP nonce and skill marker from our earlier work and the test result.'],root,env,'resume')
    resume_final=next((e for e in reversed(resume_events) if e.get('type')=='result'),{})
    resume_text=resume_final.get('result','')
    compact_checks={'compact_exit':compact_code==0,'compact_boundary':any(e.get('subtype')=='compact_boundary' for e in compact_events),
        'resume_exit':resume_code==0 and bool(resume_final) and not resume_final.get('is_error'),
        'nonce_preserved':any(i['nonce'] in resume_text for i in invocations),'skill_marker_preserved':skill_marker in resume_text,
        'project_rules_preserved':project_marker in resume_text,
        'no_tools_after_resume':not any(isinstance(b,dict) and b.get('type')=='tool_use'
            for e in resume_events for b in e.get('message',{}).get('content',[]))}
    summary['compaction']={'verified':all(compact_checks.values()),'checks':compact_checks,'elapsed_s':compact_elapsed+resume_elapsed,'final':resume_final}
    summary['verified']=summary['verified'] and summary['compaction']['verified']
    (root/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps({'compaction':summary['compaction'],'verified':summary['verified']},ensure_ascii=False),flush=True)
    if not summary['verified']: raise SystemExit(1)


if __name__=='__main__': main()
