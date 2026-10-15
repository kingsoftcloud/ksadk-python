"""Prewarm immutable host wiring without claiming a future user turn."""

import logging

from ksadk.plugins.host import PluginHostError

logger = logging.getLogger(__name__)


async def prewarm_runtime_build(studio, build_id: str) -> None:
    try:
        spec = studio.resolve_run_spec(build_id)
        runtime = studio.run_service.plugin_runtime
        if runtime is None or spec.plugin_bundle_root is None:
            return
        # Session activation claims include selected model, reasoning and
        # approval settings. Only the actual Run may pin those claims.
        entry = await runtime._admission.resolve(lambda: runtime._host_for(spec.plugin_bundle_root))
        if entry.agent_id != spec.agent_id:
            raise PluginHostError("plugin_bundle_agent_mismatch", "prewarm Build Agent mismatch")
    except Exception as error:
        logger.info("runtime prewarm for Build %s skipped: %s", build_id, error)
