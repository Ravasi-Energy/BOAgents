"""Adverse regressions for BO-SOL-02 audit, synthetic isolated database."""
import pytest
from starlette.requests import Request

from openexecutive.bo import identity
from openexecutive.bo.execution import engine, guardian, store
from openexecutive.bo.execution.mandate import MandateValidationError
from openexecutive.bo.execution.synth import SyntheticCounterProvider
from openexecutive.bo.settings import store as settings

from .test_bo_execution import TENANT, _enable, _mandate, _run, _step, _work
from .test_bo_execution import audit as audit
from .test_bo_execution import db as db


def test_f1_service_cannot_delegate_admin(monkeypatch):
    monkeypatch.setenv('BO_ADMIN_EMAILS', 'admin@example.invalid')
    monkeypatch.setattr(identity, '_is_principal_email', lambda _: False)
    req = Request({'type': 'http', 'headers': [(b'x-api-key', b'service'), (b'x-caller-email', b'admin@example.invalid')], 'query_string': b''})
    with pytest.raises(identity.UnauthenticatedError):
        identity.resolve_identity(req)


def test_f2_ancestor_guardian_is_checked(db, monkeypatch):
    _enable()
    parent = _mandate(guardian_ref='revoked-parent')
    child = _mandate(parent=parent, guardian_ref='other-child')
    def check(tenant, mandate, **kw):
        if mandate.guardian_ref == 'revoked-parent':
            raise guardian.GuardianDeniedError('revoked', 'revoked ancestor')
    monkeypatch.setattr(guardian, 'assert_effect_authorized', check)
    run = _run(child)
    _work(SyntheticCounterProvider(idempotent=True))
    assert store.get_run(TENANT, run['run_id'])['state'] == store.RUN_FAILED


def test_f3_disabled_before_claim(db):
    _enable()
    run = _run(_mandate())
    settings.set_value(TENANT, 'bo.exec.enabled', False, expected_version=1, actor='admin')
    assert _work(SyntheticCounterProvider(idempotent=True))['claimed'] == 0
    assert store.get_run(TENANT, run['run_id'])['state'] == store.RUN_PENDING


def test_f4_resume_reacquires_capacity(db):
    _enable()
    m = _mandate(concurrency_limit=1)
    run = _run(m)
    store.transition_run(TENANT, run['run_id'], store.RUN_UNKNOWN, clear_lease=True, reservation_state=store.RES_RELEASED)
    _run(m)
    with pytest.raises(store.BudgetExceededError):
        engine.resume_run(TENANT, run['run_id'], actor='admin')


def test_f5_reconcile_invalidates_old_fence(db):
    _enable()
    run = _run(_mandate())
    entry = store.get_or_create_intent(TENANT, run, 0, provider='synth', payload={})
    claimed = store.claim_ledger_entry(TENANT, entry['entry_id'], worker_id='old', lease_s=60)
    store.mark_ledger_status(TENANT, entry['entry_id'], store.LED_SUCCEEDED, receipt_ref='receipt-safe')
    assert not store.finalize_ledger_entry(TENANT, entry['entry_id'], status=store.LED_UNKNOWN, fence_version=claimed['fence_version'])
    assert store.get_ledger_entry(TENANT, entry['entry_id'])['receipt_ref'] == 'receipt-safe'


def test_f6_children_share_parent_capacity(db):
    _enable()
    parent = _mandate(budget_limit='1', concurrency_limit=1)
    children = [_mandate(parent=parent, budget_limit='1', concurrency_limit=1) for _ in range(2)]
    _run(children[0])
    with pytest.raises(store.BudgetExceededError):
        _run(children[1])


@pytest.mark.parametrize('flag,state', [('cancel_requested', store.RUN_CANCELLED), ('pause_requested', store.RUN_PAUSED)])
def test_f7_pending_control(db, flag, state):
    _enable()
    run = _run(_mandate())
    result = store.request_flag(TENANT, run['run_id'], flag, actor='admin')
    assert result['state'] == state
    if state == store.RUN_PAUSED:
        assert engine.resume_run(TENANT, run['run_id'], actor='admin')['state'] == store.RUN_PENDING
    else:
        assert store.reservation_for(TENANT, run['run_id'])['state'] == store.RES_RELEASED


def test_f8_admin_step_cap(db):
    _enable()
    settings.set_value(TENANT, 'bo.exec.max_steps', 1, expected_version=0, actor='admin')
    with pytest.raises(MandateValidationError):
        _run(_mandate(), [_step(), _step()])


