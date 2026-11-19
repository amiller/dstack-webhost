"""Dedicated project hosts must retain root paths without exposing sibling apps."""
import asyncio
import json
from types import SimpleNamespace
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from .ingress import Ingress

class Store:
    def load(self, name):
        if name not in ('private-agent', 'sibling'):
            raise FileNotFoundError(name)
        return SimpleNamespace(name=name, runtime='deno', mode='attested', isolation='shared')

class Runtime:
    def get_route(self, runtime, mode):
        return '127.0.0.1', 12345

async def journey():
    ingress=Ingress(Store(),None,None,None,Runtime(),None,None)
    async def proxy(request, ip, port, path):
        return web.json_response({'path':path})
    ingress._proxy=proxy
    app=web.Application();app.router.add_route('*','/{path:.*}',ingress.handle)
    async with TestClient(TestServer(app)) as client:
        for path,expected in [('/', '/private-agent/'),('/app.js','/private-agent/app.js'),('/continuity/email/request?x=1','/private-agent/continuity/email/request?x=1'),('/sibling/private','/private-agent/sibling/private'),('/_api/projects','/private-agent/_api/projects')]:
            response=await client.get(path,headers={'Host':'private-agent.example.com'})
            assert response.status==200
            assert (await response.json())['path']==expected
        response=await client.get('/sibling/private',headers={'Host':'pod.example.com'})
        assert (await response.json())['path']=='/sibling/private'

def test_project_host_routes_root_and_isolates_siblings(monkeypatch):
    monkeypatch.setenv('PROJECT_HOSTS',json.dumps({'private-agent.example.com':'private-agent'}))
    asyncio.run(journey())
