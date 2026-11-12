"""Hosted A2A registration must stay on the authenticated CLI path."""
import json
from click.testing import CliRunner
from ksadk.cli.cmd_a2a import a2a


def test_hosted_registration_uses_explicit_region_and_runtime_identity(monkeypatch):
    from ksadk.api.client import AgentEngineClient
    calls = []
    def init(self, **kwargs):
        calls.append(('init', kwargs))
        self._session = None
    card = {"name": "runtime", "supportedInterfaces": [{"protocolBinding": "HTTP+JSON"}]}
    async def action(self, name, params, **kwargs):
        calls.append((name, params, kwargs))
        if name == "GetAToARuntimeConfig":
            return {"AgentCard": card}
        return {'a2_a_agent_id': 'registration', 'status': 'registering', 'service_url': 'private'}
    monkeypatch.setattr(AgentEngineClient, '__init__', init)
    monkeypatch.setattr(AgentEngineClient, '_action_async', action)
    result = CliRunner().invoke(a2a, ['register-hosted', '--agent-id', 'ar-test',
                                    '--space-id', 'as-test', '--region', 'pre-online'])
    assert result.exit_code == 0, result.output
    assert calls == [('init', {'region': 'pre-online'}), ('GetAToARuntimeConfig', {'AgentId': 'ar-test'}, {'preserve_case': True}),
        ('CreateAToAAgent', {
        'Source': 'hosted', 'AgentId': 'ar-test', 'A2ASpaceIds': ['as-test'], 'AgentCard': card}, {})]
    assert json.loads(result.output) == {'a2a_agent_id': 'registration', 'status': 'registering'}
    assert 'private' not in result.output


def test_hosted_registration_requires_explicit_region():
    result = CliRunner().invoke(a2a, ['register-hosted', '--agent-id', 'ar-test', '--space-id', 'as-test'])
    assert result.exit_code == 2
    assert '--region' in result.output


def test_hosted_registration_does_not_print_remote_error_details(monkeypatch):
    from ksadk.api.client import AgentEngineClient, AgentEngineAPIError
    async def fail(*args, **kwargs):
        raise AgentEngineAPIError(409, 'private secret')
    monkeypatch.setattr(AgentEngineClient, '_action_async', fail)
    result = CliRunner().invoke(a2a, ['register-hosted', '--agent-id', 'ar-test',
                                    '--space-id', 'as-test', '--region', 'pre-online'])
    assert result.exit_code == 1
    assert '409' in result.output
    assert 'private secret' not in result.output


def test_hosted_registration_rejects_missing_runtime_card(monkeypatch):
    from ksadk.api.client import AgentEngineClient
    actions = []
    async def action(self, name, params, **kwargs):
        actions.append(name)
        return {'AgentCard': None}
    monkeypatch.setattr(AgentEngineClient, '_action_async', action)
    result = CliRunner().invoke(a2a, ['register-hosted', '--agent-id', 'ar-test',
                                    '--space-id', 'as-test', '--region', 'pre-online'])
    assert result.exit_code == 1
    assert actions == ['GetAToARuntimeConfig']
