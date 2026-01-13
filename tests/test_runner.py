"""Tests for runner module."""

import pytest
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ksadk import Agent, Runner


@pytest.mark.asyncio
async def test_runner_creation():
    """Test runner creation."""
    agent = Agent(name="TestAgent")
    runner = Runner(agent=agent)

    assert runner.agent == agent


@pytest.mark.asyncio
async def test_runner_run():
    """Test runner run."""
    agent = Agent(name="TestAgent")
    runner = Runner(agent=agent)

    response = await runner.run({"message": "Hello"})

    assert response["success"] is True


def test_runner_run_sync():
    """Test runner sync run."""
    agent = Agent(name="TestAgent")
    runner = Runner(agent=agent)

    response = runner.run_sync({"message": "Hello"})

    assert response["success"] is True


@pytest.mark.asyncio
async def test_runner_faas_mode():
    """Test runner FaaS mode."""
    agent = Agent(name="TestAgent")
    runner = Runner(agent=agent)

    # Simulate FaaS event
    event = {
        "message": "Hello FaaS"
    }

    response = await runner.run_faas(event)

    assert response["statusCode"] == 200
    assert "body" in response


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
