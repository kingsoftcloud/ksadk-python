from types import SimpleNamespace

import pytest

from ksadk.plugins.providers.codex_native import create_codex_adapter
from ksadk.runtime.launch import RuntimeLaunchContext, RuntimeServices
from tests.a2a.test_invocation_service_r85 import request


def context(tmp_path, specs, bootstrap):
    calls = []
    class Client:
        def configure_a2a_experts(self, values):
            self.specs = values
    def factory(**kwargs):
        calls.append(kwargs)
        return Client()
    return RuntimeLaunchContext(runtime_type='codex', project_dir=tmp_path,
        config={'a2a_experts': specs}, services=RuntimeServices(
            codex_client_factory=factory, managed_a2a_bootstrap=bootstrap)), calls


def test_factory_assembles_fixed_experts_from_launch_config(tmp_path):
    bootstrap = SimpleNamespace(outbound_enabled=True)
    ctx, calls = context(tmp_path, [request().spec.model_dump()], bootstrap)
    adapter = create_codex_adapter(ctx)
    assert len(calls) == 1
    assert adapter._client.specs == (request().spec,)
    assert adapter._managed_a2a_bootstrap is bootstrap
    assert 'managed_a2a_bootstrap' not in ctx.config


@pytest.mark.parametrize('fault', ['unversioned', 'native', 'duplicate', 'endpoint', 'credential', 'missing_bootstrap', 'disabled'])
def test_bad_exposure_rejected_before_native_client_created(tmp_path, fault):
    spec = request().spec.model_dump()
    specs = [spec]
    bootstrap = SimpleNamespace(outbound_enabled=True)
    if fault == 'unversioned':
        spec['expected_version_id'] = None
    elif fault == 'native':
        spec['exposure'] = 'native'
    elif fault == 'duplicate':
        specs.append(dict(spec))
    elif fault in {'endpoint', 'credential'}:
        spec[fault] = 'forbidden'
    elif fault == 'missing_bootstrap':
        bootstrap = None
    else:
        bootstrap.outbound_enabled = False
    ctx, calls = context(tmp_path, specs, bootstrap)
    with pytest.raises(ValueError):
        create_codex_adapter(ctx)
    assert not calls
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_provider_stage_and_activation_keep_bootstrap_out_of_config(tmp_path):
    from ksadk.plugins.providers.codex import CodexAgentProviderFactory, CodexAgentActivation
    from tests.plugins.test_codex_provider_vertical import _provider_manifest, _profile
    bootstrap = SimpleNamespace(outbound_enabled=True)
    runtime = await CodexAgentProviderFactory().stage(_provider_manifest(), profile=_profile(),
        services={'managed_a2a_bootstrap': bootstrap})
    assert runtime._managed_a2a_bootstrap is bootstrap
    activation = CodexAgentActivation(bundle=None,
        config=SimpleNamespace(project_dir=tmp_path, launch_config={}),
        session_service=runtime._session_service, codex_client_factory=None,
        mcp_projection=None, managed_a2a_bootstrap=runtime._managed_a2a_bootstrap)
    assert activation._launch_context.services.managed_a2a_bootstrap is bootstrap
    assert not activation._launch_context.config


def frozen_binding():
    from ksadk.studio.contracts import RemoteAgentBinding
    spec = request().spec
    return RemoteAgentBinding.freeze(local_name=spec.local_name, space_id=spec.space_id,
        target_agent_id=spec.target_agent_id, expected_version_id=spec.expected_version_id,
        policy_ref='policy://a2a/runtime')


def test_bundle_binding_projects_fixed_target_and_validates_digest():
    from ksadk.plugins.providers.codex import _bundle_a2a_experts
    binding = frozen_binding()
    payload = binding.model_dump(mode='json', by_alias=True)
    projected = _bundle_a2a_experts({'remoteAgents': [payload]})
    assert projected[0]['binding_id'] == binding.binding_id
    assert projected[0]['expected_version_id'] == binding.expected_version_id
    assert projected[0]["policy_ref"] == binding.policy_ref
    assert set(projected[0]) <= set(request().spec.model_dump()) | {"policy_ref"}
    assert _bundle_a2a_experts({'remoteAgents': (payload,)}) == projected
    payload['localName'] = 'tampered'
    with pytest.raises(ValueError, match='DIGEST_MISMATCH'):
        _bundle_a2a_experts({'remoteAgents': [payload]})


