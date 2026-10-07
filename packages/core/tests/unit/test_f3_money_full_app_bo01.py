"""BO01 F3 regression candidates for the normal A01 unit suite.

Real complete app + loopback HTTP via already collected owner fixture.
Synthetic provider boundary only; no auth/router/domain overrides.
The invalid-money cases are intentionally RED until the owner fixes backend.
"""
import pytest
from tests.unit.test_bughunt_p0_mutators_full_app import full_app  # noqa: F401


@pytest.mark.parametrize('value,currency', [('1.005','EUR'),('6000.25','ZZZ')])
def test_f3_arr_invalid_domain_refused_without_write(full_app, value, currency):
    client, headers, snapshot, *_ = full_app
    before=client.get('/company-profile',headers=headers('admin')).json()
    durable=snapshot()['profile']
    reply=client.patch('/company-profile',headers=headers('admin'),json={
        'expected_version':before['version'],
        'annual_revenue_arr':value,'annual_revenue_arr_currency':currency})
    assert reply.status_code==422, reply.text
    assert client.get('/company-profile',headers=headers('admin')).json()==before
    assert snapshot()['profile']==durable


@pytest.mark.parametrize('currency', ['RON','EUR','USD','GBP'])
def test_f3_arr_string_exact_api_yaml_readback(full_app, currency):
    client, headers, snapshot, *_ = full_app
    from openexecutive.memory.company_profile import CompanyProfile
    for amount in ['0','-6000.25','12345678901234567890.12']:
        before=client.get('/company-profile',headers=headers('admin')).json()
        result=client.patch('/company-profile',headers=headers('admin'),json={
            'expected_version':before['version'],
            'annual_revenue_arr':amount,'annual_revenue_arr_currency':currency})
        assert result.status_code==200, result.text
        read=client.get('/company-profile',headers=headers('admin')).json()
        assert isinstance(read['annual_revenue_arr'], str)
        from decimal import Decimal
        assert Decimal(read['annual_revenue_arr'])==Decimal(amount)
        assert read['annual_revenue_arr_currency']==currency
        path=full_app[-1]/'profile.yaml'
        loaded=CompanyProfile.load_from_yaml(path)
        assert loaded.annual_revenue_arr==Decimal(amount)
        assert loaded.annual_revenue_arr_currency==currency


def test_f3_arr_viewer_refused_durable_state_intact(full_app):
    client, headers, snapshot, *_ = full_app
    before=client.get('/company-profile',headers=headers('admin')).json()
    durable=snapshot()['profile']
    result=client.patch('/company-profile',headers=headers('viewer'),json={
        'expected_version':before['version'],
        'annual_revenue_arr':'6000.25','annual_revenue_arr_currency':'USD'})
    assert result.status_code==403, result.text
    assert client.get('/company-profile',headers=headers('admin')).json()==before
    assert snapshot()['profile']==durable