def test_f9_authority_revoked_local(db):
    from openexecutive.api.routes.bo import run_authority
    _enable()
    m = _mandate()
    run = _run(m)
    store.revoke_mandate(TENANT, m.mandate_id, reason='test', actor='admin')
    ident = identity.Identity('admin', TENANT, 'admin', False, 'dev')
    assert run_authority(run['run_id'], ident)['authorized'] is False


def test_f10_dead_letter_retry_new_series(db, monkeypatch):
    from openexecutive.bo.routing import delivery
    from openexecutive.bo.routing import store as outbox
    outbox.enqueue_outbox(TENANT, 'catalog', 'sample', {'eventId': 'evt-synthetic'})
    with outbox.get_conn() as conn:
        conn.execute('UPDATE bo_telemetry_outbox SET attempts=26, delivered=2')
    calls = []
    class Adapter:
        def deliver_event(self, envelope, **_kwargs):
            calls.append(envelope)
            return {'status': 'RECEIVED'}
    outbox.retry_outbox_entry(TENANT, 'evt-synthetic', reason='transport restored', actor='admin')
    delivery.deliver_pending(TENANT, adapter=Adapter())
    assert calls == [{'eventId': 'evt-synthetic'}]
    row = outbox.list_outbox(TENANT)[0]
    assert row['attempts'] == 27
    assert row['delivered'] == 1


def test_f3_disabled_during_checkpoint(db):
    _enable()
    run = _run(_mandate())
    def checkpoint(*args, **kwargs):
        settings.set_value(TENANT, 'bo.exec.enabled', False, expected_version=1, actor='admin')
        return store.write_checkpoint(*args, **kwargs)
    _work(SyntheticCounterProvider(idempotent=True), checkpoint_writer=checkpoint)
    assert store.get_run(TENANT, run['run_id'])['state'] == store.RUN_PAUSED
    assert store.list_ledger(TENANT, run['run_id']) == []


def test_f1_authenticated_proxy_and_wrong_secret(monkeypatch):
    monkeypatch.setenv('BACKEND_PROXY_SECRET', 'proxy-only')
    monkeypatch.setenv('BACKEND_SHARED_SECRET', 'service-only')
    monkeypatch.setenv('BO_ADMIN_EMAILS', 'admin@example.invalid')
    for secret in (b'service-only', b'wrong', b'proxy-only'):
        req = Request({'type': 'http', 'headers': [(b'x-api-key', b'service-only'), (b'x-caller-email', b'admin@example.invalid'), (b'x-caller-proxy-secret', secret)], 'query_string': b''})
        if secret == b'proxy-only':
            assert identity.resolve_identity(req).role == 'admin'
        else:
            with pytest.raises(identity.UnauthenticatedError):
                identity.resolve_identity(req)


def test_f4_unknown_retains_budget_and_resume_deduplicates(db):
    _enable()
    m = _mandate(budget_limit='2', concurrency_limit=1)
    first = _run(m)
    provider = SyntheticCounterProvider(idempotent=True, fail_after_write=True)
    _work(provider)
    assert store.reservation_for(TENANT, first['run_id'])['state'] == 'EXPOSED'
    second = _run(m)
    with pytest.raises(store.BudgetExceededError):
        engine.resume_run(TENANT, first['run_id'], actor='admin')
    store.request_flag(TENANT, second['run_id'], 'cancel_requested', actor='admin')
    engine.resume_run(TENANT, first['run_id'], actor='admin')
    provider.fail_after_write = False
    _work(provider)
    assert provider.total(TENANT) == 1
    assert provider.submit_calls == 1
    assert store.reservation_for(TENANT, first['run_id'])['state'] == store.RES_COMMITTED


def test_f6_concurrent_siblings_decimal_budget(db):
    from concurrent.futures import ThreadPoolExecutor
    _enable()
    parent = _mandate(budget_limit='0.3', concurrency_limit=2)
    children = [_mandate(parent=parent, budget_limit='0.3', concurrency_limit=1) for _ in range(2)]
    def submit(child):
        try:
            _run(child, budget='0.2')
            return True
        except store.BudgetExceededError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, children)) == [False, True]


