"""Scoped async stream cleanup without changing its primary result or error.

Optional ``aclose`` and close-only async contexts are handled here. This helper does
not promise resource release after a failure, retry cleanup, detach a task,
or suppress cancellation. External resources remain their Port owner's duty.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TypeVar

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")


def _cleanup_warning(owner: str) -> None:
    _LOGGER.warning(
        "Async stream cleanup failed; resource release unconfirmed (%s)", owner,
        extra={"event": "async_stream.cleanup_failed", "owner": owner},
    )


async def close_async_iterator(iterator: object, *, owner: str) -> None:
    """Join a declared close operation, preserving the enclosing operation.

    An AsyncIterator need not have ``aclose``. A close error must not erase a
    sealed model response, replace a protocol rejection or turn cancellation
    into an unrelated provider failure. The diagnostic deliberately contains
    no exception message, traceback, provider data or credentials.
    """
    try:
        close = getattr(iterator, "aclose", None)
        if callable(close):
            await close()
    except Exception:
        _cleanup_warning(owner)


@asynccontextmanager
async def owning_async_iterator(
    iterator: AsyncIterator[_T], *, owner: str
) -> AsyncIterator[AsyncIterator[_T]]:
    try:
        yield iterator
    finally:
        await close_async_iterator(iterator, owner=owner)


async def _exit_context(
    context: AbstractAsyncContextManager[_T], error: BaseException | None, *, owner: str
) -> None:
    try:
        await context.__aexit__(
            type(error) if error is not None else None,
            error,
            error.__traceback__ if error is not None else None,
        )
    except Exception:
        _cleanup_warning(owner)


@asynccontextmanager
async def owning_async_context(
    context: AbstractAsyncContextManager[_T], *, owner: str
) -> AsyncIterator[_T]:
    """Own a close-only response context; body/entry failures stay authoritative.

    Used for HTTPX streaming responses, whose exit only closes the response.
    This is not for transaction contexts or contexts that suppress body errors.
    """
    value = await context.__aenter__()
    try:
        yield value
    except BaseException as error:
        await _exit_context(context, error, owner=owner)
        raise
    else:
        await _exit_context(context, None, owner=owner)
