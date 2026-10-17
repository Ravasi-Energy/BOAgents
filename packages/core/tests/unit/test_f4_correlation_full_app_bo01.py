"""B-P1-1: actual HTTP replay must preserve one run and reject a changed payload."""
import json
from datetime import datetime,timezone,timedelta
from tests.unit.test_bughunt_p0_mutators_full_app import full_app  # noqa: F401


def test_f4_correlation_identical_replay_one_run_changed_payload_conflict(full_app):
    client,headers,*_=full_app
    h=headers('admin')
    settings=client.get('/bo/settings',headers=h).json()['settings']
    for key,value in [('bo.exec.enabled',True),('bo.exec.budget_cap','100 USD')]:
        setting=next(x for x in settings if x['key']==key)
        result=client.put('/bo/settings/'+key,headers=h,json={'expected_version':setting['version'],'value':value})
        assert result.status_code==200,result.text
    mandate=client.post('/bo/execution/mandates',headers=h,json={
        'allowed_resources':['synth.counter'],'allowed_actions':['increment'],
        'budget_limit':'100','budget_currency':'USD','concurrency_limit':2,
        'max_steps':10,'max_depth':1,
        'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
    assert mandate.status_code==201,mandate.text
    body={'mandate_id':mandate.json()['mandate']['mandate_id'],'steps':[{'action':'increment','resource':'synth.counter','payload':{'amount':1}}],'budget_amount':'5','correlation_id':'bo01-f4-replay'}
    first=client.post('/bo/execution/runs',headers=h,json=body)
    second=client.post('/bo/execution/runs',headers=h,json=body)
    assert first.status_code==second.status_code==201,(first.text,second.text)
    assert first.json()['run']['run_id']==second.json()['run']['run_id']
    before=client.get('/bo/execution/runs',headers=h).json()
    changed={**body,'steps':[{'action':'increment','resource':'synth.counter','payload':{'amount':2}}]}
    third=client.post('/bo/execution/runs',headers=h,json=changed)
    after=client.get('/bo/execution/runs',headers=h).json()
    print(json.dumps({'first':first.status_code,'replay':second.status_code,'sameRun':first.json()['run']['run_id'],'changedStatus':third.status_code,'changedBody':third.json(),'before':before,'after':after}))
    assert third.status_code==409,third.text
    assert after==before


def _mandate_body(**over):
    body={'allowed_resources':['synth.counter'],'allowed_actions':['increment'],
        'budget_limit':'100','concurrency_limit':2,'max_steps':10,'max_depth':1,
        'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}  # noqa: UP017
    body.update(over)
    return body


def test_money_boundary_rejects_float_and_invented_currency(full_app):  # noqa: F811
    """v2 §4-5: float JSON nu e reprezentare exactă; ZZZ nu e ISO real."""
    client,headers,*_=full_app
    h=headers('admin')
    settings=client.get('/bo/settings',headers=h).json()['settings']
    s=next(x for x in settings if x['key']=='bo.exec.enabled')
    r=client.put('/bo/settings/bo.exec.enabled',headers=h,
        json={'expected_version':s['version'],'value':True})
    assert r.status_code==200,r.text
    r=client.post('/bo/execution/mandates',headers=h,
        json=_mandate_body(budget_limit=100.5))
    assert r.status_code==422,r.text
    r=client.post('/bo/execution/mandates',headers=h,
        json=_mandate_body(budget_limit='100',budget_currency='ZZZ'))
    assert r.status_code==422,r.text
    r=client.post('/bo/execution/mandates',headers=h,
        json=_mandate_body(budget_limit='100',budget_currency='usd'))
    assert r.status_code==422,r.text  # minuscule ≠ cod ISO
    ok=client.post('/bo/execution/mandates',headers=h,
        json=_mandate_body(budget_limit='100',budget_currency='USD'))
    assert ok.status_code==201,ok.text
    assert ok.json()['mandate']['budget_currency']=='USD'
    mid=ok.json()['mandate']['mandate_id']
    steps=[{'action':'increment','resource':'synth.counter','payload':{'amount':1}}]
    r=client.post('/bo/execution/runs',headers=h,
        json={'mandate_id':mid,'steps':steps,'budget_amount':5.5})
    assert r.status_code==422,r.text
    r=client.post('/bo/execution/runs',headers=h,
        json={'mandate_id':mid,'steps':steps,'budget_amount':'5',
              'budget_currency':'EUR'})
    assert r.status_code in (400,409,422),r.text  # mismatch cu mandatul USD


def test_mandate_without_currency_refused_at_boundary(full_app):  # noqa: F811
    """v2/AC5: bugetul monetar cere monedă la crearea mandatului — NULL
    nu se reclasifică adimensional; rândurile legacy NULL nu produc
    execuție monetară (probat la nivel store în test_bo_execution)."""
    client,headers,*_=full_app
    h=headers('admin')
    settings=client.get('/bo/settings',headers=h).json()['settings']
    for key,value in [('bo.exec.enabled',True),('bo.exec.budget_cap','100 USD')]:
        s=next(x for x in settings if x['key']==key)
        r=client.put('/bo/settings/'+key,headers=h,
            json={'expected_version':s['version'],'value':value})
        assert r.status_code==200,r.text
    r=client.post('/bo/execution/mandates',headers=h,
        json=_mandate_body(budget_limit='100'))  # fără monedă → refuz 422
    assert r.status_code==422,r.text