def test_f5_reconciliation_cas_and_active_worker(db):
    _enable()
    run = _run(_mandate())
    entry = store.get_or_create_intent(TENANT, run, 0, provider='synth', payload={})
    claim = store.claim_ledger_entry(TENANT, entry['entry_id'], worker_id='active', lease_s=60)
    with pytest.raises(store.ConflictError):
        store.mark_ledger_status(TENANT, entry['entry_id'], store.LED_INTENT, expected_fence=claim['fence_version'])
    store.mark_ledger_status(TENANT, entry['entry_id'], store.LED_SUCCEEDED, expected_fence=claim['fence_version'], receipt_ref='proof')
    with pytest.raises(store.ConflictError):
        store.mark_ledger_status(TENANT, entry['entry_id'], store.LED_UNKNOWN, expected_fence=claim['fence_version'])


def test_f1_real_http_gate_rejects_service_impersonation(db, monkeypatch):
    from fastapi.testclient import TestClient

    from openexecutive.api.main import create_app
    monkeypatch.setenv('BACKEND_SHARED_SECRET', 'synthetic-service')
    monkeypatch.setenv('BACKEND_PROXY_SECRET', 'synthetic-proxy')
    monkeypatch.setenv('BO_TENANT_ID', TENANT)
    monkeypatch.setenv('BO_ADMIN_EMAILS', 'admin@example.invalid')
    client = TestClient(create_app())  # no lifespan: no agents/services started
    payload = {'value': True, 'expected_version': 0}
    headers = {'x-api-key': 'synthetic-service', 'x-caller-email': 'admin@example.invalid'}
    assert client.put('/bo/settings/bo.exec.enabled', headers=headers, json=payload).status_code == 401
    headers.pop('x-caller-email')
    assert client.put('/bo/settings/bo.exec.enabled', headers=headers, json=payload).status_code == 403
    headers.update({'x-caller-email': 'admin@example.invalid', 'x-caller-proxy-secret': 'synthetic-proxy'})
    assert client.put('/bo/settings/bo.exec.enabled', headers=headers, json=payload).status_code == 200


def test_f2_inherited_binding_and_checkpoint_revocation(db, monkeypatch):
    _enable()
    parent = _mandate(guardian_ref='parent-auth')
    child = _mandate(parent=parent)
    assert child.guardian_ref == parent.guardian_ref
    revoked = False
    def check(*args, **kwargs):
        if revoked:
            raise guardian.GuardianDeniedError('revoked', 'revoked at checkpoint')
    monkeypatch.setattr(guardian, 'assert_effect_authorized', check)
    run = _run(child)
    def checkpoint(*args, **kwargs):
        nonlocal revoked
        revoked = True
        return store.write_checkpoint(*args, **kwargs)
    provider = SyntheticCounterProvider(idempotent=True)
    _work(provider, checkpoint_writer=checkpoint)
    assert provider.submit_calls == 0
    assert store.get_run(TENANT, run['run_id'])['state'] == store.RUN_FAILED


def test_f10_upgrade_preserves_history(db):
    from openexecutive.bo.routing import store as outbox
    outbox.enqueue_outbox(TENANT, 'catalog', 'upgrade', {'eventId': 'evt-upgrade'})
    with outbox.get_conn() as conn:
        conn.execute('ALTER TABLE bo_telemetry_outbox DROP COLUMN retry_base')
        conn.execute('DROP TABLE bo_outbox_retries')
        conn.execute("UPDATE bo_telemetry_outbox SET attempts=26, delivered=2, last_error='cap'")
    outbox.initialize_db()
    outbox.initialize_db()
    outbox.retry_outbox_entry(TENANT, 'evt-upgrade', reason='restored', actor='admin')
    row = outbox.list_outbox(TENANT)[0]
    assert row['attempts'] == 26 and row['series_attempts'] == 0
    assert row['retry_history'][0]['attempts_before'] == 26
    assert row['retry_history'][0]['last_error'] == 'cap'
    assert row['envelope'] == {'eventId': 'evt-upgrade'}


def test_f4_upgrade_retains_legacy_ambiguous_exposure(db):
    _enable()
    m = _mandate(budget_limit='1', concurrency_limit=1)
    run = _run(m)
    _work(SyntheticCounterProvider(idempotent=True, fail_after_write=True))
    with store.get_conn() as conn:
        conn.execute('UPDATE bo_budget_reservations SET state=? WHERE run_id=?', (store.RES_RELEASED, run['run_id']))
    store.initialize_db()
    assert store.reservation_for(TENANT, run['run_id'])['state'] == store.RES_EXPOSED
    with pytest.raises(store.BudgetExceededError):
        _run(m)


