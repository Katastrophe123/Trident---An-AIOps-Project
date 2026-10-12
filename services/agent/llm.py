"""The LLM client: one small interface, two providers.

Why an interface rather than calling the SDK directly:

* **Tests must never need an API key.** Every test in this repository runs
  offline, so the diagnose node is exercised against a fake provider that can
  be told to return good JSON, malformed JSON, or an outright failure.
* **Providers are interchangeable.** ``OpenAIProvider`` speaks the OpenAI wire
  format, which Groq, Together, vLLM, Ollama and OpenAI itself all expose. The
  base URL and model name are configuration, not code.

The ``stub`` provider deserves an explanation. It is **not** a mock for tests —
it is a deterministic, rule-based diagnoser used when no API key is set, so the
whole graph can be run and demonstrated offline. It reads the same evidence and
produces the same :class:`Diagnosis` shape, by pattern-matching obvious signals
(a container that exited, an authentication error in the logs, latency an order
of magnitude above baseline). Two consequences, both deliberate:

1. Anything it produces is labelled ``source="stub"`` all the way through to
   the API and the stored postmortem, so a rule-based answer is never mistaken
   for a model's.
2. It doubles as the baseline for the Phase 4 evaluation. "The LLM beat
   if-statements by X%" is a far more honest claim than an accuracy number
   quoted on its own, and if it turns out not to, that is worth knowing.
"""

from __future__ import annotations

import abc
import json
import re
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from services.agent.config import AgentSettings
from services.common.logging_setup import get_logger

logger = get_logger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

#: Matches a ```json ... ``` fence, which many models emit despite being asked
#: for bare JSON.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    """The provider could not be reached, or refused."""


class LLMFormatError(ValueError):
    """The provider replied, but not with JSON matching the schema."""


@dataclass(frozen=True, slots=True)
class Completion:
    """A raw provider response."""

    text: str
    model: str
    #: llm | stub — propagated into the incident record.
    source: str


class LLMProviderBase(abc.ABC):
    """Anything that can turn a prompt into text."""

    @property
    @abc.abstractmethod
    def source(self) -> str:
        """``llm`` or ``stub``."""

    @abc.abstractmethod
    async def complete(
        self, *, system: str, user: str, context: dict[str, Any] | None = None
    ) -> Completion:
        """Return the model's reply.

        ``context`` carries the structured evidence. A real model ignores it
        (everything it needs is already rendered into ``user``); the stub
        provider reads it instead of parsing the prompt back out of a string.
        """

    async def aclose(self) -> None:  # pragma: no cover - overridden when needed
        return None


def extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model reply.

    Tolerant on purpose. Models wrap JSON in prose, in markdown fences, or add
    a trailing explanation, and re-asking for all three cases wastes a request
    when the payload is right there.
    """
    candidate = text.strip()
    if not candidate:
        raise LLMFormatError("empty response")

    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        # Fall back to the outermost {...} span.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise LLMFormatError(f"no JSON object found in: {text[:200]!r}") from None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMFormatError(f"invalid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise LLMFormatError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


class LLMClient:
    """Wraps a provider with JSON parsing, schema validation and retries."""

    def __init__(self, provider: LLMProviderBase, *, max_format_retries: int = 2) -> None:
        self._provider = provider
        self._max_format_retries = max_format_retries

    @property
    def source(self) -> str:
        return self._provider.source

    async def complete_model(
        self,
        *,
        system: str,
        user: str,
        schema: type[ModelT],
        context: dict[str, Any] | None = None,
    ) -> tuple[ModelT, str]:
        """Return a validated model instance plus the model name used.

        Retries on malformed output, feeding the error back so the next attempt
        has something to correct. A provider *failure* (network, auth, rate
        limit) is not retried here - it is raised, because re-asking a provider
        that is down just wastes the attempt budget.
        """
        attempts = self._max_format_retries + 1
        prompt = user
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            completion = await self._provider.complete(system=system, user=prompt, context=context)
            try:
                payload = extract_json(completion.text)
                return schema.model_validate(payload), completion.model
            except (LLMFormatError, ValidationError) as exc:
                last_error = exc
                logger.warning(
                    "llm_malformed_response",
                    extra={
                        "attempt": attempt,
                        "attempts_allowed": attempts,
                        "schema": schema.__name__,
                        "error": str(exc)[:300],
                        "reply_head": completion.text[:200],
                    },
                )
                if attempt == attempts:
                    break
                prompt = (
                    f"{user}\n\n"
                    f"Your previous reply could not be used. Error:\n{str(exc)[:600]}\n\n"
                    "Reply with ONLY a single valid JSON object matching the "
                    "schema. No prose, no markdown fences."
                )

        raise LLMFormatError(
            f"{schema.__name__} not produced after {attempts} attempt(s): {last_error}"
        )

    async def aclose(self) -> None:
        await self._provider.aclose()


class OpenAIProvider(LLMProviderBase):
    """Any OpenAI-compatible chat-completions endpoint."""

    def __init__(self, settings: AgentSettings) -> None:
        from openai import AsyncOpenAI

        if not settings.llm_api_key:
            raise LLMError(
                "llm_provider='openai' needs LLM_API_KEY. Set it, or use "
                "LLM_PROVIDER=stub to run without a model."
            )
        self._settings = settings
        self._client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,  # retries are this module's business, not the SDK's
        )

    @property
    def source(self) -> str:
        return "llm"

    async def complete(
        self, *, system: str, user: str, context: dict[str, Any] | None = None
    ) -> Completion:
        from openai import APIConnectionError, APIError, APITimeoutError, RateLimitError

        try:
            response = await self._client.chat.completions.create(
                model=self._settings.llm_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=self._settings.llm_temperature,
                max_tokens=self._settings.llm_max_tokens,
                # Honoured by OpenAI and Groq; harmless where unsupported
                # because extract_json tolerates fenced and wrapped output.
                response_format={"type": "json_object"},
            )
        except (APIConnectionError, APITimeoutError) as exc:
            raise LLMError(f"{self._settings.llm_base_url} unreachable: {exc}") from exc
        except RateLimitError as exc:
            raise LLMError(f"rate limited by {self._settings.llm_base_url}: {exc}") from exc
        except APIError as exc:
            raise LLMError(f"provider error: {exc}") from exc

        choices = response.choices
        if not choices or not choices[0].message.content:
            raise LLMFormatError("provider returned no content")

        return Completion(
            text=choices[0].message.content,
            model=response.model or self._settings.llm_model,
            source="llm",
        )

    async def aclose(self) -> None:
        await self._client.close()


def build_llm_client(settings: AgentSettings) -> LLMClient:
    """Pick a provider from settings.

    Falls back to the stub rather than crashing when ``openai`` is selected
    without a key: a running agent that labels its diagnoses ``stub`` is more
    useful than a service that will not start.
    """
    from services.agent.stub_provider import StubProvider

    provider: LLMProviderBase
    if settings.llm_provider == "openai":
        try:
            provider = OpenAIProvider(settings)
        except LLMError as exc:
            logger.warning(
                "llm_falling_back_to_stub",
                extra={"reason": str(exc)[:300], "provider_requested": "openai"},
            )
            provider = StubProvider()
    else:
        provider = StubProvider()

    logger.info(
        "llm_client_ready",
        extra={
            "provider": settings.llm_provider,
            "source": provider.source,
            "model": settings.llm_model if provider.source == "llm" else "stub-heuristic",
            "base_url": settings.llm_base_url if provider.source == "llm" else None,
        },
    )
    return LLMClient(provider, max_format_retries=settings.llm_max_format_retries)
