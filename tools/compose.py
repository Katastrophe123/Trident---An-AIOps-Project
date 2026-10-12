"""Thin wrapper around the Docker Compose CLI.

``chaos.py`` drives containers through ``docker compose`` rather than the Docker
Python SDK. Three reasons:

1. **The RUNBOOK stays honest.** Every container action the script takes is a
   command a human can paste, which is exactly what the runbook documents.
2. **Compose owns the config.** The "bad config" scenario needs a container
   recreated with a different environment, which is a compose concern - doing it
   through the SDK would mean reimplementing what ``--force-recreate`` does.
3. **No socket plumbing.** The CLI already resolves the active Docker context
   (Colima, Docker Desktop, a remote host), so there is nothing to configure.

The *agent* will use the Docker SDK in Phase 2, as the plan specifies: it needs
``get_container_status`` and ``restart_container`` from inside a container, where
the socket is mounted deliberately.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Repository root, so the script works from any working directory.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"

#: Written by the "bad config" scenario and read by compose via `env_file`.
OVERRIDE_DIR = PROJECT_ROOT / ".chaos"
OVERRIDE_FILE = OVERRIDE_DIR / "overrides.env"


class ComposeError(RuntimeError):
    """A compose command failed."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def printable(self) -> str:
        return " ".join(self.command)


def require_compose() -> None:
    """Fail early with a usable message if Docker is not available."""
    if shutil.which("docker") is None:
        raise ComposeError(
            "The `docker` CLI was not found on PATH. Start Docker (or Colima) first."
        )
    probe = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["docker", "compose", "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        raise ComposeError(
            f"`docker compose` is not usable. Is the Docker daemon running?\n{probe.stderr.strip()}"
        )


def run(*args: str, check: bool = True, timeout: float = 180.0) -> CommandResult:
    """Run ``docker compose <args>`` from the repository root.

    ``--env-file`` is what makes the bad-config scenario work, and it took a
    failed injection to get right. ``docker-compose.yml`` resolves each
    service's database URL as ``${PAYMENTS_DATABASE_URL:-<good default>}``, and
    that interpolation is done **by compose itself**, which reads only the shell
    environment or ``--env-file``. The first attempt listed the override file
    under a service's ``env_file:`` instead, which injects the variable *into
    the container* - where nothing reads it - and loses to the explicit
    ``environment:`` entry anyway. The symptom was a service that came back
    perfectly healthy after a fault was supposedly injected.

    A plain ``docker compose up`` (no ``--env-file``) therefore always gets the
    good defaults, which is the behaviour worth having: only this tool can
    break the stack.
    """
    ensure_override_file()
    command = [
        "docker",
        "compose",
        "--env-file",
        str(OVERRIDE_FILE),
        "-f",
        str(COMPOSE_FILE),
        *args,
    ]
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env={**os.environ},
    )
    result = CommandResult(
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
    )
    if check and not result.ok:
        raise ComposeError(
            f"`{result.printable}` failed with exit code {result.returncode}\n"
            f"{result.stderr or result.stdout}"
        )
    return result


def running_services() -> set[str]:
    """Names of the services currently running."""
    result = run("ps", "--services", "--filter", "status=running", check=False)
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def write_overrides(values: dict[str, str]) -> Path:
    """Write ``.chaos/overrides.env``, which compose loads into every service.

    An empty mapping writes an empty (but present) file, because compose's
    ``env_file`` entry must resolve whether or not a fault is injected.
    """
    OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Written by tools/chaos.py. Safe to delete; `chaos.py reset` clears it.",
        *(f"{key}={value}" for key, value in sorted(values.items())),
    ]
    OVERRIDE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return OVERRIDE_FILE


def clear_overrides() -> bool:
    """Blank the override file. Returns True if it had any content."""
    had_content = OVERRIDE_FILE.exists() and bool(
        [
            line
            for line in OVERRIDE_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    )
    write_overrides({})
    return had_content


def ensure_override_file() -> None:
    """Create the override file if absent, so `docker compose up` never fails."""
    if not OVERRIDE_FILE.exists():
        write_overrides({})