@pytest.mark.parametrize('kind', ['replace', 'remove', 'inject'])
def test_local_launch_cannot_override_bundle_experts_before_credentials_or_io(tmp_path, kind):
    from ksadk.plugins.host import PluginHostError
    from ksadk.plugins.providers.codex import _resolve_bundle_config
    binding = frozen_binding().model_dump(mode='json', by_alias=True)
    bundle = SimpleNamespace(resolved_agent_spec={'remoteAgents': [] if kind == 'inject' else [binding]})
    local = RuntimeLaunchContext(runtime_type='codex', project_dir=tmp_path,
        config={'a2a_experts': [] if kind == 'remove' else [request().spec.model_dump()]})
    with pytest.raises(PluginHostError, match='immutable Bundle'):
        _resolve_bundle_config(bundle, plugin_id='test', credential_resolver=None,
            runtime_state_root=None, local_launch=local)
    assert not list(tmp_path.iterdir())


def test_frozen_bundle_binding_reaches_official_factory(tmp_path):
    from types import MappingProxyType
    from ksadk.plugins.providers.codex import _resolve_bundle_config
    binding = frozen_binding()
    bundle = SimpleNamespace(root=tmp_path, manifest=SimpleNamespace(files=[]),
        resolved_agent_spec=MappingProxyType({
            'model': {'model': 'fixture'}, 'instructions': {'system': 'Ask the expert'},
            'capabilities': {},
            'remoteAgents': (MappingProxyType(binding.model_dump(mode='json', by_alias=True)),),
        }))
    local = RuntimeLaunchContext(runtime_type='codex', project_dir=tmp_path,
        config={'model': 'fixture', 'models': ['fixture']})
    resolved = _resolve_bundle_config(bundle, plugin_id='test', credential_resolver=None,
        runtime_state_root=None, local_launch=local)
    bootstrap = SimpleNamespace(outbound_enabled=True)
    ctx, calls = context(tmp_path, resolved.launch_config['a2a_experts'], bootstrap)
    adapter = create_codex_adapter(ctx)
    assert calls
    assert adapter._a2a_experts[0].binding_id == binding.binding_id
    assert adapter._a2a_experts[0].expected_version_id == binding.expected_version_id


def test_executor_injects_platform_bootstrap_before_factory_validation(tmp_path):
    from ksadk.runtime.executor import RuntimeExecutor
    from ksadk.plugins.providers.codex_native import codex_runtime_registry
    bootstrap = SimpleNamespace(outbound_enabled=True)
    ctx, calls = context(tmp_path, [request().spec.model_dump()], None)
    executor = RuntimeExecutor(codex_runtime_registry())
    executor.configure_managed_a2a(bootstrap)
    adapter = executor.create_adapter(ctx)
    assert calls
    assert adapter._managed_a2a_bootstrap is bootstrap
    assert ctx.services.managed_a2a_bootstrap is None
    assert 'managed_a2a_bootstrap' not in ctx.config
    adapter.configure_managed_a2a(bootstrap)
    with pytest.raises(ValueError, match='IDENTITY_CONFLICT'):
        adapter.configure_managed_a2a(SimpleNamespace(outbound_enabled=True))


def test_executor_rejects_conflicting_bootstrap_before_client_creation(tmp_path):
    from ksadk.runtime.executor import RuntimeExecutor
    from ksadk.plugins.providers.codex_native import codex_runtime_registry
    ctx, calls = context(tmp_path, [request().spec.model_dump()], SimpleNamespace(outbound_enabled=True))
    executor = RuntimeExecutor(codex_runtime_registry())
    executor.configure_managed_a2a(SimpleNamespace(outbound_enabled=True))
    with pytest.raises(ValueError, match='IDENTITY_CONFLICT'):
        executor.create_adapter(ctx)
    assert not calls


def test_fixed_expert_capabilities_do_not_advertise_rejected_goal_mode(tmp_path):
    bootstrap = SimpleNamespace(outbound_enabled=True)
    expert_ctx, _ = context(tmp_path, [request().spec.model_dump()], bootstrap)
    expert = create_codex_adapter(expert_ctx)
    assert expert.capabilities().goal.supported is False
    assert expert.capabilities().goal.reason == "a2a_codex_expert_goal_unsupported"
    plain_ctx, _ = context(tmp_path, [], None)
    plain = create_codex_adapter(plain_ctx)
    assert plain.capabilities().goal.supported is True
    assert plain.capabilities().goal.mode == "native"


def test_bundle_rejects_unknown_policy_before_runtime_creation():
    from ksadk.plugins.providers.codex import _bundle_a2a_experts
    from ksadk.studio.contracts import RemoteAgentBinding
    original = frozen_binding()
    values = original.model_dump(exclude={"binding_id", "digest"})
    values["policy_ref"] = "policy://missing-policy"
    invalid = RemoteAgentBinding.freeze(**values)
    with pytest.raises(ValueError, match="A2A_POLICY_REF_UNSUPPORTED"):
        _bundle_a2a_experts({"remoteAgents": [invalid.model_dump()]})
