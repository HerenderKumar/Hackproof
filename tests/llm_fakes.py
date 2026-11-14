"""Stand-ins for the model gateway: a scripted transport and chat-completions replies."""

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
