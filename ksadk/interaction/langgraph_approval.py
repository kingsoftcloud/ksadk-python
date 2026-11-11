"""The declared LangChain HITL contract, separate from arbitrary interrupt values."""

from collections.abc import Mapping
from copy import deepcopy


def approval_contract(value):
    if not isinstance(value, Mapping):
        return None
    actions, reviews = value.get("action_requests"), value.get("review_configs")
    if not isinstance(actions, list) or not actions or not isinstance(reviews, list):
        return None
    if len(actions) != len(reviews) or any(
        not isinstance(action, Mapping) or not isinstance(review, Mapping)
        or action.get("name") != review.get("action_name")
        or not isinstance(review.get("allowed_decisions"), list)
        for action, review in zip(actions, reviews, strict=True)
    ):
        return None
    return {
        "kind": "langchain_hitl",
        "allowed_decisions": [list(review["allowed_decisions"]) for review in reviews],
        "actions": [action["name"] for action in actions],
        "args_schemas": [deepcopy(review.get("args_schema")) for review in reviews],
    }


def approval_response(contract, action, response):
    """Map a whole-request decision to every declared review; preserve native detail."""
    if not contract or contract.get("kind") != "langchain_hitl":
        if response is None or response == {}:
            if action not in {"approve", "reject"}:
                raise ValueError("LangGraph approval requires approve or reject")
            return {"decisions": [{"type": action}]}
        return response
    allowed = contract["allowed_decisions"]
    if response is None or response == {} or response == {"decision": action}:
        response = {"decisions": [{"type": action} for _ in allowed]}
    decisions = response.get("decisions") if isinstance(response, dict) else None
    if not isinstance(decisions, list) or len(decisions) != len(allowed):
        raise ValueError("LangChain HITL requires one decision per requested action")
    for index, (decision, choices) in enumerate(zip(decisions, allowed, strict=True)):
        if not isinstance(decision, dict) or decision.get("type") not in choices:
            raise ValueError("Decision is not allowed by the stored LangChain HITL request")
        if action in {"approve", "reject"} and decision.get("type") != action:
            raise ValueError("Approval action conflicts with its native decision")
        if decision["type"] == "edit":
            edited = decision.get("edited_action")
            if (not isinstance(edited, dict) or not isinstance(edited.get("name"), str)
                    or not isinstance(edited.get("args"), dict)):
                raise ValueError("Edited action requires a tool name and arguments")
            schema = contract["args_schemas"][index]
            if schema is not None:
                if edited["name"] != contract["actions"][index]:
                    raise ValueError("Edited tool has no declared argument schema")
                from ksadk.interaction.schema import validate_answer

                validate_answer(schema, edited["args"])
    return response
