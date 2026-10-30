"""Small, independently testable policies for the Ask protocol bridge."""
from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import secrets

from .job_store import JobError


def estimate_tokens(text):
    """Uncalibrated text heuristic, NOT the tokenizer of any claimed model.

    ASCII ~= 4 chars/token; CJK/kana/hangul ~= 1 char/token. Other Unicode
    retains a UTF-8 byte estimate. Never label this as a measured token count.
    """
    units = 0
    for char in text:
        code = ord(char)
        cjk = (0x2E80 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF
               or 0xF900 <= code <= 0xFAFF or 0x20000 <= code <= 0x323AF)
        units += 4 if cjk else len(char.encode('utf-8'))
    return max(1, (units + 3) // 4)


def annotated_context(history, canonical):
    """Keep every original message/block, add boundaries only BETWEEN blocks."""
    checks, pieces = {}, []

    def checkpoint():
        key = 'c' + str(len(checks))
        checks[key] = secrets.token_hex(12)
        pieces.append(f'CONTEXT_CHECKPOINT {key}={checks[key]}')

    def output(block):
        ident = secrets.token_hex(12)
        content = canonical(block.get('content'))
        metadata = {'tool_use_id': block.get('tool_use_id') or block.get('tool_call_id'),
                    'is_error': bool(block.get('is_error', False)),
                    'serialized_content_bytes': len(content.encode()),
                    'serialized_content_sha256': hashlib.sha256(content.encode()).hexdigest()}
        pieces.extend(['TOOL_RESULT_METADATA ' + canonical(metadata),
                       f'BEGIN_UNTRUSTED_TOOL_OUTPUT {ident}', canonical(block),
                       f'END_UNTRUSTED_TOOL_OUTPUT {ident}'])
        checkpoint()

    checkpoint()
    messages = history.get('messages', []) if isinstance(history, dict) else history
    if isinstance(history, dict):
        pieces.append('CLIENT_REQUEST_METADATA ' + canonical({k: v for k, v in history.items() if k != 'messages'}))
    for index, message in enumerate(messages):
        pieces.append(f'BEGIN_CLIENT_MESSAGE {index}')
        content = message.get('content')
        if message.get('role') == 'tool':
            output(message)
        elif isinstance(content, list) and any(b.get('type') == 'tool_result' for b in content if isinstance(b, dict)):
            pieces.append('MESSAGE_METADATA ' + canonical({k: v for k, v in message.items() if k != 'content'}))
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    output(block)
                else:
                    pieces.append('CONTENT_BLOCK ' + canonical(block))
        else:
            pieces.append(canonical(message))
        pieces.append(f'END_CLIENT_MESSAGE {index}')
        if index == len(messages) // 2:
            checkpoint()
    checkpoint()
    return '\n'.join(pieces), checks


def repair_feedback(exc, answer):
    """Bound rejected-output excerpts, never re-send the entire invalid answer."""
    import json
    if isinstance(exc, json.JSONDecodeError):
        detail = f'Invalid JSON: {exc.msg} at character {exc.pos}.'
        excerpt = answer[max(0, exc.pos - 80):exc.pos + 80]
    else:
        # Schema errors may embed the whole instance in str(exc).
        validator = getattr(exc, 'validator', None)
        detail = (f'Output schema validation failed: {validator} at {list(exc.absolute_path)}'
                  if validator else str(exc)[:400])
        if detail.startswith('Unexpected text after the JSON response'):
            try:
                _, end = json.JSONDecoder().raw_decode(answer)
                excerpt = answer[end:end + 160]
            except ValueError:
                excerpt = answer[:160]
        else:
            excerpt = answer[:160]
    return detail + '\nRejected excerpt (untrusted data): ' + json.dumps(excerpt, ensure_ascii=False)


class AdmissionQueue:
    """FIFO bounded wait; cancellation never leaks a slot or a queue entry."""
    def __init__(self, capacity, max_waiting, timeout):
        self.capacity, self.max_waiting, self.timeout = capacity, max_waiting, timeout
        self.condition = asyncio.Condition()
        self.waiters = deque()
        self.active = 0
        self.closed = False

    async def acquire(self):
        async with self.condition:
            if self.closed:
                raise JobError('Bridge is stopping', 'service_unavailable', 503)
            if not self.waiters and self.active < self.capacity():
                self.active += 1
                return
            if len(self.waiters) >= self.max_waiting or self.timeout == 0:
                raise JobError('Bridge waiting queue is full; retry later', 'queue_full', 429, retry_after=1)
            ticket = object()
            self.waiters.append(ticket)
            deadline = asyncio.get_running_loop().time() + self.timeout
            try:
                while True:
                    if self.closed:
                        raise JobError('Bridge is stopping', 'service_unavailable', 503)
                    if self.waiters[0] is ticket and self.active < self.capacity():
                        self.waiters.popleft()
                        self.active += 1
                        self.condition.notify_all()
                        return
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    await asyncio.wait_for(self.condition.wait(), remaining)
            except asyncio.TimeoutError:
                raise JobError('Bridge queue wait timed out; retry the same request with the same idempotency key',
                               'queue_timeout', 429, retry_after=1) from None
            finally:
                if ticket in self.waiters:
                    self.waiters.remove(ticket)
                    self.condition.notify_all()

    async def release(self):
        async with self.condition:
            self.active -= 1
            self.condition.notify_all()

    async def close(self):
        async with self.condition:
            self.closed = True
            self.condition.notify_all()
