"""Host-owned dispatch revocation, independent of cooperative Task cancellation.

This process-local authority is inherited by an effect's parallel members. It
is not a permission grant, serialized state, or a substitute for Store fencing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(slots=True)
class EffectAdmission:
    deadline: float
    clock: Callable[[], float]
    revocation_requested: Callable[[], bool]
    parent: EffectAdmission | None = None
    _revoked: bool = field(default=False, init=False)

    def revoke(self) -> None:
        self._revoked = True

    def check(self) -> None:
        if self._revoked or self.clock() >= self.deadline:
            self.revoke()
            raise asyncio.CancelledError
        if self.parent is not None:
            self.parent.check()
        if self.revocation_requested():
            self.revoke()
            raise asyncio.CancelledError


_CURRENT: ContextVar[EffectAdmission | None] = ContextVar("native_effect_admission", default=None)


@contextmanager
def effect_admission(
    *, deadline: float, clock: Callable[[], float], revocation_requested: Callable[[], bool],
) -> Iterator[EffectAdmission]:
    """Bind only while creating the owned Task; its copied context retains it."""
    admission = EffectAdmission(
        deadline=deadline, clock=clock, revocation_requested=revocation_requested,
        parent=_CURRENT.get(),
    )
    token = _CURRENT.set(admission)
    try:
        yield admission
    finally:
        _CURRENT.reset(token)


def check_effect_admission() -> None:
    """Recheck immediately before a host adapter starts another operation.

Outside the Native effect driver there is no ambient admission or deadline;
direct Port callers retain responsibility for their own execution lifecycle.
"""
    admission = _CURRENT.get()
    if admission is not None:
        admission.check()


@contextmanager
def owned_child_admission() -> Iterator[None]:
    """Transfer lifetime to the child owner, preserving all other context.

    A launch must still be admitted. Once published, the owner's cancellation
    and the child's Kernel control its effects; completion of the launching
    tool is not child cancellation. Ordinary nested/parallel effects continue
    inheriting their parent admission through ``effect_admission``.
    """
    check_effect_admission()
    token = _CURRENT.set(None)
    try:
        yield
    finally:
        _CURRENT.reset(token)
