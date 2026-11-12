"""Local ADK node; the Studio-generated entry supplies frozen remote experts."""
import json
import os

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm, LiteLLMClient


class ExpertModelClient(LiteLLMClient):
    """Ask the real model for exactly one expert call in a handoff turn."""

    async def acompletion(self, model, messages, tools, **kwargs):
        last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
        has_reply = any(m.get("role") == "tool" for m in messages[last_user + 1:])
        if last_user >= 0 and tools and not has_reply:
            name = tools[0]["function"]["name"]
            kwargs["tool_choice"] = {"type": "function", "function": {"name": name}}
            arguments = json.dumps({"message": messages[last_user]["content"]}, ensure_ascii=False)
            messages = [*messages, {"role": "system", "content": (
                "Call the expert with these exact arguments: " + arguments
                + ". Preserve the complete JSON document, not only its text field."
            )}]
        return await super().acompletion(model=model, messages=messages, tools=tools, **kwargs)

root_agent = Agent(
    name="r86_local",
    model=LiteLlm(model="openai/" + os.environ["OPENAI_MODEL_NAME"],
        api_base=os.environ["OPENAI_BASE_URL"], api_key=os.environ["OPENAI_API_KEY"],
        llm_client=ExpertModelClient()),
    instruction=("If an expert tool is available, call it exactly once with the complete user message "
        "verbatim, then return the tool reply verbatim. Otherwise return the user message verbatim. "
        "Do not answer for the expert or alter JSON."),
)
