"""Verify the real Claude Code coding loop through the configured model gateway.

Uses only a new synthetic project. No global permission settings are changed.
Checks actual advertised tools, a failed test, an Edit, and a >20s Bash command.
"""
import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--claude', default=shutil.which('claude'))
    parser.add_argument('--settings', type=Path, default=Path.home()/'.claude/settings.json')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if not args.claude:
        parser.error('Claude Code is not on PATH')
    root = args.output_dir.resolve()/('coding-'+secrets.token_hex(5))
    root.mkdir(parents=True, mode=0o700)
    (root/'pricing.py').write_text('def total(prices, discount_percent):\n    """Return the discounted total of a list of prices."""\n    return sum(prices) - discount_percent\n')
    (root/'test_pricing.py').write_text('''import unittest
from pricing import total

class PricingTests(unittest.TestCase):
    def test_percentage(self):
        self.assertAlmostEqual(total([80, 20, 100], 25), 150)
    def test_no_discount(self):
        self.assertEqual(total([12, 8], 0), 20)
    def test_empty(self):
        self.assertEqual(total([], 25), 0)
    def test_full_discount(self):
        self.assertEqual(total([200], 100), 0)

if __name__ == '__main__':
    unittest.main()
''')
    config = json.loads(args.settings.read_text())
    env = os.environ.copy()
    env.update({k: str(v) for k, v in config.get('env', {}).items() if k.startswith('ANTHROPIC_')})
    env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] = '1'
    env.pop('CLAUDECODE', None)
    prompt = (f'Work only inside the current project {root}. Fix the percentage-discount bug. '
        'This is a real tool compatibility acceptance test. Use Glob/Grep/Write if they are available; otherwise use Bash equivalents (prefer rg over grep). Use Glob to discover the Python files, Grep to find the total function, '
        'and Read to inspect source and tests. Use Bash to run the unmodified tests and observe the failure. '
        'Use Edit to fix pricing.py without changing the tests. Use Write to create NOTES.md explaining the fix. '
        f'Finally use Bash to execute `sleep 25 && {sys.executable} -m unittest -v` and wait for completion. '
        'Wait for that command to actually finish. Do not run anything outside this project. '
        f'Use {sys.executable} for all Python test commands. '
        'Report the actual test result in Chinese. Do not just describe the tool calls; execute them.')
    enabled = 'Glob,Grep,Read,Edit,Write,Bash'
    cmd = [args.claude, '--bare', '--setting-sources', '', '--tools', enabled, '--allowedTools', enabled,
           '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--no-session-persistence',
           '--max-turns', '18', '--output-format', 'stream-json', '--verbose',
           '--model', env.get('ANTHROPIC_MODEL', 'claude-opus-5-5'), '-p', prompt]
    uses, returned, durations = {}, [], {}
    final = None
    advertised = set()
    tests_before = (root/'test_pricing.py').read_bytes()
    start = time.monotonic()
    print(json.dumps({'project':str(root), 'phase':'start'}), flush=True)
    with (root/'trace.jsonl').open('w') as trace, (root/'stderr.log').open('w') as stderr:
        proc = subprocess.Popen(cmd, cwd=root, env=env, stdout=subprocess.PIPE, stderr=stderr, text=True)
        timer = threading.Timer(1200, proc.kill)
        timer.start()
        try:
            for line in proc.stdout:
                trace.write(line); trace.flush()
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('subtype') == 'init':
                    advertised = set(event.get('tools', []))
                    print(json.dumps({'phase':'available_tools','tools':sorted(advertised)}), flush=True)
                for block in event.get('message', {}).get('content', []):
                    if not isinstance(block, dict):
                        continue
                    if block.get('type') == 'tool_use':
                        uses[block['id']] = (block, time.monotonic())
                        print(json.dumps({'phase':'tool_use','name':block['name'],'elapsed_s':round(time.monotonic()-start,1)}), flush=True)
                    elif block.get('type') == 'tool_result':
                        returned.append(block)
                        if block.get('tool_use_id') in uses:
                            durations[block['tool_use_id']] = time.monotonic()-uses[block['tool_use_id']][1]
                        print(json.dumps({'phase':'tool_result','error':block.get('is_error',False),'elapsed_s':round(time.monotonic()-start,1)}), flush=True)
                if event.get('type') == 'result':
                    final = event
            code = proc.wait()
        finally:
            timer.cancel()
            if proc.poll() is None:
                proc.kill(); proc.wait()
    check = subprocess.run([sys.executable,'-m','unittest','-v'],cwd=root,capture_output=True,text=True)
    tool_names = sorted({v[0]['name'] for v in uses.values()})
    long_calls = [i for i,(call,_) in uses.items() if call['name']=='Bash' and 'sleep 25' in call['input'].get('command','')]
    paired = {r.get('tool_use_id') for r in returned}
    failed_test_seen = any(r.get('is_error') and '-m unittest' in uses.get(r.get('tool_use_id'), ({},0))[0].get('input',{}).get('command','') and 'FAIL' in str(r.get('content')) for r in returned)
    verified = (code==0 and final and not final.get('is_error') and check.returncode==0 and
        {'Read','Edit','Bash'} <= set(tool_names) and (set(enabled.split(',')) & advertised) <= set(tool_names) and set(uses) <= paired and
        (root/'test_pricing.py').read_bytes() == tests_before and failed_test_seen and any(durations.get(i,0)>=24 for i in long_calls) and (root/'NOTES.md').is_file())
    summary = {'verified':bool(verified),'exit_code':code,'advertised_tools':sorted(advertised),'tools':tool_names,'tool_use_count':len(uses),
        'tool_result_count':len(returned),'observed_failing_test':failed_test_seen,'final_tests_pass':check.returncode==0,
        'long_command_seconds':[round(durations.get(i,0),2) for i in long_calls],
        'elapsed_s':round(time.monotonic()-start,1),'project':str(root),'final':final}
    (root/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    (root/'independent-tests.log').write_text(check.stdout+check.stderr)
    print(json.dumps(summary,ensure_ascii=False),flush=True)
    if not verified:
        raise SystemExit(1)


if __name__=='__main__':
    main()
