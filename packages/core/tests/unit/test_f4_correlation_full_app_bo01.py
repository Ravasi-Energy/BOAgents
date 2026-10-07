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
        'budget_limit':'100','concurrency_limit':2,'max_steps':10,'max_depth':1,
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
