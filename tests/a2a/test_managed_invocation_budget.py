import pytest
from ksadk.a2a.timeouts import invocation_seconds

@pytest.mark.parametrize('value,expected',[('',600),('450',450),('0',600),('nan',600),('inf',600),('invalid',600)])
def test_managed_whole_budget_is_configurable_positive_finite(monkeypatch,value,expected):
    monkeypatch.setenv('KSADK_A2A_INVOCATION_SECONDS',value)
    assert invocation_seconds()==expected
    assert invocation_seconds(25)==25
