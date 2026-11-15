"""Stand-ins for the model gateway: a scripted transport and chat-completions replies."""

import copy
import json
from typing import Any


class Scripted:
    """A transport that answers from a list and keeps every request body it was sent."""

    def __init__(self, *replies: dict[str, Any] | Exception) -> None:
        self.replies = list(replies)
        self.bodies: list[dict[str, Any]] = []

    async def __call__(self, body: dict[str, Any]) -> dict[str, Any]:
        self.bodies.append(body)
        answer = self.replies.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def user_messages(self) -> list[str]:
        return [body["messages"][1]["content"] for body in self.bodies]


def tool_reply(name: str, arguments: Any, finish: str = "tool_calls") -> dict[str, Any]:
    """A reply calling `name`; `arguments` that are not text are sent as their JSON."""
    text = arguments if isinstance(arguments, str) else json.dumps(arguments)
    call = {"id": "call_1", "type": "function", "function": {"name": name, "arguments": text}}
    message = {"role": "assistant", "content": None, "tool_calls": [call]}
    return {
        "id": "gen-1",
        "model": "claude-sonnet-5-5",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 390, "completion_tokens": 50, "cost": 0.14},
    }


def draft_entry(document: dict[str, Any], claim_id: str = "c1") -> dict[str, Any]:
    """A `submit_tests` entry: an example spec as the model would send it for `claim_id`."""
    keys = ("claim_type", "steps", "practice_cases", "final_cases")
    return {
        "claim_id": claim_id,
        "status": "draft",
        **copy.deepcopy({k: document[k] for k in keys}),
    }


def unclear_entry(reason: str, claim_id: str = "c1") -> dict[str, Any]:
    return {"claim_id": claim_id, "claim_type": "calculation", "status": "needs_clarification",
            "reason": reason}  # fmt: skip


def draft_answer(*tests: dict[str, Any], finish: str = "tool_calls") -> dict[str, Any]:
    """A reply calling `submit_tests` with these entries."""
    return tool_reply("submit_tests", {"tests": list(tests)}, finish)


def calls_reply(*calls: tuple[str, Any], finish: str = "tool_calls") -> dict[str, Any]:
    """A reply making several tool calls, given as `(name, arguments)`; ids are `call_1`, …"""
    entries = [
        {
            "id": f"call_{n}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
            },
        }
        for n, (name, arguments) in enumerate(calls, start=1)
    ]
    message = {"role": "assistant", "content": None, "tool_calls": entries}
    return {
        "id": "gen-1",
        "model": "claude-sonnet-5-5",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "cost": 0.3},
    }


def text_reply(text: str, finish: str = "stop") -> dict[str, Any]:
    """A reply with words and no tool call."""
    return {
        "id": "gen-1",
        "model": "claude-sonnet-5-5",
        "choices": [
            {"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": text}}
        ],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 20, "cost": 0.2},
    }
