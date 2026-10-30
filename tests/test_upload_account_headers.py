"""Account routing belongs on the signing API, never on external storage."""
import asyncio
from types import SimpleNamespace

import pytest

from perplexity import upstream_async
from perplexity.client import Client


def test_account_header_prefers_resolved_account_and_falls_back_to_cookie():
    client=Client.__new__(Client)
    client._cookies={'__Secure-pplx.session.fixture-account':'private-test-value'}
    assert client.account_request_headers()=={'x-pplx-account':'fixture-account'}
    client._user_info={'user':{'id':'resolved-account'}}
    assert client.account_request_headers()=={'x-pplx-account':'resolved-account'}


@pytest.mark.asyncio
async def test_upload_header_not_forwarded_to_storage(monkeypatch):
    requests=[]
    class Session:
        curl_options={}
        def __init__(self,**kwargs):
            assert 'x-pplx-account' not in kwargs['headers']
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def post(self,url,**kwargs):
            requests.append((url,kwargs))
            if len(requests)==1:
                return SimpleNamespace(status_code=200,headers={},json=lambda:{'fields':{},'s3_bucket_url':'https://storage.invalid/upload','s3_object_url':'https://storage.invalid/file'})
            if len(requests)==2:
                return SimpleNamespace(status_code=204,headers={})
            raise asyncio.CancelledError()
    class Account:
        def account_request_headers(self): return {'x-pplx-account':'fixture-account'}
        def prepare_search(self,*args): return {'params':{'attachments':[],'model_preference':'fixture'}},{}
        def cookie_snapshot(self): return 0,{}
    monkeypatch.setattr(upstream_async,'AsyncSession',Session)
    with pytest.raises(asyncio.CancelledError):
        async for _ in upstream_async.search_stream(Account(),query='fixture',mode='auto',files={'context.txt':b'context'}): pass
    assert requests[0][1]['headers']=={'x-pplx-account':'fixture-account'}
    assert 'headers' not in requests[1][1]
