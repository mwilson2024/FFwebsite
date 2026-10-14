"""Small, privacy-bounded OpenAI Responses API client.

The browser never receives the API key. Callers can pass only user-entered chat;
MFL transport objects, league context, and cookies do not cross this boundary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

import requests


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_OPENAI_MODEL = "gpt-6-luna"
MAX_USER_MESSAGE_CHARS = 2_000
MAX_ASSISTANT_CHARS = 6_000
MAX_HISTORY_MESSAGES = 12


class OpenAIAssistantError(RuntimeError):
    """A safe, user-displayable OpenAI integration failure."""


@dataclass(frozen=True)
class AssistantTurn:
    role: str
    content: str


def normalize_model(value: str | None) -> str:
    """Accept an operator-controlled model identifier without accepting payloads."""
    model = (value or DEFAULT_OPENAI_MODEL).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", model):
        return DEFAULT_OPENAI_MODEL
    return model


def _output_text(payload: Mapping[str, object]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()[:MAX_ASSISTANT_CHARS]
    parts: list[str] = []
    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "output_text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts).strip()[:MAX_ASSISTANT_CHARS]


def ask_openai(
    *,
    api_key: str,
    model: str,
    instructions: str,
    user_message: str,
    history: Iterable[AssistantTurn] = (),
    session: requests.Session | None = None,
) -> str:
    """Send one bounded, non-streaming Responses API request.

    ``store=False`` prevents this application from opting into response storage.
    The explicit connect/read timeout prevents a slow provider from holding a web
    request indefinitely.
    """
    key = api_key.strip()
    message = user_message.strip()
    if not key:
        raise OpenAIAssistantError("The OpenAI assistant is not configured yet.")
    if not message:
        raise OpenAIAssistantError("Enter a question for the assistant.")
    if len(message) > MAX_USER_MESSAGE_CHARS:
        raise OpenAIAssistantError(
            f"Keep each question under {MAX_USER_MESSAGE_CHARS:,} characters."
        )

    input_messages: list[dict[str, str]] = []
    for turn in tuple(history)[-MAX_HISTORY_MESSAGES:]:
        role = turn.role if turn.role in {"user", "assistant"} else "user"
        content = turn.content.strip()[:MAX_ASSISTANT_CHARS]
        if content:
            input_messages.append({"role": role, "content": content})
    input_messages.append({"role": "user", "content": message})

    client = session or requests.Session()
    try:
        response = client.post(
            OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": normalize_model(model),
                "instructions": instructions,
                "input": input_messages,
                "max_output_tokens": 700,
                "store": False,
            },
            timeout=(5, 35),
        )
    except requests.RequestException:
        raise OpenAIAssistantError(
            "OpenAI could not be reached. Your question was not saved; try again shortly."
        ) from None

    if response.status_code == 429:
        raise OpenAIAssistantError(
            "OpenAI is rate-limited or the account needs billing/usage attention. Try again later."
        )
    if response.status_code in {401, 403}:
        raise OpenAIAssistantError(
            "OpenAI rejected the server key. Check OPENAI_API_KEY in the host settings."
        )
    if response.status_code >= 400:
        raise OpenAIAssistantError(
            f"OpenAI returned HTTP {response.status_code}. Try again shortly."
        )
    try:
        payload = response.json()
    except ValueError:
        raise OpenAIAssistantError("OpenAI returned an unreadable response.") from None
    if not isinstance(payload, Mapping):
        raise OpenAIAssistantError("OpenAI returned an unreadable response.")
    answer = _output_text(payload)
    if not answer:
        raise OpenAIAssistantError("OpenAI returned no answer. Try rephrasing the question.")
    return answer
