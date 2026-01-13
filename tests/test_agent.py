"""Tests for agent module."""

import pytest
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ksadk import Agent


@pytest.mark.asyncio
async def test_agent_creation():
    """Test agent creation."""
    agent = Agent(name="TestAgent", description="Test agent")

    assert agent.name == "TestAgent"
    assert agent.description == "Test agent"
    assert len(agent.tools) == 0


@pytest.mark.asyncio
async def test_agent_run():
    """Test agent run."""
    agent = Agent(name="TestAgent")

    response = await agent.run({"message": "Hello"})

    assert response["success"] is True
    assert "response" in response
    assert response["agent"] == "TestAgent"


@pytest.mark.asyncio
async def test_agent_run_without_message():
    """Test agent run without message."""
    agent = Agent(name="TestAgent")

    response = await agent.run({})

    assert response["success"] is False
    assert "error" in response


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
