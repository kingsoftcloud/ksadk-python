"""Local interrupt recovery for graphs that did not request conversational checkpoints."""

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ksadk.runners._langgraph_fresh_turn import consume_fresh_root_read


class LocalInterruptSaver(AsyncSqliteSaver):
    # New business input is prepared from Session history. Resume, nested
    # graphs and checkpoint inspection still read the exact native state.
    fresh_checkpoint_view_on_read = True
    _ksadk_resume_only = True

    async def aget_tuple(self, config):
        if consume_fresh_root_read(config):
            return None
        return await super().aget_tuple(config)
