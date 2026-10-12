"""Settings for the agent service.

Extends :class:`ServiceSettings` rather than replacing it, so the agent reads
the same database URL, log level and service URLs as everything else and only
adds what is specific to it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field

from services.common.config import ServiceSettings

LLMProvider = Literal["openai", "stub"]


class AgentSettings(ServiceSettings):
    """Agent-specific configuration."""

    service_name: str = "agent-service"

    # --- LLM --------------------------------------------------------------
    #: ``openai`` talks to any OpenAI-compatible endpoint (Groq, Together,
    #: vLLM, Ollama, OpenAI itself). ``stub`` is a deterministic local
    #: provider; see services/agent/llm.py for why it exists.
    llm_provider: LLMProvider = "stub"
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "llama-3.3-70b-versatile"
    #: Low, not zero: a diagnosis should be reproducible, but some providers
    #: behave badly at exactly 0.
    llm_temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=1200, ge=128, le=8192)
    llm_timeout_seconds: float = Field(default=45.0, gt=0)
    #: How many times to re-ask when the model returns unparseable JSON.
    llm_max_format_retries: int = Field(default=2, ge=0, le=5)

    # --- Evidence ---------------------------------------------------------
    #: Default window for `get_logs`.
    evidence_log_minutes: float = Field(default=5.0, gt=0)
    evidence_log_limit: int = Field(default=40, ge=1, le=500)

    # --- Docker -----------------------------------------------------------
    #: The compose project whose containers the agent reports on. Containers
    #: are matched by compose label, not by name substring.
    compose_project: str = "aiops-incident-agent"
    docker_enabled: bool = True

    # --- Alert monitor ----------------------------------------------------
    monitor_enabled: bool = True
    monitor_interval_seconds: float = Field(default=5.0, gt=0)
    #: Consecutive failed polls before an incident is opened. Two, so a single
    #: dropped request during a restart does not create an incident.
    monitor_failure_threshold: int = Field(default=2, ge=1, le=10)

    @property
    def llm_is_live(self) -> bool:
        """Whether a real model will be called."""
        return self.llm_provider == "openai" and bool(self.llm_api_key)


@lru_cache
def get_agent_settings() -> AgentSettings:
    return AgentSettings()