@pytest.mark.parametrize('alter', [
    {'product': 'Hire'}, {'installationId': 'another-installation'},
    {'tenantRef': 'another-tenant'}, {'mandateId': 'another-mandate'},
    {'allowed': {'actions': ['read'], 'resources': ['synth.counter']}},
    {'allowed': {'actions': ['increment'], 'resources': ['another.counter']}},
    {'allowed': None},
])
def test_common_contract_effect_binding(db, monkeypatch, alter):
    from .test_bo_execution import _guardian_active
    _enable()
    mandate = _mandate(guardian_ref='bound')
    monkeypatch.setattr(guardian, '_link_config', lambda *a: ('http://synthetic.invalid', 'synthetic', 1, True))
    monkeypatch.setattr(guardian, '_policy_token', lambda *a: None)
    def response(method, url, token, timeout):
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(url).query)
        assert query['action'] == ['increment'] and query['resource'] == ['synth.counter']
        body = _guardian_active(url)
        body.update(alter)
        return 200, body
    monkeypatch.setattr(guardian, '_request', response)
    run = _run(mandate)
    provider = SyntheticCounterProvider(idempotent=True)
    _work(provider)
    assert provider.submit_calls == 0
    assert store.get_run(TENANT, run['run_id'])['state'] == store.RUN_FAILED


def test_common_contract_terminal_epoch(db):
    from openexecutive.bo.routing import store as outbox
    _enable()
    run = _run(_mandate())
    _work(SyntheticCounterProvider(idempotent=True))
    terminal = [row['envelope']['checkpoint'] for row in outbox.list_outbox(TENANT)
                if row['envelope'].get('checkpoint', {}).get('state') == 'SUCCEEDED']
    assert len(terminal) == 1
    assert terminal[0]['fencingToken'] == store.get_run(TENANT, run['run_id'])['lease_seq']
    assert terminal[0]['fencingToken'] == terminal[0]['lease']['fencingToken']


def test_http_settings_execution_controls_and_authority(db, monkeypatch):
    from fastapi.testclient import TestClient

    from openexecutive.api.main import create_app

    from .test_bo_execution import _fields
    monkeypatch.setenv('BACKEND_SHARED_SECRET', 'synthetic-service')
    monkeypatch.setenv('BACKEND_PROXY_SECRET', 'synthetic-proxy')
    monkeypatch.setenv('BO_TENANT_ID', TENANT)
    monkeypatch.setenv('BO_ADMIN_EMAILS', 'admin@example.invalid')
    client = TestClient(create_app())
    client.headers.update({'x-api-key': 'synthetic-service', 'x-caller-email': 'admin@example.invalid', 'x-caller-proxy-secret': 'synthetic-proxy'})
    for key, value in [('enabled', True), ('max_steps', 1)]:
        response = client.put('/bo/settings/bo.exec.' + key, json={'value': value, 'expected_version': 0})
        assert response.status_code == 200, response.text
    response = client.post('/bo/execution/mandates', json=_fields())
    assert response.status_code == 201, response.text
    mandate_id = response.json()['mandate']['mandate_id']
    payload = {'mandate_id': mandate_id, 'steps': [_step(), _step()], 'budget_amount': '1'}
    assert client.post('/bo/execution/runs', json=payload).status_code == 422
    payload['steps'] = [_step()]
    response = client.post('/bo/execution/runs', json=payload)
    assert response.status_code == 201, response.text
    run_id = response.json()['run']['run_id']
    path = '/bo/execution/runs/' + run_id
    assert client.post(path + '/pause').json()['run']['state'] == store.RUN_PAUSED
    assert client.get(path + '/authority').json()['authorized'] is False
    assert client.post(path + '/resume').json()['run']['state'] == store.RUN_PENDING
    response = client.put('/bo/settings/bo.exec.enabled', json={'value': False, 'expected_version': 1})
    assert response.status_code == 200
    assert client.get(path + '/authority').json()['authorized'] is False
    assert client.post(path + '/cancel', json={'reason': 'synthetic stop'}).json()['run']['state'] == store.RUN_CANCELLED
    detail = client.get(path).json()
    assert detail['reservation']['state'] == store.RES_RELEASED
    assert detail['ledger'] == []
