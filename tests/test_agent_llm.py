"""The LLM wrapper: JSON extraction, schema validation and the retry loop.

Every test here runs offline. That is a requirement, not a convenience — a test
suite that needs an API key is a test suite that does not run in CI, and the
malformed-output path is exactly the one a live model exercises only
occasionally and at random.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.agent.config import AgentSettings
from services.agent.llm import (
    Completion,
    LLMClient,
    LLMError,
    LLMFormatError,
    LLMProviderBase,
    build_llm_client,
    extract_json,
)
from services.agent.schemas import Diagnosis, PostmortemReport

GOOD_DIAGNOSIS = {
    "root_cause": "The payments container is not running.",
    "affected_service": "payments",
    "confidence": 0.95,
    "proposed_fix": "Restart the payments container.",
    "reasoning": "Docker reports it exited and callers get connection errors.",
    "scenario": "crashed_service",
    "proposed_actions": [
        {"tool": "restart_container", "target": "payments", "reason": "it is down"}
    ],
}


class ScriptedProvider(LLMProviderBase):
    """Returns pre-written replies in order. The workhorse of these tests."""

    def __init__(self, replies: list[str | Exception], *, source: str = "llm") -> None:
        self._replies = list(replies)
        self._source = source
        self.prompts: list[str] = []
        self.calls = 0

    @property
    def source(self) -> str:
        return self._source

    async def complete(
        self, *, system: str, user: str, context: dict[str, Any] | None = None
    ) -> Completion:
        self.calls += 1
        self.prompts.append(user)
        reply = self._replies.pop(0) if self._replies else "{}"
        if isinstance(reply, Exception):
            raise reply
        return Completion(text=reply, model="scripted-1", source=self._source)


def make_settings(**overrides: Any) -> AgentSettings:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": "sqlite+aiosqlite:///:memory:",
        "monitor_enabled": False,
        "docker_enabled": False,
    }
    values.update(overrides)
    return AgentSettings(**values)


class TestExtractJson:
    def test_plain_object(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_markdown_fence(self) -> None:
        """Models emit fences constantly, even when told not to."""
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_unlabelled_fence(self) -> None:
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_prose_around_the_object(self) -> None:
        """Re-asking for this would waste a request when the payload is right
        there."""
        text = 'Sure! Here is my analysis:\n{"a": 1}\nHope that helps.'
        assert extract_json(text) == {"a": 1}

    def test_nested_braces_survive(self) -> None:
        text = 'blah {"a": {"b": [1, 2]}} blah'
        assert extract_json(text) == {"a": {"b": [1, 2]}}

    @pytest.mark.parametrize("text", ["", "   ", "no json here", "[1, 2, 3]", "{{{"])
    def test_unusable_replies_raise_format_error(self, text: str) -> None:
        with pytest.raises(LLMFormatError):
            extract_json(text)

    def test_a_json_array_is_rejected(self) -> None:
        """The schemas are objects; an array would fail validation anyway, and
        failing here gives a clearer message."""
        with pytest.raises(LLMFormatError, match="expected a JSON object"):
            extract_json("[1, 2]")


class TestCompleteModel:
    async def test_valid_json_first_time(self) -> None:
        import json

        provider = ScriptedProvider([json.dumps(GOOD_DIAGNOSIS)])
        client = LLMClient(provider)

        diagnosis, model = await client.complete_model(system="s", user="u", schema=Diagnosis)

        assert isinstance(diagnosis, Diagnosis)
        assert diagnosis.affected_service == "payments"
        assert diagnosis.confidence == 0.95
        assert model == "scripted-1"
        assert provider.calls == 1

    async def test_malformed_json_is_retried(self) -> None:
        import json

        provider = ScriptedProvider(["not json at all", json.dumps(GOOD_DIAGNOSIS)])
        client = LLMClient(provider, max_format_retries=2)

        diagnosis, _ = await client.complete_model(system="s", user="u", schema=Diagnosis)

        assert diagnosis.affected_service == "payments"
        assert provider.calls == 2

    async def test_the_retry_prompt_contains_the_error(self) -> None:
        """Feeding the failure back is the point of retrying; re-sending the
        identical prompt would just re-roll the dice."""
        import json

        provider = ScriptedProvider(["garbage", json.dumps(GOOD_DIAGNOSIS)])
        client = LLMClient(provider, max_format_retries=1)
        await client.complete_model(system="s", user="original prompt", schema=Diagnosis)

        assert provider.prompts[0] == "original prompt"
        assert "could not be used" in provider.prompts[1]
        assert "original prompt" in provider.prompts[1]

    async def test_schema_violations_are_retried_too(self) -> None:
        """Syntactically valid JSON that breaks the contract: confidence above
        1.0 and an invented service name."""
        import json

        bad = {**GOOD_DIAGNOSIS, "confidence": 1.5}
        worse = {**GOOD_DIAGNOSIS, "affected_service": "billing"}
        provider = ScriptedProvider(
            [json.dumps(bad), json.dumps(worse), json.dumps(GOOD_DIAGNOSIS)]
        )
        client = LLMClient(provider, max_format_retries=2)

        diagnosis, _ = await client.complete_model(system="s", user="u", schema=Diagnosis)

        assert diagnosis.confidence == 0.95
        assert provider.calls == 3

    async def test_gives_up_after_the_retry_budget(self) -> None:
        provider = ScriptedProvider(["nope", "still nope", "nope again", "and again"])
        client = LLMClient(provider, max_format_retries=2)

        with pytest.raises(LLMFormatError, match="not produced after 3 attempt"):
            await client.complete_model(system="s", user="u", schema=Diagnosis)

        assert provider.calls == 3  # 1 + 2 retries, not 4

    async def test_zero_retries_means_one_attempt(self) -> None:
        provider = ScriptedProvider(["nope"])
        client = LLMClient(provider, max_format_retries=0)

        with pytest.raises(LLMFormatError):
            await client.complete_model(system="s", user="u", schema=Diagnosis)
        assert provider.calls == 1

    async def test_a_provider_failure_is_not_retried(self) -> None:
        """Re-asking a provider that is down only burns the attempt budget.
        Format errors are the agent's problem to fix; outages are not."""
        provider = ScriptedProvider([LLMError("connection refused"), '{"whatever": true}'])
        client = LLMClient(provider, max_format_retries=2)

        with pytest.raises(LLMError, match="connection refused"):
            await client.complete_model(system="s", user="u", schema=Diagnosis)
        assert provider.calls == 1

    async def test_extra_fields_are_ignored_not_fatal(self) -> None:
        """Models like to add commentary keys. Dropping them beats rejecting
        an otherwise good answer."""
        import json

        payload = {**GOOD_DIAGNOSIS, "severity": "high", "notes": "extra"}
        provider = ScriptedProvider([json.dumps(payload)])
        client = LLMClient(provider)

        diagnosis, _ = await client.complete_model(system="s", user="u", schema=Diagnosis)
        assert diagnosis.root_cause.startswith("The payments container")

    async def test_postmortem_schema_also_validated(self) -> None:
        import json

        provider = ScriptedProvider(
            [json.dumps({"summary": "payments down", "body": "## Summary\n\nIt broke."})]
        )
        client = LLMClient(provider)

        report, _ = await client.complete_model(system="s", user="u", schema=PostmortemReport)
        assert isinstance(report, PostmortemReport)
        assert report.summary == "payments down"

    async def test_client_reports_its_source(self) -> None:
        assert LLMClient(ScriptedProvider([], source="llm")).source == "llm"
        assert LLMClient(ScriptedProvider([], source="stub")).source == "stub"


