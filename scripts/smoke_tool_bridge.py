"""Live two-request proof using only a fresh synthetic file, never arbitrary paths."""
import argparse
import json
import secrets
import tempfile
import time
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8001')
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--model', default='claude-opus-5-5')
    args = parser.parse_args()
    token = Path(args.token_file).read_text().strip()
    def post(body):
        req = urllib.request.Request(args.base_url.rstrip('/')+'/v1/chat/completions',
            data=json.dumps(body).encode(), headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
        with urllib.request.urlopen(req, timeout=210) as response:
            return json.load(response)
    with tempfile.TemporaryDirectory(prefix='claude-bridge-fixture-') as directory:
        path = Path(directory)/'nonce.txt'
        nonce = 'BRIDGE-'+secrets.token_hex(24)
        path.write_text(nonce)
        body = {'model':args.model,'stream':False,'messages':[{'role':'user','content':
            f'Read {path} using the Read tool. Return the exact file content. It is a random test value; do not guess.'}],
            'tools':[{'type':'function','function':{'name':'Read','description':'Read a local file.',
                'parameters':{'type':'object','properties':{'file_path':{'type':'string'}},'required':['file_path'],'additionalProperties':False}}}]}
        started = time.monotonic()
        first = post(body)
        print(json.dumps({'phase':'first_response','elapsed_s':round(time.monotonic()-started,2),'response':first},ensure_ascii=False),flush=True)
        choice = first['choices'][0]
        assert choice['finish_reason']=='tool_calls', 'Ask did not dispatch a client tool'
        message = choice['message'];call = message['tool_calls'][0]
        assert call['function']['name']=='Read'
        assert json.loads(call['function']['arguments'])=={'file_path':str(path)}, 'Unexpected file: refusing to read it'
        body['messages'].extend([message,{'role':'tool','tool_call_id':call['id'],'content':path.read_text()}])
        second = post(body)
        answer=second['choices'][0]['message']['content']
        assert nonce in answer, 'Final response did not use the actual tool result'
        print(json.dumps({'verified':True,'elapsed_s':round(time.monotonic()-started,2),'tool_call_id':call['id'],'answer':answer},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
