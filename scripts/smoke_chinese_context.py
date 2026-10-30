"""Replay language failures in an isolated bridge; never execute proposed tools.

Run in the candidate container with the existing data mounted read-only. Only
bounded results are written; account configuration is copied to a temporary dir.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from perplexity.server.anthropic import normalize_request
from perplexity.server.client_pool import ClientPool
from perplexity.server.files_store import FilesStore
from perplexity.server.job_runtime import JobRuntime
from perplexity.server.tool_bridge import BridgeConfig, ToolBridge, canonical
from perplexity.server.webui_sessions import WebUISessionStore


def read_context(data, job_id):
    with sqlite3.connect((data / 'webui_sessions.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT payload_json FROM chat_jobs WHERE id=?', (job_id,)).fetchone()
    payload = json.loads(row[0])
    ref = next(iter(payload['file_ids'].values()))
    document = (data / 'uploads' / (ref + '.bin')).read_text()
    metadata = next(line for line in document.splitlines() if line.startswith('CLIENT_REQUEST_METADATA '))
    body = json.loads(metadata.split(' ', 1)[1])
    body['messages'] = []
    for section in re.findall(r'BEGIN_CLIENT_MESSAGE \d+\n(.*?)\nEND_CLIENT_MESSAGE \d+', document, re.S):
        message, blocks = None, []
        for line in section.splitlines():
            if line.startswith('MESSAGE_METADATA '):
                message = json.loads(line.split(' ', 1)[1])
            elif line.startswith('CONTENT_BLOCK '):
                blocks.append(json.loads(line.split(' ', 1)[1]))
            elif line.startswith('{'):
                value = json.loads(line)
                if value.get('type') == 'tool_result':
                    blocks.append(value)
                elif 'role' in value:
                    message = value
        if message is None:
            raise ValueError('Unrecognized archived message')
        if blocks:
            message['content'] = blocks
        body['messages'].append(message)
    return body


async def main(args):
    if args.export_only:
        cases = {job_id: read_context(args.data, job_id) for job_id in args.jobs}
        with os.fdopen(os.open(args.output, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), 'w') as f:
            json.dump(cases, f, ensure_ascii=False)
        print(json.dumps({'exported_cases': len(cases)}), flush=True)
        return
    cases = json.loads(args.cases.read_text()) if args.cases else None
    config = json.loads((args.data / 'config/token_pool_config.json').read_text())
    live_bridge = json.loads((args.data / 'config/tool_bridge.json').read_text())
    config['tokens'] = [t for t in config['tokens'] if t['id'] == live_bridge['account_id']]
    config['heart_beat'] = {'enable': False}
    config['fallback'] = {'fallback_to_auto': False}
    config['incognito'] = {'enabled': True}
    reports = []
    with tempfile.TemporaryDirectory(prefix='chinese-context-') as temporary:
        root = Path(temporary)
        pool_path = root / 'pool.json'
        with os.fdopen(os.open(pool_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as f:
            json.dump(config, f)
        pool = await asyncio.to_thread(ClientPool, str(pool_path))
        runtime = JobRuntime(WebUISessionStore(root / 'sessions.db'), pool, files=FilesStore(root / 'files'))
        await runtime.start()
        bridge = ToolBridge(runtime, BridgeConfig(account_id=live_bridge['account_id'], allowed_tools=['*'], run_timeout=240))
        try:
            for job_id in args.jobs:
                native = cases[job_id] if cases else read_context(args.data, job_id)
                body, context = normalize_request(native)
                started = time.monotonic()
                run = None
                print(json.dumps({'event': 'replaying', 'job': job_id}), flush=True)
                try:
                    run, pending = await bridge.prepare(body, native_context=context)
                    payload = bridge.payload(run, await pending)
                    message = payload['choices'][0]['message']
                    text = message.get('content') or ''
                    final = await runtime.get(run.job['id'])
                    snapshot = final.get('snapshot', {})
                    reports.append({'job': job_id, 'verified': bool(re.search(r'[\u4e00-\u9fff]', text)) and not message.get('tool_calls'),
                                    'text': text, 'tools_proposed': len(message.get('tool_calls', [])),
                                    'elapsed_s': round(time.monotonic() - started, 2), 'language': run.inputs.get('language'),
                                    'attachment_bytes': run.attachment_bytes, 'query_chars': len(run.inputs['query']),
                                    'repairs': run.repairs, 'text_checks': run.request_text_checks,
                                    'model': snapshot.get('display_model'), 'selected_model': snapshot.get('user_selected_model')})
                except Exception as exc:
                    reports.append({'job': job_id, 'verified': False, 'error_type': type(exc).__name__,
                                    'code': getattr(exc, 'code', None), 'elapsed_s': round(time.monotonic() - started, 2)})
                finally:
                    if run:
                        await bridge.release(run)
                print(json.dumps(reports[-1], ensure_ascii=False), flush=True)
                args.output.write_text(json.dumps(reports, ensure_ascii=False, indent=2))
        finally:
            await bridge.close()
            await runtime.close()
            await asyncio.to_thread(pool.close)
    if not all(r['verified'] for r in reports):
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=Path('/app/data'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--export-only', action='store_true')
    parser.add_argument('--cases', type=Path)
    parser.add_argument('jobs', nargs='+')
    asyncio.run(main(parser.parse_args()))
