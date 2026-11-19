"""Select and bind graph factories for checkpoint and tunnel support."""

from ._langgraph_checkpoint_binding import bind_native_checkpointer


class LangGraphFactoryMixin:
    def _sdk_mcp_agent(self, graph=None):
        graph = self._agent if graph is None else graph
        builder = getattr(graph, "builder", None)
        return vars(builder).get("_ksadk_mcp_agent") if hasattr(builder, "__dict__") else None

    def _graph_factory(self, *, module=None, agent=None):
        module = self._module if module is None else module
        agent = self._agent if agent is None else agent
        factory = getattr(module, "ksadk_graph_factory", None)
        tunnel_tools = self._configured_tunnel_tools()
        if tunnel_tools is not None:
            return self._tunnel_graph_factory(factory, agent, tunnel_tools)
        if callable(factory):
            return factory
        if self._sdk_mcp_agent(agent) is not None:
            return lambda *, checkpointer: bind_native_checkpointer(agent, checkpointer)
        return None

    def _configured_tunnel_tools(self):
        binding = getattr(self, "_tunnel_binding", None)
        if binding is None:
            return None
        from ksadk.tunnel.langgraph import TunnelTools

        if self._tunnel_tools is None:
            self._tunnel_tools = TunnelTools(binding.policy.agent_id, binding.registry)
        return self._tunnel_tools

    def _build_loaded_tunnel_graph(self, module, agent, managed_saver, local_saver):
        saver = managed_saver
        if saver is None:
            saver = getattr(agent, "checkpointer", None)
        if saver is None:
            saver = local_saver
        factory = self._graph_factory(module=module, agent=agent)
        return factory(checkpointer=saver)

    def _tunnel_graph_factory(self, factory, agent, tunnel_tools):
        """Require explicit integration for arbitrary compiled application graphs."""
        import inspect

        accepts_tunnel = False
        if callable(factory):
            params = inspect.signature(factory).parameters
            accepts_tunnel = (
                "tunnel_tools" in params
                and params["tunnel_tools"].kind != inspect.Parameter.POSITIONAL_ONLY
            )

        def build(*, checkpointer):
            if accepts_tunnel:
                graph = factory(checkpointer=checkpointer, tunnel_tools=tunnel_tools)
            elif callable(factory):
                graph = factory(checkpointer=checkpointer)
            else:
                graph = agent
            from langgraph.graph.state import CompiledStateGraph

            if not isinstance(graph, CompiledStateGraph):
                raise TypeError("MCP tunnel graph factory must return CompiledStateGraph")
            if self._sdk_mcp_agent(graph) is not None:
                from ksadk.mcp_runtime.oauth._agent import bind_tunnel_graph

                graph = bind_tunnel_graph(graph, tunnel_tools, checkpointer=checkpointer)
            elif not accepts_tunnel:
                raise TypeError(
                    "LangGraph MCP tunnel requires an SDK create_mcp_agent graph or "
                    "ksadk_graph_factory(*, checkpointer, tunnel_tools). Custom graphs "
                    "must explicitly bind tunnel_tools in their model and tool nodes."
                )
            return graph

        return build
