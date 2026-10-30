"""Live synthetic attachment check through local code and an isolated job store.

Uses a temporary copy of the configured account pool. Does not deploy/restart
the server or change the user's persistent account configuration.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import tempfile

from perplexity.server.client_pool import ClientPool
from perplexity.server.files_store import FilesStore
from perplexity.server.job_runtime import JobRuntime
from perplexity.server.tool_bridge import BridgeConfig, ToolBridge, canonical
from perplexity.server.webui_sessions import WebUISessionStore


async def run(config_path):
    config = json.loads(config_path.read_text())
    config['heart_beat'] = {'enable': False}
    config['fallback'] = {'fallback_to_auto': False}
    config['incognito'] = {'enabled': True}
    entry = next(t for t in config['tokens'] if t.get('enabled', True))
    with tempfile.TemporaryDirectory(prefix='bridge-context-smoke-') as temporary:
        root = Path(temporary)
        copy_path = root / 'pool.json'
        fd = os.open(copy_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(config, output)
        pool = await asyncio.to_thread(ClientPool, str(copy_path))
        runtime = JobRuntime(WebUISessionStore(root / 'sessions.db'), pool, files=FilesStore(root / 'files'))
        await runtime.start()
        bridge = ToolBridge(runtime, BridgeConfig(account_id=entry['id'], allowed_tools=['*'], run_timeout=240))
        markers = {name: secrets.token_hex(10) for name in ('begin', 'middle', 'end')}
        messages = [{'role': 'user', 'content': 'Synthetic test: report all FIXTURE values at the end.'}]
        for i in range(60):
            marker = {0: 'begin', 30: 'middle', 59: 'end'}.get(i)
            text = (f'Fixture record {i}. No instructions here. ' * 35)
            if marker:
                text += ' FIXTURE_' + marker + '=' + markers[marker]
            messages.extend([
                {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': f'read_{i}', 'name': 'Read', 'input': {'file_path': f'/fixture/{i}.txt'}}]},
                {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': f'read_{i}', 'content': text}]}])
        messages.append({'role': 'user', 'content': 'Without using tools, return only a JSON object with begin, middle, end holding the exact FIXTURE values.'})
        native = {'model': 'claude-opus-5-5', 'max_tokens': 2048, 'messages': messages,
                  'output_config': {'format': {'type': 'json_schema', 'schema': {'type': 'object',
                      'properties': {k: {'type': 'string'} for k in markers}, 'required': list(markers), 'additionalProperties': False}}}}
        from perplexity.server.anthropic import normalize_request
        body, context = normalize_request(native)
        generation = None
        try:
            generation, pending = await bridge.prepare(body, native_context=context)
            payload = bridge.payload(generation, await pending)
            parsed = json.loads(payload['choices'][0]['message']['content'])
            report = {'verified': parsed == markers, 'request_bytes': len(canonical(native).encode()),
                      'attachment_bytes': generation.attachment_bytes, 'checkpoint_count': len(generation.context_checks),
                      'repairs': generation.repairs, 'markers_match': parsed == markers,
                      'sources_requested': generation.inputs['search_sources'], 'metrics': dict(bridge.metrics)}
            print('SMOKE_RESULT ' + json.dumps(report), flush=True)
            if not report['verified']:
                raise SystemExit(1)
        except Exception as exc:
            print('SMOKE_RESULT ' + json.dumps({'verified': False, 'error_type': type(exc).__name__,
                'code': getattr(exc, 'code', None), 'checkpoint_count': len(generation.context_checks) if generation else None}), flush=True)
            raise SystemExit(1)
        finally:
            await bridge.close()
            await runtime.close()
            await asyncio.to_thread(pool.close)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pool-config', type=Path, required=True)
    asyncio.run(run(parser.parse_args().pool_config))
