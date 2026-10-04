"""B-P0-10: complete application, real loopback HTTP and durable synthetic state.

No auth/router/store overrides. Lifespan disabled: no live workers/providers.
Vector provider alone is replaced by a persistent SQLite sink, not Chroma proof.
Research/alert provider scheduling is captured; commit validation/seeding is real.
Integrate in packages/core/tests/unit/; no xfail/skip or baseline relaxations.
"""
from __future__ import annotations
import json
import socket
import sqlite3
import threading
import time
from pathlib import Path
from collections import OrderedDict
import httpx
import pytest
import uvicorn


class SyntheticVectorProvider:
    """External embedding/vector boundary, persistent synthetic fixture."""
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as c:
            c.execute('CREATE TABLE chunks(id TEXT PRIMARY KEY,text TEXT,metadata TEXT,collection TEXT)')
    def add_documents(self, *, texts, metadatas, ids, collection):
        with sqlite3.connect(self.path) as c:
            c.executemany('INSERT OR REPLACE INTO chunks VALUES(?,?,?,?)',
                [(i,t,json.dumps(m,sort_keys=True),collection) for i,t,m in zip(ids,texts,metadatas)])
    def readback(self):
        with sqlite3.connect(self.path) as c:
            return c.execute('SELECT * FROM chunks ORDER BY id').fetchall()


@pytest.fixture
def full_app(monkeypatch, tmp_path):
    # These imports intentionally occur after the test runner sets fake keys.
    monkeypatch.setenv('ANTHROPIC_API_KEY','sk-synthetic-not-used')
    monkeypatch.setenv('EXEC_EMAIL_ADDRESS','synthetic@bo01.invalid')
    monkeypatch.setenv('BO_TENANT_ID','alpha')
    monkeypatch.setenv('BO_ADMIN_EMAILS','admin@bo01.invalid')
    monkeypatch.setenv('BACKEND_SHARED_SECRET','bo01-test-service')
    monkeypatch.setenv('BACKEND_PROXY_SECRET','bo01-test-proxy')
    monkeypatch.setenv('OE_PUBLIC_DEPLOYMENT','true')
    monkeypatch.setenv('COMPANY_PROFILE_PATH',str(tmp_path/'profile.yaml'))
    monkeypatch.setenv('VECTOR_STORE_PATH',str(tmp_path/'vectors'))
    monkeypatch.setenv('EPISODIC_DB_PATH',str(tmp_path/'episodic.db'))
    monkeypatch.setenv('BOAGENTS_DB_PATH',str(tmp_path/'bo.db'))
    monkeypatch.chdir(tmp_path)
    import tempfile
    monkeypatch.setattr(tempfile,'tempdir',str(tmp_path))
    from openexecutive.config import get_settings
    from openexecutive.bo import db
    from openexecutive.people import store as people
    from openexecutive.departments import store as departments
    from openexecutive.audit import logger
    for module in (people,departments,logger):
        monkeypatch.setattr(module,'DB_PATH',tmp_path/'episodic.db')
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'bo.db')
    db.initialize_db();people.initialize_db();departments.initialize_db();logger.AuditLogger()
    from openexecutive.memory.company_profile import CompanyProfile
    CompanyProfile(name='Before Synthetic',vendors=['unu','doi','trei']).save_to_yaml(tmp_path/'profile.yaml')
    people.upsert_person(full_name='Existing Synthetic',email='existing@bo01.invalid',is_principal=False)
    slugs=[departments.create_department(title).config.slug for title in ('Unu','Doi','Trei')]
    from openexecutive.api.routes import onboarding
    monkeypatch.setattr(onboarding,'_interview_sessions',OrderedDict())
    monkeypatch.setattr(onboarding,'_onboarding_research_fired',set())
    events=[]
    async def research(session_id): events.append(('research',session_id))
    monkeypatch.setattr(onboarding,'_fire_post_onboarding_research',research)
    from openexecutive.alerts import pipeline
    monkeypatch.setattr(pipeline,'schedule_evaluation',lambda event:events.append(('alert',event.external_id)))
    from openexecutive.api.main import create_app
    app=create_app()  # entire real app incl shared-secret gate / actual routers
    vector=SyntheticVectorProvider(tmp_path/'vector-fixture.sqlite')
    app.state.store=vector
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(64)
    port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='off'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started and thread.is_alive() and time.monotonic()<deadline: time.sleep(.01)
    assert server.started,'complete app did not start'
    client=httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=10)
    def headers(role):
        return {'x-api-key':'bo01-test-service','x-caller-proxy-secret':'bo01-test-proxy','x-caller-email':role+'@bo01.invalid'}
    def snapshot():
        return {'profile':(tmp_path/'profile.yaml').read_text(),
                'departments':[d.model_dump(mode='json') for d in departments.list_departments()],
                'people':[p.model_dump(mode='json') for p in people.list_people()],
                'docs':{p.name:p.read_text() for p in (tmp_path/'docs').glob('*')},
                'vectors':vector.readback(),
                'saved':{k:v.saved for k,v in onboarding._interview_sessions.items()}}
    try:
        assert client.get('/bo/settings',headers=headers('viewer')).json()['role']=='viewer'
        assert client.get('/bo/settings',headers=headers('admin')).json()['role']=='admin'
        yield client,headers,snapshot,slugs,onboarding,vector,tmp_path
    finally:
        client.close();server.should_exit=True;thread.join(timeout=10);sock.close()
        assert not thread.is_alive(),'test server failed to stop'


