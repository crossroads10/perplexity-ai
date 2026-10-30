"""Exercise real gateway admission using two synthetic, tool-free requests."""
import argparse
import asyncio
import json
from pathlib import Path
import secrets
import subprocess
import time

import httpx


async def run(output, direct):
    settings = json.loads((Path.home()/'.claude/settings.json').read_text())['env']
    headers = {'x-api-key': settings.get('ANTHROPIC_AUTH_TOKEN') or settings['ANTHROPIC_API_KEY'],
               'anthropic-version': '2023-06-01'}
    url = settings['ANTHROPIC_BASE_URL'].rstrip('/')+'/v1/messages'
    if direct:
        info = json.loads(subprocess.check_output(['docker','inspect','perplexity-mcp'],text=True))[0]
        env = dict(value.split('=',1) for value in info['Config']['Env'] if '=' in value)
        headers['x-api-key'] = env['MCP_TOKEN']
        url = 'http://127.0.0.1:8000/v1/messages'
    started, admitted = time.monotonic(), asyncio.Event()
    report = {'route': 'adapter' if direct else 'gateway'}
    async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
        def body(prompt):
            return {'model': 'claude-opus-5-5', 'max_tokens': 2048, 'stream': True,
                    'metadata': {'user_id': 'queue_probe_'+secrets.token_hex(12)},
                    'messages': [{'role': 'user', 'content': prompt}]}
        async def first():
            frames = []
            try:
                async with client.stream('POST', url, headers=headers, json=body(
                    'Write exactly 80 numbered lines. Each line must contain its number followed by: synthetic queue measurement. No tools.')) as response:
                    report['first_http_status'] = response.status_code
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith('data: '):
                            continue
                        event = json.loads(line[6:])
                        frames.append(event)
                        if event.get('type') == 'message_start':
                            report['first_admitted_s'] = round(time.monotonic()-started, 3)
                            admitted.set()
                report['first_finished'] = bool(frames and frames[-1]['type'] == 'message_stop')
                report['first_elapsed_s'] = round(time.monotonic()-started, 3)
            finally:
                admitted.set()
        task = asyncio.create_task(first())
        try:
            await asyncio.wait_for(admitted.wait(), 150)
            if task.done():
                await task
                raise RuntimeError('First request ended before the concurrency probe')
            second_started = time.monotonic()
            response = await client.post(url, headers=headers, json=body('Reply exactly QUEUE_PROBE_OK. No tools.'))
            report['second_elapsed_s'] = round(time.monotonic()-second_started, 3)
            report['second_http_status'] = response.status_code
            report['retry_after'] = response.headers.get('retry-after')
            report['should_retry'] = response.headers.get('x-should-retry')
            if response.headers.get('content-type', '').startswith('application/json'):
                report['second_body'] = response.json()
            else:
                frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
                report['second_finished'] = bool(frames and frames[-1]['type'] == 'message_stop')
            await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    second_ok = (report.get('second_http_status') == 429 and (report.get('retry_after') or '').isdigit()
                 and report.get('should_retry') == 'true'
                 if direct else report.get('second_http_status') == 200 and report.get('second_finished'))
    report['verified'] = bool(report.get('first_finished') and second_ok)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if not report['verified']:
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--direct-adapter', action='store_true')
    args = parser.parse_args()
    asyncio.run(run(args.output, args.direct_adapter))
