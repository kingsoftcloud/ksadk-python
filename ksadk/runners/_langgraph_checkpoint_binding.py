"""Bind SDK-owned storage to a copy of a native compiled graph."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)


def bind_native_checkpointer(graph: Any, saver: Any) -> Any:
    if not isinstance(graph, CompiledStateGraph):
        raise TypeError(
            "Automatic checkpoints require a native CompiledStateGraph or "
            "ksadk_graph_factory(*, checkpointer)"
        )
    return graph.copy(update={"checkpointer": saver})


class LocalCheckpointBindingMixin:
    async def _bind_local_checkpoint(self, *, backend: str, sqlite_saver_type=None) -> None:
        """Create local storage after the common selector has ruled out PostgreSQL."""
        factory = self._graph_factory()
        if not isinstance(self._agent, CompiledStateGraph) and not callable(factory):
            raise RuntimeError(
                "Local checkpoints require CompiledStateGraph or ksadk_graph_factory"
            )
        saver = getattr(self, "_sdk_local_saver", None)
        context = None
        try:
            if saver is None:
                if backend == "memory":
                    from langgraph.checkpoint.memory import InMemorySaver

                    saver = InMemorySaver()
                else:
                    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

                    path = (
                        Path(
                            os.getenv("KSADK_CHECKPOINT_PATH")
                            or (
                                Path(self.project_dir)
                                / ".agentengine"
                                / "ui"
                                / "checkpoints.sqlite"
                            )
                        )
                        .expanduser()
                        .resolve()
                    )
                    path.parent.mkdir(parents=True, exist_ok=True)
                    context = (sqlite_saver_type or AsyncSqliteSaver).from_conn_string(str(path))
                    saver = await context.__aenter__()
                    await saver.setup()
            graph = (
                factory(checkpointer=saver)
                if callable(factory)
                else bind_native_checkpointer(self._agent, saver)
            )
            if not isinstance(graph, CompiledStateGraph):
                raise TypeError("Local checkpoint factory must return CompiledStateGraph")
            self._agent = graph
            self._sdk_checkpoint_bound = True
            self._sdk_local_saver = saver
            if context is not None:
                self._sdk_local_checkpoint_context = context
            if backend == "memory":
                logger.info("LangGraph auto checkpoint backend=memory reason=explicit_backend")
            else:
                logger.info(
                    "LangGraph auto checkpoint backend=sqlite reason=no_postgres_config "
                    "path=%s shared_across_pods=false", path,
                )
        except BaseException:
            if context is not None:
                await context.__aexit__(None, None, None)
            raise

    async def _close_local_checkpoint(self) -> None:
        context = getattr(self, "_sdk_local_checkpoint_context", None)
        self._sdk_local_checkpoint_context = None
        self._sdk_local_saver = None
        if context is not None:
            await context.__aexit__(None, None, None)
