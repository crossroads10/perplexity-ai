"""Expose only the bridge callback through a tunnel, never the main server API.

Run with: python scripts/tool_bridge_proxy.py --config data/config/tool_bridge.json
Point the tunnel at 127.0.0.1:8767. Public MCP URL: https://HOST/SECRET/mcp
"""
import argparse
import json
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web


def make_app(secret, upstream):
    async def lifespan(app):
        async with ClientSession(timeout=ClientTimeout(total=28)) as session:
            app["session"] = session
            yield

    async def callback(request):
        if request.headers.get("Origin"):
            return web.Response(status=403)
        async with request.app["session"].request(request.method,
                upstream.rstrip('/') + f"/bridge/{secret}/mcp", data=await request.read(),
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}) as response:
            return web.Response(status=response.status, body=await response.read(),
                                headers={"Content-Type": response.headers.get("Content-Type", "application/json")})
    app = web.Application(client_max_size=1024 * 1024)
    app.cleanup_ctx.append(lifespan)
    app.router.add_route('*', f'/{secret}/mcp', callback)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='data/config/tool_bridge.json')
    parser.add_argument('--upstream', default='http://127.0.0.1:8000')
    parser.add_argument('--port', type=int, default=8767)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    web.run_app(make_app(config['secret'], args.upstream), host='127.0.0.1', port=args.port, access_log=None)
