"""Run the installed Claude Code against the user's existing gateway, with Read only."""
import argparse
import json
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--claude', default=shutil.which('claude'))
    parser.add_argument('--settings', type=Path, default=Path.home()/'.claude/settings.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.claude:
        parser.error('Claude Code is not on PATH; supply --claude /absolute/path/to/claude')
    config = json.loads(args.settings.read_text())
    env = os.environ.copy()
    env.update({k: str(v) for k, v in config.get('env', {}).items() if k.startswith('ANTHROPIC_')})
    env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] = '1'
    env.pop('CLAUDECODE', None)
    with tempfile.TemporaryDirectory(prefix='claude-bridge-e2e-') as directory:
        fixture = Path(directory)/'nonce.txt'
        nonce = 'CLAUDE-READ-'+secrets.token_hex(24)
        fixture.write_text(nonce)
        cmd = [args.claude, '--bare', '--setting-sources', '', '--tools', 'Read',
               '--allowedTools', 'Read', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
               '--no-session-persistence', '--max-turns', '3', '--output-format', 'stream-json',
               '--verbose', '--model', env.get('ANTHROPIC_MODEL', 'claude-opus-5-5'),
               '-p', f'Read {fixture} using the Read tool, then return its exact content. Do not guess.']
        result = subprocess.run(cmd, cwd=directory, env=env, capture_output=True, text=True, timeout=240)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result.stdout)
        os.chmod(args.output, 0o600)
        events = []
        for line in result.stdout.splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        uses, returned = [], []
        final = ''
        for event in events:
            for block in event.get('message', {}).get('content', []):
                if isinstance(block, dict) and block.get('type') == 'tool_use':
                    uses.append(block)
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    returned.append(block)
            if event.get('type') == 'result':
                final = event.get('result', '')
        verified = (result.returncode == 0 and len(uses) == 1 and uses[0]['name'] == 'Read'
                    and uses[0]['input'].get('file_path') == str(fixture)
                    and any(r.get('tool_use_id') == uses[0]['id'] and nonce in json.dumps(r) for r in returned)
                    and nonce in final)
        print(json.dumps({'verified': verified, 'exit_code': result.returncode,
                          'tool_use_count': len(uses), 'tool_result_count': len(returned),
                          'final': final, 'evidence': str(args.output)}, ensure_ascii=False))
        if not verified:
            print(result.stderr[-2000:])
            raise SystemExit(1)


if __name__ == '__main__':
    main()