class TestProviderSelection:
    def test_stub_is_the_default(self) -> None:
        client = build_llm_client(make_settings())
        assert client.source == "stub"

    def test_openai_without_a_key_falls_back_to_the_stub(self) -> None:
        """A running agent that labels its answers `stub` is more useful than a
        service that refuses to start."""
        client = build_llm_client(make_settings(llm_provider="openai", llm_api_key=None))
        assert client.source == "stub"

    def test_openai_with_a_key_builds_a_live_provider(self) -> None:
        """No request is made - only the client is constructed."""
        settings = make_settings(
            llm_provider="openai",
            llm_api_key="sk-not-a-real-key",
            llm_base_url="https://example.invalid/v1",
        )
        client = build_llm_client(settings)

        assert client.source == "llm"
        assert settings.llm_is_live is True

    def test_llm_is_live_requires_both_provider_and_key(self) -> None:
        assert make_settings(llm_provider="stub", llm_api_key="sk-x").llm_is_live is False
        assert make_settings(llm_provider="openai", llm_api_key=None).llm_is_live is False
        assert make_settings(llm_provider="openai", llm_api_key="sk-x").llm_is_live is True

    def test_retry_budget_comes_from_settings(self) -> None:
        client = build_llm_client(make_settings(llm_max_format_retries=4))
        assert client._max_format_retries == 4  # noqa: SLF001
