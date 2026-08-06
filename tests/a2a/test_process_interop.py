"""A2A three-way interoperability across real HTTP and OS processes."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_SCRIPT = Path(__file__).with_name("process_server.py")
EXTERNAL_TOKEN = "test-external-token"


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class AgentProcess:
    name: str
    port: int
    process: subprocess.Popen[str]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def start_agent(tmp_path: Path, name: str, *, token: str = "") -> AgentProcess:
    port = unused_port()
    command = [
        sys.executable,
        str(SERVER_SCRIPT),
        "--port",
        str(port),
        "--name",
        name,
        "--database",
        str(tmp_path / f"{name}.db"),
    ]
    if token:
        command.extend(["--require-token", token])
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    agent = AgentProcess(name, port, process)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise AssertionError(f"{name} exited during startup: {output}")
        try:
            response = httpx.get(f"{agent.url}/.well-known/agent-card.json", timeout=0.3)
            if response.status_code == 200:
                return agent
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    process.terminate()
    raise AssertionError(f"timed out starting {name} on {port}")


@pytest.fixture
def agent_processes(tmp_path: Path):
    agents = [
        start_agent(tmp_path, "hosted-a"),
        start_agent(tmp_path, "hosted-b"),
        start_agent(tmp_path, "external", token=EXTERNAL_TOKEN),
    ]
    print("processes", [(agent.name, agent.process.pid, agent.port) for agent in agents])
    try:
        yield {agent.name: agent for agent in agents}
    finally:
        for agent in agents:
            agent.process.terminate()
        for agent in agents:
            try:
                agent.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                agent.process.kill()
                agent.process.wait(timeout=5)


def invoke(source: AgentProcess, target: AgentProcess, *, target_source: str, token: str = ""):
    response = httpx.post(
        f"{source.url}/test/invoke",
        json={
            "target_url": target.url,
            "target_id": target.name,
            "target_source": target_source,
            "credential": token,
            "message": f"{source.name}-to-{target.name}",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def assert_streamed(result: dict) -> None:
    assert result["task_id"]
    assert "run.started" in result["event_types"]
    assert "run.progress" in result["event_types"]
    assert "run.completed" in result["event_types"]
    assert "artifact.created" in result["event_types"]
    assert "text.completed" in result["event_types"]
    assert "echo:" in "".join(result["texts"])


def test_hosted_to_hosted_process_interop(agent_processes):
    result = invoke(
        agent_processes["hosted-a"],
        agent_processes["hosted-b"],
        target_source="hosted",
    )
    assert_streamed(result)
    assert result["source"] == "hosted-a" and result["target"] == "hosted-b"
    print("hosted->hosted", result["task_id"])


def test_hosted_to_external_uses_credential_store_and_cursor_resume(agent_processes):
    source = agent_processes["hosted-a"]
    before = httpx.get(f"{source.url}/test/events").json()["events"]
    result = invoke(
        source,
        agent_processes["external"],
        target_source="external",
        token=EXTERNAL_TOKEN,
    )
    assert_streamed(result)
    auth = httpx.get(f"{agent_processes['external'].url}/test/auth").json()
    assert f"Bearer {EXTERNAL_TOKEN}" in auth["authorization"]

    previous_cursor = before[-1]["seq_id"] if before else 0
    first = httpx.get(
        f"{source.url}/test/events", params={"after_seq_id": previous_cursor, "limit": 2}
    ).json()["events"]
    resumed = httpx.get(
        f"{source.url}/test/events", params={"after_seq_id": first[-1]["seq_id"]}
    ).json()["events"]
    seq_ids = [event["seq_id"] for event in first + resumed]
    assert seq_ids == sorted(set(seq_ids))
    assert {event["invocation_id"] for event in first + resumed} == {result["task_id"]}
    print("hosted->external", result["task_id"], seq_ids)


def test_external_to_hosted_process_interop(agent_processes):
    result = invoke(
        agent_processes["external"],
        agent_processes["hosted-b"],
        target_source="hosted",
    )
    assert_streamed(result)
    assert result["source"] == "external" and result["target"] == "hosted-b"
    print("external->hosted", result["task_id"])
