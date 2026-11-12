"""The historical token-provider import path must retain public type identity."""

import pickle
import subprocess
import sys

import pytest


def test_workload_provider_public_import_identity(tmp_path):
    import ksadk.a2a as public
    from ksadk.a2a import control_plane, workload_tokens

    assert public.WorkloadTokenProvider is workload_tokens.WorkloadTokenProvider
    assert public.FileWorkloadTokenProvider is workload_tokens.FileWorkloadTokenProvider
    assert issubclass(public.FileWorkloadTokenProvider, workload_tokens.WorkloadTokenProvider)
    assert isinstance(public.FileWorkloadTokenProvider(tmp_path), workload_tokens.WorkloadTokenProvider)
    for name in ("ENV_A2A_TOKEN_DIR", "DEFAULT_A2A_TOKEN_DIR", "MAX_WORKLOAD_TOKEN_BYTES"):
        assert getattr(workload_tokens, name) == getattr(control_plane, name)


def test_historical_workload_provider_pickle_global_resolves_canonical_type():
    from ksadk.a2a import FileWorkloadTokenProvider

    historical_global = b"cksadk.a2a.workload_tokens\nFileWorkloadTokenProvider\n."
    assert pickle.loads(historical_global) is FileWorkloadTokenProvider


@pytest.mark.parametrize("first", ["control_plane", "workload_tokens"])
def test_workload_provider_import_order_keeps_identity(first):
    code = (f"import ksadk.a2a.{first}\n"
            "from ksadk.a2a import control_plane, workload_tokens\n"
            "assert control_plane.WorkloadTokenProvider is workload_tokens.WorkloadTokenProvider\n"
            "assert control_plane.FileWorkloadTokenProvider is workload_tokens.FileWorkloadTokenProvider\n")
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)
