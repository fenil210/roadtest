"""A small client for the OpenAI Chat Completions API, used by the judge and the simulated user."""

from __future__ import annotations

import asyncio
import json
import os
import random
from typing import Any

import httpx

from .config import ModelRef, ModelsSpec
from .errors import ModelError

DEFAULT_BASE_URL = "https://api.openai.com/v1"
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
# A 429 with one of these codes is a billing problem; waiting will not help.
QUOTA_CODES = {
    "insufficient_quota",
    "credit_balance_exhausted",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
}
MAX_ATTEMPTS = 5


class LLM:
    def __init__(self, models: ModelsSpec, concurrency: int = 8):
        self.models = models
        self.base_url = (models.base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = os.environ.get(models.api_key_env, "")
        self.usage: dict[str, dict[str, int]] = {}
        self._limit = asyncio.Semaphore(concurrency)
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        """True when calls can be made: a key is set, or the endpoint is a custom one that may need none."""
        return bool(self.api_key) or self.base_url != DEFAULT_BASE_URL

    @property
    def missing_key_message(self) -> str:
        return (
            f"{self.models.api_key_env} is not set. Export it or add it to a .env file "
            "(it is used for the judge and the simulated user)."
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def text(self, model: ModelRef, messages: list[dict[str, str]]) -> str:
        return await self._chat(model, messages, None)

    async def json(self, model: ModelRef, messages: list[dict[str, str]], name: str, schema: dict[str, Any]) -> dict[str, Any]:
        """A reply that matches `schema`. Retries once when the model returns something else."""
        response_format = {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}
        problem = ""
        for _ in range(2):
            content = await self._chat(model, messages, response_format)
            try:
                value = json.loads(content)
            except json.JSONDecodeError:
                problem = f"`{model.model}` did not return JSON: {content[:200]}"
                continue
            if isinstance(value, dict) and all(key in value for key in schema.get("required", [])):
                return value
            problem = f"`{model.model}` returned JSON without the expected fields: {content[:200]}"
        raise ModelError(problem)

    async def check(self, model: ModelRef) -> None:
        """Raise ModelError unless the key works and the model exists."""
        response = await self._request("GET", f"/models/{model.model}", None, model.model)
        if response.status_code != 200:
            raise ModelError(self._describe(response, model.model))

    async def _chat(self, model: ModelRef, messages: list[dict[str, str]], response_format: dict[str, Any] | None) -> str:
        body: dict[str, Any] = {"model": model.model, "messages": messages, **model.params}
        if model.reasoning_effort:
            body["reasoning_effort"] = model.reasoning_effort
        if response_format:
            body["response_format"] = response_format
        async with self._limit:
            response = await self._request("POST", "/chat/completions", body, model.model)
        if response.status_code != 200:
            raise ModelError(self._describe(response, model.model))
        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelError(f"unexpected reply from {self.base_url}: {response.text[:200]}") from None
        self._count(model.model, data.get("usage") or {})
        if message.get("refusal"):
            raise ModelError(f"`{model.model}` refused: {message['refusal']}")
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not content:
            reason = choice.get("finish_reason")
            raise ModelError(f"`{model.model}` returned no text (finish_reason: {reason})")
        return content

    async def _request(self, method: str, path: str, body: dict[str, Any] | None, model: str) -> httpx.Response:
        if not self.configured:
            raise ModelError(self.missing_key_message)
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        last_error = ""
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self._client.request(method, self.base_url + path, headers=headers, json=body)
            except httpx.HTTPError as error:
                last_error = f"could not reach {self.base_url}: {type(error).__name__}"
                if attempt < MAX_ATTEMPTS - 1:  # no point waiting after the last try
                    await asyncio.sleep(_backoff(attempt))
                continue
            if response.status_code in RETRYABLE_STATUSES and _error_code(response) not in QUOTA_CODES:
                last_error = self._describe(response, model)
                if attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_backoff(attempt, response.headers.get("retry-after")))
                    continue
            return response
        raise ModelError(last_error or f"no reply from {self.base_url}")

    def _describe(self, response: httpx.Response, model: str) -> str:
        code = _error_code(response)
        try:
            message = response.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = response.text[:200]
        if response.status_code == 401:
            return f"the API key in {self.models.api_key_env} was rejected (HTTP 401). Check the key."
        if response.status_code == 404 or code == "model_not_found":
            return f"model `{model}` was not found for this key. Check `models:` in roadtest.yaml."
        if code in QUOTA_CODES:
            return f"the OpenAI account is out of quota ({code}). Check billing."
        return f"model call failed (HTTP {response.status_code}): {message}"

    def _count(self, model: str, usage: dict[str, Any]) -> None:
        totals = self.usage.setdefault(model, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        totals["calls"] += 1
        totals["input_tokens"] += int(usage.get("prompt_tokens") or 0)
        totals["output_tokens"] += int(usage.get("completion_tokens") or 0)


def _error_code(response: httpx.Response) -> str | None:
    try:
        error = response.json().get("error") or {}
    except (ValueError, AttributeError):
        return None
    if not isinstance(error, dict):
        return None
    return error.get("code") or error.get("type")


def _backoff(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 30.0) + random.uniform(0, 0.5)
        except ValueError:
            pass
    return min(1.0 * (2**attempt), 20.0) + random.uniform(0, 0.5)
