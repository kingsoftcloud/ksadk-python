"""Consume the locked Build's readonly Skill knowledge in each child's own Run.

ZCode's child profile explicitly adds Skill to its allowed tool surface. KsADK
keeps the DSH package source and authorization, never copying the parent's
disclosure cursor or making Skill text an executable permission grant.
"""

from ksadk.harness.native.adapters import ToolAuthorizer
from ksadk.harness.native.ports import RunConfiguration, ToolPort
from ksadk.harness.native.state import RunScope
from ksadk.harness.native.studio_admission import studio_mapping_digest
from ksadk.harness.native.subagents import ChildRunRequest
from ksadk.harness.skill_adapters.native_disclosure import (
    DEFINITIONS,
    NativeSkillDisclosurePort,
    install_native_skill_tools,
)
from ksadk.harness.stores.native_contract import RunStore
from ksadk.studio.native_skill_source import StudioNativeSkillSource


class StudioChildSkills:
    def __init__(
        self,
        *,
        scope: RunScope,
        store: RunStore,
        source: StudioNativeSkillSource,
        authorize: ToolAuthorizer,
        activation_binding_digest: str,
        metadata_policy: str | None = None,
    ) -> None:
        self.scope, self.store, self.source, self.authorize = scope, store, source, authorize
        self.refs = tuple(source.refs)
        self.metadata_policy = metadata_policy
        if not self.refs:
            raise ValueError("child_skill_bindings_empty")
        self.tool_names = tuple(definition.name for definition in DEFINITIONS)
        self.binding_digest = studio_mapping_digest(
            {
                "policy": "studio-child-skill-knowledge/v1",
                "activation_binding": activation_binding_digest,
                "refs": self.refs,
                "bundle_claims": source.binding_claims,
                **({"metadata_policy": metadata_policy} if metadata_policy is not None else {}),
            }
        )

    def bind(
        self,
        child: ChildRunRequest,
        configuration: RunConfiguration,
        tools: ToolPort,
    ) -> tuple[RunConfiguration, NativeSkillDisclosurePort]:
        if child.parent_scope != self.scope or child.scope != self.scope:
            raise ValueError("child_skill_binding_scope_mismatch")
        configuration = configuration.model_copy(
            update={
                "host_binding_digest": studio_mapping_digest(
                    {
                        "parent_binding": configuration.host_binding_digest,
                        "inherited_skill_binding": self.binding_digest,
                    }
                ),
            }
        )
        return install_native_skill_tools(
            configuration,
            tools,
            scope=child.scope,
            source=self.source,
            skill_refs=self.refs,
            authorize=self.authorize,
            store=self.store,
            metadata_policy=self.metadata_policy,
        )
