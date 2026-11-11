"""Non-serialized completion checkpoint carried to the native adapter only."""

from pydantic import PrivateAttr

from ksadk.events.canonical import RunCompleted


class NativeCheckpointCompleted(RunCompleted):
    _checkpoint_ref: dict[str, str] | None = PrivateAttr(default=None)
    _checkpoint_error: bool = PrivateAttr(default=False)