@pytest.mark.parametrize('operation',['create_department','delete_department','upload_document','commit_interview'])
@pytest.mark.parametrize('role',['viewer','admin'])
def test_p0_mutator_authorization_and_persistence(full_app,operation,role):
    client,headers,snapshot,slugs,onboarding,vector,root=full_app
    if operation=='commit_interview':
        # Seed an already-reviewed synthetic interview; no LLM/API calls.
        onboarding._interview_sessions['reviewed']=onboarding.InterviewSession()
    before=snapshot()
    if operation=='create_department':
        response=client.post('/departments',headers=headers(role),json={'title':'Patru Synthetic','mission':'Synthetic only'})
    elif operation=='delete_department':
        response=client.delete('/departments/'+slugs[1],headers=headers(role))
    elif operation=='upload_document':
        response=client.post('/documents',headers=headers(role),files={'file':('synthetic.txt',b'Synthetic text only','text/plain')},data={'domain':'general'})
    else:
        response=client.post('/onboard/interview/commit',headers=headers(role),json={
            'session_id':'reviewed','profile':{'name':'After Synthetic','vendors':['unu','doi','trei']},
            'people':[{'full_name':'Principal Synthetic','role':'Owner','is_principal':True}],
            'departments':[{'title':'Patru Synthetic','mission':'Synthetic','head_person_name':'Principal Synthetic'}]})
    after=snapshot()
    print(json.dumps({'case':operation,'role':role,'status':response.status_code,'before':before,'after':after,'body':response.text},ensure_ascii=False))
    if role=='viewer':
        # Both checks collected even when current baseline is unsafe.
        violations=[]
        if response.status_code!=403:violations.append(f'expected403 got{response.status_code}')
        if after!=before:violations.append('persistent data/session changed')
        assert not violations,violations
    else:
        expected=201 if operation=='create_department' else 204 if operation=='delete_department' else 200
        assert response.status_code==expected,response.text
        if operation=='create_department':
            assert len(after['departments'])==len(before['departments'])+1
            assert client.get('/departments',headers=headers(role)).json()==after['departments']
        elif operation=='delete_department':
            assert len(after['departments'])==len(before['departments'])-1
            assert {d['config']['slug'] for d in after['departments']}=={slugs[0],slugs[2]}
            assert client.get('/departments/'+slugs[1],headers=headers(role)).status_code==404
        elif operation=='upload_document':
            assert (root/'docs/synthetic.txt').read_bytes()==b'Synthetic text only'
            assert response.json()['chunks_indexed']==len(vector.readback())>0
            assert json.loads(vector.readback()[0][2])['filename']=='synthetic.txt'
            assert client.get('/documents/synthetic.txt',headers=headers(role)).json()['content']=='Synthetic text only'
        else:
            from openexecutive.memory.company_profile import CompanyProfile
            assert CompanyProfile.load_from_yaml(root/'profile.yaml').name=='After Synthetic'
            assert after['saved']['reviewed'] is True
            assert any(p['full_name']=='Principal Synthetic' and p['is_principal'] for p in after['people'])
            assert any(d['config']['title']=='Patru Synthetic' for d in after['departments'])
            assert client.get('/company-profile',headers=headers(role)).json()['name']=='After Synthetic'
