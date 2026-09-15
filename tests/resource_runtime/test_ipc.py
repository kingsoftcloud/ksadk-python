import io
import struct

import pytest
from pydantic import ValidationError

from ksadk.resource_runtime.ipc import (
    MAX_FRAME_BYTES,
    FrameError,
    ResourceRequest,
    decode_body,
    encode_frame,
    read_frame,
    write_frame,
)


class FragmentedStream(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 3))

    def write(self, value):
        return super().write(value[:3])


def test_fragmented_frames_round_trip_and_clean_eof():
    stream = FragmentedStream()
    payload = {"query": "中文资料", "args": {"topK": 3}}
    write_frame(stream, payload)
    write_frame(stream, {"next": True})
    stream.seek(0)
    assert read_frame(stream) == payload
    assert read_frame(stream) == {"next": True}
    assert read_frame(stream) is None


@pytest.mark.parametrize(
    "body",
    [
        b'{"v":1,"v":2}',
        b'{"nested":{"a":1,"a":2}}',
        b'{"n":NaN}',
        b'{"n":Infinity}',
        b'{"n":1e400}',
        b"[]",
        b"\xff",
    ],
)
def test_ambiguous_or_invalid_json_rejected(body):
    with pytest.raises(FrameError):
        decode_body(body)


@pytest.mark.parametrize(
    "wire",
    [
        b"\x00",
        struct.pack("!I", 10) + b"{}",
        struct.pack("!I", 0),
        struct.pack("!I", MAX_FRAME_BYTES + 1),
    ],
)
def test_truncated_and_oversized_frames_rejected(wire):
    with pytest.raises(FrameError):
        read_frame(io.BytesIO(wire))


def test_oversize_rejected_before_reading_body():
    class Stream:
        def read(self, size):
            assert size == 4
            return struct.pack("!I", MAX_FRAME_BYTES + 1)

    with pytest.raises(FrameError):
        read_frame(Stream())


def test_oversized_output_rejected():
    with pytest.raises(FrameError):
        encode_frame({"content": "x" * MAX_FRAME_BYTES})


@pytest.mark.parametrize(
    "change",
    [{"operation": "eval"}, {"v": True}, {"v": 2}, {"deadline": 1.5}, {"userId": "forged"}],
)
def test_unknown_operations_and_untrusted_identity_rejected(change):
    request = {
        "v": 1,
        "requestId": "request-a",
        "deadline": 1000,
        "operation": "load_memory",
        "handle": "h" * 32,
        "arguments": {},
    }
    with pytest.raises(ValidationError):
        ResourceRequest.model_validate({**request, **change})
