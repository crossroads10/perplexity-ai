"""One synthetic Ask request with explicit empty sources; never print credentials.

Run inside the existing adapter container (python - < this file) to reuse its
network and dependencies without changing the running server or its configuration.
"""
import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import time

from perplexity.client import Client
from perplexity.server.client_pool import build_account_cookies
from perplexity.server.utils import resolve_chat_model
from perplexity.upstream_async import search_stream
from perplexity.upstream_protocol import clean_result


async def main():
    config = json.loads(Path(os.environ['PPLX_TOKEN_POOL_CONFIG']).read_text())
    bridge = json.loads(Path('data/config/tool_bridge.json').read_text())
    entry = next(t for t in config['tokens'] if t['id'] == bridge['account_id'])
    cookies = build_account_cookies(entry.get('csrf_token'), entry.get('session_token'), entry.get('cookies'))
    client = await asyncio.to_thread(Client, cookies)
    nonce = secrets.token_hex(12)
    query = ('Return exactly one JSON object with type="final" and text="' + nonce + '". '
             'This is a local coding protocol test. Do not search the web or add citations. No code fences.')
    resolved = resolve_chat_model('claude-opus-5-5', False, [client.subscription_tier])
    started = time.monotonic()
    try:
        latest = {}
        async for event in search_stream(client, query=query, mode=resolved['mode'], model=resolved['model'],
                                         sources=[], incognito=True, timeout=180):
            latest = event
        result = clean_result(latest)
        answer = result.get('answer', '').strip()
        parsed, end = json.JSONDecoder().raw_decode(answer)
        suffix = answer[end:].strip()
        report = {'sources_sent': [], 'status': latest.get('status'),
            'elapsed_s': round(time.monotonic() - started, 2),
            'exact_json': parsed == {'type': 'final', 'text': nonce} and not suffix,
            'numeric_citation_suffix': bool(re.fullmatch(r'(?:\[\d{1,6}\]\s*)+', suffix)),
            'source_count': len(result.get('sources', [])),
            'model_downgraded': bool(latest.get('model_downgraded')),
            'display_model': latest.get('display_model')}
        report['verified'] = report['exact_json'] and report['source_count'] == 0 and not report['model_downgraded']
        print(json.dumps(report))
        if not report['verified']:
            raise SystemExit(1)
    except Exception as exc:
        print(json.dumps({'verified': False, 'error_type': type(exc).__name__, 'code': getattr(exc, 'code', None)}))
        raise SystemExit(1)
    finally:
        client.session.close()


asyncio.run(main())
