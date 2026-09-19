"""LLM Gateway with multi-model fallback and structured output retry.

Wraps LiteLLM and provides:
  - Automatic fallback to secondary models on failure
  - Structured JSON output with validation + correction retry
  - Token budget tracking
  - Observability hooks (trace IDs, latency)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from litellm import completion
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when all fallback models fail."""


class LLMGateway:
    """Unified LLM entry point.

    Usage:
        gateway = LLMGateway(settings)
        text = gateway.chat(messages)
        obj = gateway.chat_structured(messages, response_model=MyModel)
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self.model = settings.model
        self.fallbacks = settings.fallback_model_list
        self._candidate_models: List[str] = [self.model, *self.fallbacks]

    # ── public API ──────────────────────────────────────────────────────────

    def chat(self, messages: List[Dict[str, Any]]) -> str:
        """Plain text chat with fallback."""
        content, _ = self._call_with_fallback(messages, tools=None)
        return content

    def chat_structured(
        self,
        messages: List[Dict[str, Any]],
        response_model: type[BaseModel],
        max_retries: int = 3,
    ) -> BaseModel:
        """Chat that returns a validated Pydantic object.

        If the model returns invalid JSON, we feed the error back and
        retry up to `max_retries` times.
        """
        system_instruction = (
            "Respond with a single JSON object matching this schema:\n"
            f"{response_model.model_json_schema()}\n"
            "Do not include any text outside the JSON object."
        )
        work_messages = list(messages)
        # Insert schema instruction as a system message at the top
        if work_messages and work_messages[0].get("role") == "system":
            work_messages[0] = {
                "role": "system",
                "content": work_messages[0]["content"] + "\n\n" + system_instruction,
            }
        else:
            work_messages.insert(0, {"role": "system", "content": system_instruction})

        last_error: Optional[str] = None
        for attempt in range(max_retries):
            try:
                content, _ = self._call_with_fallback(work_messages, tools=None)
                parsed = self._parse_json(content)
                return response_model.model_validate(parsed)
            except (json.JSONDecodeError, ValidationError) as e:
                last_error = str(e)
                logger.warning(
                    "Structured output validation failed (attempt %d/%d): %s",
                    attempt + 1,
                    max_retries,
                    last_error[:200],
                )
                work_messages.append({"role": "assistant", "content": content})
                work_messages.append({
                    "role": "user",
                    "content": (
                        "Your previous response was not valid JSON matching the schema. "
                        f"Error: {last_error}\n"
                        "Please respond with ONLY a valid JSON object, no markdown, no extra text."
                    ),
                })
                continue

        raise LLMError(
            f"Failed to get valid structured output after {max_retries} retries. "
            f"Last error: {last_error}"
        )

    # ── internals ───────────────────────────────────────────────────────────

    def _call_with_fallback(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
    ) -> Tuple[str, Optional[Any]]:
        errors: list[str] = []
        for model in self._candidate_models:
            try:
                return self._call_once(model, messages, tools)
            except Exception as e:
                msg = f"{model}: {e}"
                errors.append(msg)
                logger.warning("LLM call failed, trying fallback: %s", msg[:200])
                continue
        raise LLMError("All models failed: " + " | ".join(errors))

    def _call_once(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
    ) -> Tuple[str, Optional[Any]]:
        effective_model = model
        if self.settings.api_base and "/" not in model:
            effective_model = f"openai/{model}"
        kwargs: Dict[str, Any] = {
            "model": effective_model,
            "messages": messages,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
        }
        if self.settings.api_key:
            kwargs["api_key"] = self.settings.api_key
        if self.settings.api_base:
            kwargs["api_base"] = self.settings.api_base
        if tools:
            kwargs["tools"] = tools

        start = time.time()
        response = completion(**kwargs)
        latency_ms = (time.time() - start) * 1000

        message = response.choices[0].message
        content = message.content or ""
        tool_calls = getattr(message, "tool_calls", None)

        logger.debug(
            "LLM call model=%s latency_ms=%.0f tokens=%s",
            model,
            latency_ms,
            getattr(response, "usage", None),
        )
        return content, tool_calls

    @staticmethod
    def _parse_json(content: str) -> Any:
        text = content.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        return json.loads(text)