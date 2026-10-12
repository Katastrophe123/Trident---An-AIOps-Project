"""The operator tooling: the compose wrapper, the chaos CLI and the traffic generator.

These are scripts, not services, but they are the interface a human (and in
Phase 3, the scoring harness) actually uses, so the parts that can be tested
offline are tested: which compose command each scenario issues, what the
override file ends up containing, and that the traffic generator treats a
failure as a data point rather than a crash.

What is *not* here: actually stopping a container. That is verified by hand in
the RUNBOOK walkthrough, because faking it would only test the mock.
"""

from __future__ import annotations

import inspect
import pathlib
import random
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from tools import chaos as chaos_cli
from tools import compose, traffic


@pytest.fixture
def override_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect .chaos/overrides.env into a temp dir."""
    path = tmp_path / ".chaos" / "overrides.env"
    monkeypatch.setattr(compose, "OVERRIDE_DIR", path.parent)
    monkeypatch.setattr(compose, "OVERRIDE_FILE", path)
    return path


# --------------------------------------------------------------------------- #
# tools/compose.py
# --------------------------------------------------------------------------- #
class TestCommandResult:
    def test_ok_is_exit_code_zero(self) -> None:
        good = compose.CommandResult(command=["docker"], returncode=0, stdout="", stderr="")
        bad = compose.CommandResult(command=["docker"], returncode=1, stdout="", stderr="")
        assert good.ok is True
        assert bad.ok is False

    def test_printable_is_the_command_a_human_could_paste(self) -> None:
        result = compose.CommandResult(
            command=["docker", "compose", "stop", "payments"],
            returncode=0,
            stdout="",
            stderr="",
        )
        assert result.printable == "docker compose stop payments"


class TestOverrides:
    def test_writing_creates_the_directory_and_file(self, override_file: Path) -> None:
        compose.write_overrides({"PAYMENTS_DATABASE_URL": "postgresql://bad"})

        assert override_file.exists()
        content = override_file.read_text(encoding="utf-8")
        assert "PAYMENTS_DATABASE_URL=postgresql://bad" in content
        assert content.startswith("#")  # explains itself to whoever finds it

    def test_keys_are_sorted_so_the_file_is_stable(self, override_file: Path) -> None:
        compose.write_overrides({"B": "2", "A": "1"})
        lines = [
            line
            for line in override_file.read_text(encoding="utf-8").splitlines()
            if not line.startswith("#")
        ]
        assert lines == ["A=1", "B=2"]

    def test_an_empty_mapping_still_writes_a_file(self, override_file: Path) -> None:
        """compose's `env_file` entry has to resolve even with no fault injected."""
        compose.write_overrides({})
        assert override_file.exists()

    def test_clear_reports_whether_anything_was_set(self, override_file: Path) -> None:
        compose.write_overrides({"ORDERS_DATABASE_URL": "postgresql://bad"})
        assert compose.clear_overrides() is True
        # Second time there is nothing left to clear.
        assert compose.clear_overrides() is False

    def test_clear_ignores_the_comment_header(self, override_file: Path) -> None:
        compose.write_overrides({})
        assert compose.clear_overrides() is False

    def test_clear_when_the_file_never_existed(self, override_file: Path) -> None:
        assert not override_file.exists()
        assert compose.clear_overrides() is False
        assert override_file.exists()

    def test_ensure_is_idempotent(self, override_file: Path) -> None:
        compose.write_overrides({"KEEP": "me"})
        compose.ensure_override_file()
        assert "KEEP=me" in override_file.read_text(encoding="utf-8")


class TestComposeRun:
    def test_the_compose_file_is_passed_explicitly(
        self, override_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """So the script works from any working directory.

        ``--env-file`` rides along on every call: it is the only thing compose
        consults when interpolating ``${VAR}``, and the bad-config scenario
        depends on it.
        """
        seen: dict[str, Any] = {}

        def fake_run(command: list[str], **kwargs: Any) -> Any:
            seen["command"] = command
            seen["cwd"] = kwargs.get("cwd")
            return subprocess.CompletedProcess(command, 0, "ok\n", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        result = compose.run("stop", "payments")

        assert seen["command"] == [
            "docker",
            "compose",
            "--env-file",
            str(compose.OVERRIDE_FILE),
            "-f",
            str(compose.COMPOSE_FILE),
            "stop",
            "payments",
        ]
        assert seen["cwd"] == compose.PROJECT_ROOT
        assert result.stdout == "ok"

    def test_a_failure_raises_with_the_command_and_stderr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(command: list[str], **kwargs: Any) -> Any:
            return subprocess.CompletedProcess(command, 1, "", "no such service: nope")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(compose.ComposeError) as excinfo:
            compose.run("stop", "nope")

        assert "no such service" in str(excinfo.value)
        assert "docker compose" in str(excinfo.value)

    def test_check_false_returns_the_failure_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(command: list[str], **kwargs: Any) -> Any:
            return subprocess.CompletedProcess(command, 1, "", "boom")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert compose.run("ps", check=False).ok is False

    def test_running_services_parses_one_name_per_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(command: list[str], **kwargs: Any) -> Any:
            return subprocess.CompletedProcess(
                command, 0, "orders\npayments\n\n  inventory  \n", ""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert compose.running_services() == {"orders", "payments", "inventory"}

    def test_running_services_is_empty_when_nothing_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(command: list[str], **kwargs: Any) -> Any:
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert compose.running_services() == set()

    def test_a_missing_docker_cli_says_so_in_plain_words(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(compose.shutil, "which", lambda _: None)
        with pytest.raises(compose.ComposeError) as excinfo:
            compose.require_compose()
        assert "not found on PATH" in str(excinfo.value)
        assert "Colima" in str(excinfo.value)  # names the likely cause on this mac

    def test_a_dead_daemon_is_distinguished_from_a_missing_cli(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(compose.shutil, "which", lambda _: "/usr/bin/docker")

        def fake_run(command: list[str], **kwargs: Any) -> Any:
            return subprocess.CompletedProcess(command, 1, "", "cannot connect")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(compose.ComposeError) as excinfo:
            compose.require_compose()
        assert "daemon running" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# tools/chaos.py
# --------------------------------------------------------------------------- #
class TestChaosCliBasics:
    def test_every_scenario_from_the_plan_has_a_subcommand(self) -> None:
        parser = chaos_cli.build_parser()
        actions = [
            action
            for action in parser._actions  # noqa: SLF001
            if isinstance(action, type(parser._subparsers._group_actions[0]))  # noqa: SLF001
        ]
        names = set(actions[0].choices)
        assert names == {
            "status",
            "crash",
            "bad-config",
            "slow-dependency",
            "memory-leak",
            "db-exhaustion",
            "reset",
        }

    def test_service_urls_match_the_published_ports(self) -> None:
        assert chaos_cli.service_url("orders") == "http://localhost:8001"
        assert chaos_cli.service_url("payments") == "http://localhost:8002"
        assert chaos_cli.service_url("inventory") == "http://localhost:8003"

    def test_an_unknown_service_exits_with_the_valid_choices(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            chaos_cli.service_url("billing")
        assert "orders" in str(excinfo.value)

    def test_quiet_mode_prints_nothing(self, capsys: pytest.CaptureFixture[str]) -> None:
        reporter = chaos_cli.Reporter(quiet=True)
        reporter.say("a")
        reporter.step("b")
        reporter.ok("c")
        reporter.warn("d")
        reporter.heading("e")
        assert capsys.readouterr().out == ""

    def test_loud_mode_prints_everything(self, capsys: pytest.CaptureFixture[str]) -> None:
        chaos_cli.Reporter().ok("done")
        assert "done" in capsys.readouterr().out

    def test_the_defaults_target_different_services_on_purpose(self) -> None:
        """Each scenario defaults to the service where it is most legible: a
        crash downstream of orders, a leak in orders itself."""
        parsed = chaos_cli.build_parser().parse_args(["crash"])
        assert parsed.service == "payments"
        assert chaos_cli.build_parser().parse_args(["memory-leak"]).service == "orders"
        assert chaos_cli.build_parser().parse_args(["slow-dependency"]).service == "inventory"


class TestContainerScenarios:
    """What matters is which compose command is issued, not the mock's reply."""

    @pytest.fixture
    def recorded(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(compose, "require_compose", lambda: None)
        monkeypatch.setattr(
            compose,
            "run",
            lambda *args, **kwargs: (
                calls.append(args)  # type: ignore[func-returns-value]
                or compose.CommandResult(command=list(args), returncode=0, stdout="", stderr="")
            ),
        )
        return calls

    def test_crash_stops_the_container(
        self, recorded: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chaos_cli, "wait_for_health", lambda *a, **k: True)
        assert chaos_cli.scenario_crash("payments", chaos_cli.Reporter(quiet=True)) == 0
        assert recorded == [("stop", "payments")]

    def test_crash_waits_for_the_service_to_actually_stop(
        self, recorded: list[tuple[str, ...]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_wait(service: str, **kwargs: Any) -> bool:
            seen.update(service=service, **kwargs)
            return True

        monkeypatch.setattr(chaos_cli, "wait_for_health", fake_wait)
        chaos_cli.scenario_crash("orders", chaos_cli.Reporter(quiet=True))
        assert seen["service"] == "orders"
        assert seen["expect_reachable"] is False

    def test_crash_warns_when_the_service_is_still_answering(
        self,
        recorded: list[tuple[str, ...]],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(chaos_cli, "wait_for_health", lambda *a, **k: False)
        chaos_cli.scenario_crash("payments", chaos_cli.Reporter())
        assert "still answers" in capsys.readouterr().out

    def test_bad_config_writes_a_per_service_override(
        self,
        recorded: list[tuple[str, ...]],
        override_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(chaos_cli, "wait_for_health", lambda *a, **k: True)
        monkeypatch.setattr(chaos_cli, "get_json", lambda *a, **k: {"status": "unhealthy"})

        assert chaos_cli.scenario_bad_config("payments", chaos_cli.Reporter(quiet=True)) == 0

        content = override_file.read_text(encoding="utf-8")
        assert "PAYMENTS_DATABASE_URL=" in content
        assert chaos_cli.WRONG_PASSWORD in content
        # Only the targeted service is touched.
        assert "ORDERS_DATABASE_URL" not in content
        assert "INVENTORY_DATABASE_URL" not in content

    def test_bad_config_recreates_only_that_container(
        self,
        recorded: list[tuple[str, ...]],
        override_file: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(chaos_cli, "wait_for_health", lambda *a, **k: True)
        monkeypatch.setattr(chaos_cli, "get_json", lambda *a, **k: {"status": "unhealthy"})
        chaos_cli.scenario_bad_config("inventory", chaos_cli.Reporter(quiet=True))

        assert recorded == [("up", "-d", "--force-recreate", "--no-deps", "inventory")]

    def test_bad_config_flags_a_service_that_came_back_healthy(
        self,
        recorded: list[tuple[str, ...]],
        override_file: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """If the override was not picked up the scenario silently did nothing,
        which would waste an operator's time looking for a symptom."""
        monkeypatch.setattr(chaos_cli, "wait_for_health", lambda *a, **k: True)
        monkeypatch.setattr(chaos_cli, "get_json", lambda *a, **k: {"status": "healthy"})

        chaos_cli.scenario_bad_config("payments", chaos_cli.Reporter())
        out = capsys.readouterr().out
        assert "Expected unhealthy" in out
        assert "env_file" in out  # points at the likely cause


class TestInProcessScenarios:
    @pytest.fixture
    def posted(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
        calls: list[tuple[str, str]] = []

        def fake_post(service: str, path: str, **kwargs: Any) -> dict[str, Any]:
            calls.append((service, path))
            return {
                "latency_ms": 3000,
                "leaked_mb": 4,
                "held_db_connections": 20,
                "healthy": False,
            }

        monkeypatch.setattr(chaos_cli, "post_chaos", fake_post)
        return calls

    def test_slow_dependency_converts_seconds_to_milliseconds(
        self, posted: list[tuple[str, str]]
    ) -> None:
        chaos_cli.scenario_slow_dependency("inventory", 3.0, chaos_cli.Reporter(quiet=True))
        assert posted == [("inventory", "/latency?milliseconds=3000")]

    def test_slow_dependency_with_zero_is_the_documented_fix(
        self, posted: list[tuple[str, str]]
    ) -> None:
        chaos_cli.scenario_slow_dependency("inventory", 0, chaos_cli.Reporter(quiet=True))
        assert posted == [("inventory", "/latency?milliseconds=0")]

    def test_memory_leak_starts_the_leak(self, posted: list[tuple[str, str]]) -> None:
        chaos_cli.scenario_memory_leak("orders", chaos_cli.Reporter(quiet=True))
        assert posted == [("orders", "/memory-leak?enabled=true")]

    def test_memory_leak_tells_you_to_restart_not_to_un_leak(
        self, posted: list[tuple[str, str]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        chaos_cli.scenario_memory_leak("orders", chaos_cli.Reporter())
        assert "docker compose restart orders" in capsys.readouterr().out

    def test_db_exhaustion_passes_the_connection_count(self, posted: list[tuple[str, str]]) -> None:
        chaos_cli.scenario_db_exhaustion("payments", 20, chaos_cli.Reporter(quiet=True))
        assert posted == [("payments", "/db-exhaust?connections=20")]

    def test_db_exhaustion_reports_a_partial_grab_as_the_symptom(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            chaos_cli,
            "post_chaos",
            lambda *a, **k: {"held_db_connections": 7, "healthy": False},
        )
        chaos_cli.scenario_db_exhaustion("payments", 20, chaos_cli.Reporter())
        assert "refused after 7" in capsys.readouterr().out

    def test_every_scenario_prints_its_manual_fix(
        self, posted: list[tuple[str, str]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The runbook promises one, so the tool has to print one."""
        reporter = chaos_cli.Reporter()
        chaos_cli.scenario_slow_dependency("inventory", 1, reporter)
        chaos_cli.scenario_memory_leak("orders", reporter)
        chaos_cli.scenario_db_exhaustion("payments", 5, reporter)
        assert capsys.readouterr().out.count("Fix by hand:") == 3


class TestStatus:
    def test_all_healthy_is_true_only_when_every_service_is(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_get(service: str, path: str, **kwargs: Any) -> dict[str, Any] | None:
            if path == "/health":
                return {"status": "healthy"}
            if path == "/metrics":
                return {"requests": 10, "errors": 0}
            return {}

        monkeypatch.setattr(chaos_cli, "get_json", fake_get)
        monkeypatch.setattr(compose, "require_compose", lambda: None)
        monkeypatch.setattr(compose, "running_services", lambda: {"orders"})

        assert chaos_cli.print_status(chaos_cli.Reporter(quiet=True)) is True

    def test_an_unreachable_service_makes_it_false_and_names_the_container(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fake_get(service: str, path: str, **kwargs: Any) -> dict[str, Any] | None:
            if service == "payments":
                return None
            return {"status": "healthy"} if path == "/health" else {}

        monkeypatch.setattr(chaos_cli, "get_json", fake_get)
        monkeypatch.setattr(compose, "require_compose", lambda: None)
        monkeypatch.setattr(compose, "running_services", lambda: {"orders", "inventory"})

        assert chaos_cli.print_status(chaos_cli.Reporter()) is False
        out = capsys.readouterr().out
        assert "UNREACHABLE" in out
        assert "container stopped" in out

    def test_injected_faults_are_listed_next_to_the_service(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fake_get(service: str, path: str, **kwargs: Any) -> dict[str, Any] | None:
            if path == "/health":
                return {"status": "degraded", "detail": "fault injection is active"}
            if path == "/_chaos":
                return {
                    "latency_ms": 3000,
                    "memory_leak_active": True,
                    "leaked_mb": 64,
                    "held_db_connections": 20,
                }
            return {}

        monkeypatch.setattr(chaos_cli, "get_json", fake_get)
        monkeypatch.setattr(compose, "require_compose", lambda: None)
        monkeypatch.setattr(compose, "running_services", lambda: set())

        assert chaos_cli.print_status(chaos_cli.Reporter()) is False
        out = capsys.readouterr().out
        assert "delay 3000ms" in out
        assert "leaking 64MiB" in out
        assert "20 held conns" in out
        assert "detail:" in out

    def test_status_works_even_with_no_docker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reading health over HTTP does not need the Docker CLI."""

        def boom() -> None:
            raise compose.ComposeError("no docker")

        monkeypatch.setattr(compose, "require_compose", boom)
        monkeypatch.setattr(chaos_cli, "get_json", lambda *a, **k: None)

        assert chaos_cli.print_status(chaos_cli.Reporter(quiet=True)) is False


class TestCliExitCodes:
    """Exit codes matter: this is what a script or CI would branch on."""

    def test_status_exits_nonzero_when_unhealthy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chaos_cli, "print_status", lambda reporter: False)
        assert chaos_cli.main(["--quiet", "status"]) == 1

    def test_status_exits_zero_when_healthy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chaos_cli, "print_status", lambda reporter: True)
        assert chaos_cli.main(["--quiet", "status"]) == 0

    def test_a_docker_problem_exits_two_with_a_message(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def boom(*args: Any, **kwargs: Any) -> None:
            raise compose.ComposeError("daemon is not running")

        monkeypatch.setattr(chaos_cli, "scenario_crash", boom)
        assert chaos_cli.main(["--quiet", "crash"]) == 2
        assert "daemon is not running" in capsys.readouterr().err

    def test_an_unreachable_service_exits_two_and_suggests_the_fix(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def boom(*args: Any, **kwargs: Any) -> None:
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(chaos_cli, "scenario_memory_leak", boom)
        assert chaos_cli.main(["--quiet", "memory-leak"]) == 2
        assert "docker compose up -d" in capsys.readouterr().err

    def test_a_missing_subcommand_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            chaos_cli.main([])

    def test_an_unknown_service_is_rejected_by_the_parser(self) -> None:
        with pytest.raises(SystemExit):
            chaos_cli.main(["crash", "--service", "billing"])


class TestWaitForHealth:
    def test_returns_as_soon_as_the_expectation_is_met(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chaos_cli, "get_json", lambda *a, **k: {"status": "healthy"})
        assert chaos_cli.wait_for_health("orders", expect_reachable=True) is True

    def test_gives_up_after_the_attempt_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chaos_cli, "get_json", lambda *a, **k: None)
        monkeypatch.setattr(chaos_cli.time, "sleep", lambda _: None)
        assert chaos_cli.wait_for_health("orders", expect_reachable=True, attempts=3) is False

    def test_can_wait_for_a_service_to_go_away(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(chaos_cli, "get_json", lambda *a, **k: None)
        assert chaos_cli.wait_for_health("payments", expect_reachable=False) is True


class TestHttpHelpers:
    @staticmethod
    def patch_client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
        """Make chaos_cli's httpx.Client() return a mock-transport client.

        The real class has to be captured first: ``chaos_cli.httpx`` *is* the
        httpx module, so a replacement that called ``httpx.Client`` would call
        itself.
        """
        real_client = httpx.Client
        monkeypatch.setattr(
            httpx,
            "Client",
            lambda **kwargs: real_client(transport=httpx.MockTransport(handler)),
        )

    def test_get_json_keeps_a_503_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unhealthy service returns 503 with the diagnosis in the body, and
        throwing that away would discard the root cause."""
        self.patch_client(
            monkeypatch,
            lambda request: httpx.Response(503, json={"status": "unhealthy", "detail": "db down"}),
        )
        assert chaos_cli.get_json("orders", "/health") == {
            "status": "unhealthy",
            "detail": "db down",
        }

    def test_get_json_returns_none_on_a_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.patch_client(monkeypatch, lambda request: httpx.Response(404, json={}))
        assert chaos_cli.get_json("orders", "/nope") is None

    def test_get_json_returns_none_when_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        self.patch_client(monkeypatch, handler)
        assert chaos_cli.get_json("payments", "/health") is None


# --------------------------------------------------------------------------- #
# tools/traffic.py
# --------------------------------------------------------------------------- #
class TestOrderGeneration:
    def test_a_seeded_generator_is_reproducible(self) -> None:
        first = [traffic.build_order(random.Random(7)) for _ in range(3)]
        second = [traffic.build_order(random.Random(7)) for _ in range(3)]
        assert first == second

    def test_most_orders_are_servable(self) -> None:
        rng = random.Random(1234)
        orders = [traffic.build_order(rng) for _ in range(2000)]
        unservable = [
            order
            for order in orders
            if order["sku"] == "SKU-DOES-NOT-EXIST" or order["quantity"] == 20
        ]
        share = len(unservable) / len(orders)
        # Deliberately present, deliberately small: a baseline of legitimate
        # 4xx that an agent must not mistake for an incident.
        assert 0.01 < share < 0.08
        assert abs(share - traffic.UNSERVABLE_SHARE) < 0.02

    def test_servable_orders_use_real_skus_and_sane_quantities(self) -> None:
        rng = random.Random(99)
        for _ in range(200):
            order = traffic.build_order(rng)
            if order["sku"] == "SKU-DOES-NOT-EXIST":
                continue
            assert order["sku"] in traffic.SKUS
            assert 1 <= int(order["quantity"]) <= 20  # type: ignore[call-overload]

    def test_both_kinds_of_refusal_are_generated(self) -> None:
        rng = random.Random(2024)
        orders = [traffic.build_order(rng) for _ in range(5000)]
        assert any(order["sku"] == "SKU-DOES-NOT-EXIST" for order in orders)
        assert any(order["sku"] == "SKU-LAPTOP-15" and order["quantity"] == 20 for order in orders)

    def test_every_order_has_a_customer_id(self) -> None:
        rng = random.Random(5)
        assert all(traffic.build_order(rng)["customer_id"] for _ in range(50))


class TestClassify:
    @pytest.mark.parametrize(
        ("status_code", "body", "expected"),
        [
            (201, {"status": "confirmed"}, "confirmed"),
            (201, {"status": "rejected"}, "rejected"),
            (201, {"status": "failed"}, "failed"),
            (200, {}, "created"),
            (502, {}, "http_502_dependency_down"),
            (404, {}, "http_404"),
            (422, {}, "http_422"),
            (500, {}, "http_500"),
        ],
    )
    def test_buckets(self, status_code: int, body: dict[str, Any], expected: str) -> None:
        response = httpx.Response(status_code, json=body)
        assert traffic.classify(response) == expected

    def test_a_non_json_success_still_classifies(self) -> None:
        assert traffic.classify(httpx.Response(200, text="OK")) == "created"


class TestSending:
    @staticmethod
    def client_for(handler: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://orders")

    async def test_a_confirmed_order_is_recorded(self) -> None:
        stats = traffic.Stats()
        client = self.client_for(lambda request: httpx.Response(201, json={"status": "confirmed"}))
        await traffic.send_one(client, {"sku": "x"}, stats)
        assert stats.outcomes["confirmed"] == 1
        await client.aclose()

    async def test_a_timeout_is_a_data_point_not_an_exception(self) -> None:
        """The generator must survive the slow-dependency scenario."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        stats = traffic.Stats()
        client = self.client_for(handler)
        await traffic.send_one(client, {"sku": "x"}, stats)
        assert stats.outcomes["timeout"] == 1
        await client.aclose()

    async def test_an_unreachable_orders_service_is_recorded(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        stats = traffic.Stats()
        client = self.client_for(handler)
        await traffic.send_one(client, {"sku": "x"}, stats)
        assert stats.outcomes["unreachable"] == 1
        await client.aclose()

    async def test_a_502_is_attributed_to_a_downstream(self) -> None:
        stats = traffic.Stats()
        client = self.client_for(lambda request: httpx.Response(502, json={}))
        await traffic.send_one(client, {"sku": "x"}, stats)
        assert stats.outcomes["http_502_dependency_down"] == 1
        await client.aclose()


class TestStats:
    def test_total_sums_every_outcome(self) -> None:
        stats = traffic.Stats()
        stats.record("confirmed")
        stats.record("confirmed")
        stats.record("rejected")
        assert stats.total == 3

    def test_the_line_shows_the_rate_and_the_breakdown(self) -> None:
        stats = traffic.Stats()
        stats.record("confirmed")
        line = stats.line()
        assert "1 sent" in line
        assert "confirmed=1" in line
        assert "/s" in line

    def test_elapsed_never_divides_by_zero(self) -> None:
        assert traffic.Stats().elapsed > 0


class TestTrafficRun:
    async def test_count_stops_the_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            sent["n"] += 1
            return httpx.Response(201, json={"status": "confirmed"})

        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            traffic.httpx,
            "AsyncClient",
            lambda **kwargs: real_client(
                transport=httpx.MockTransport(handler), base_url=kwargs.get("base_url", "")
            ),
        )

        code = await traffic.run(
            url="http://orders", rate=1000.0, count=5, report_every=999, seed=1
        )
        assert code == 0
        assert sent["n"] == 5

    async def test_oneshot_sends_a_known_good_order(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json as _json

            seen["payload"] = _json.loads(request.content)
            return httpx.Response(201, json={"status": "confirmed"})

        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            traffic.httpx,
            "AsyncClient",
            lambda **kwargs: real_client(
                transport=httpx.MockTransport(handler), base_url=kwargs.get("base_url", "")
            ),
        )

        assert await traffic.oneshot("http://orders") == 0
        assert seen["payload"]["sku"] in traffic.SKUS
        assert seen["payload"]["quantity"] == 1
        assert "confirmed" in capsys.readouterr().out

    async def test_oneshot_exits_two_when_orders_is_down(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            traffic.httpx,
            "AsyncClient",
            lambda **kwargs: real_client(
                transport=httpx.MockTransport(handler), base_url=kwargs.get("base_url", "")
            ),
        )

        assert await traffic.oneshot("http://orders") == 2
        assert "failed" in capsys.readouterr().err

    async def test_oneshot_exits_one_on_a_rejection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            traffic.httpx,
            "AsyncClient",
            lambda **kwargs: real_client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(422, json={"error": "bad"})
                ),
                base_url=kwargs.get("base_url", ""),
            ),
        )
        assert await traffic.oneshot("http://orders") == 1


class TestTrafficCli:
    def test_defaults_point_at_the_published_orders_port(self) -> None:
        parsed = traffic.build_parser().parse_args([])
        assert parsed.url == traffic.DEFAULT_ORDERS_URL
        assert parsed.url.endswith(":8001")
        assert parsed.rate == 2.0
        assert parsed.count is None

    def test_oneshot_is_dispatched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: dict[str, Any] = {}

        async def fake_oneshot(url: str) -> int:
            called["url"] = url
            return 0

        monkeypatch.setattr(traffic, "oneshot", fake_oneshot)
        assert traffic.main(["--oneshot", "--url", "http://x"]) == 0
        assert called["url"] == "http://x"

    def test_run_receives_every_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: dict[str, Any] = {}

        async def fake_run(**kwargs: Any) -> int:
            called.update(kwargs)
            return 0

        monkeypatch.setattr(traffic, "run", fake_run)
        traffic.main(["--rate", "5", "--count", "10", "--seed", "3"])
        assert called["rate"] == 5.0
        assert called["count"] == 10
        assert called["seed"] == 3


def test_the_cli_entrypoints_are_coroutines() -> None:
    """main() wraps these in asyncio.run, so they have to stay async."""
    assert inspect.iscoroutinefunction(traffic.run)
    assert inspect.iscoroutinefunction(traffic.oneshot)


class TestBadConfigUsesComposeInterpolation:
    """A regression guard for an injection that silently did nothing.

    The first implementation listed the override file under a service's
    ``env_file:``. That injects the variable into the *container*, where nothing
    reads it, and loses to ``environment:`` anyway - so payments came back
    perfectly healthy after the fault was supposedly injected. The fix is
    ``docker compose --env-file``, which feeds compose's own interpolation.
    """

    def test_every_compose_command_passes_the_override_file(
        self, override_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_run(command: list[str], **kwargs: Any) -> Any:
            seen["command"] = command
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(subprocess, "run", fake_run)
        compose.run("ps")

        command = seen["command"]
        assert "--env-file" in command
        # A global flag, so it has to precede the subcommand.
        assert command.index("--env-file") < command.index("ps")
        assert command[command.index("--env-file") + 1] == str(compose.OVERRIDE_FILE)

    def test_the_override_file_is_created_before_compose_reads_it(
        self, override_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """compose errors out on a missing --env-file, so it cannot be absent."""
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
        )
        assert not override_file.exists()
        compose.run("ps")
        assert override_file.exists()

    def test_the_override_key_matches_what_the_compose_file_interpolates(self) -> None:
        """The two have to agree or the fault is a no-op, which is exactly the
        bug this guards against."""
        compose_text = compose.COMPOSE_FILE.read_text(encoding="utf-8")
        for service in chaos_cli.SERVICE_PORTS:
            key = f"{service.upper()}_DATABASE_URL"
            assert f"${{{key}:-" in compose_text, key

    def test_no_service_declares_env_file(self) -> None:
        """Keeping the directive would imply a mechanism that does not work.

        Comments are allowed to mention it - one does, to explain why.
        """
        directives = [
            line
            for line in compose.COMPOSE_FILE.read_text(encoding="utf-8").splitlines()
            if "env_file" in line and not line.lstrip().startswith("#")
        ]
        assert directives == []


class TestProjectConfigIsValid:
    """Guards two files nothing else exercises until it is too late.

    The CI workflow shipped unparseable: `DATABASE_URL: sqlite+aiosqlite:///:memory:`
    is invalid YAML, because a plain scalar ending in ':' is read as a mapping
    key. Nothing caught it locally because nothing ever parsed the file - it
    would simply have failed on GitHub.
    """

    @staticmethod
    def load(path: str) -> Any:
        import yaml

        return yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))

    def test_the_ci_workflow_parses(self) -> None:
        workflow = self.load(".github/workflows/ci.yml")
        assert set(workflow["jobs"]) == {"quality", "compose"}
        assert workflow["jobs"]["quality"]["steps"]

    def test_the_ci_workflow_runs_the_whole_gate(self) -> None:
        workflow = self.load(".github/workflows/ci.yml")
        commands = " ".join(
            str(step.get("run", "")) for step in workflow["jobs"]["quality"]["steps"]
        )
        for expected in ("ruff check", "ruff format --check", "mypy", "pytest"):
            assert expected in commands, expected

    def test_the_compose_file_parses_and_defines_every_service(self) -> None:
        compose_file = self.load("docker-compose.yml")
        assert set(compose_file["services"]) == {
            "db",
            "orders",
            "payments",
            "inventory",
            "console",
            "agent",
            "traffic",
        }

    def test_only_one_service_runs_migrations(self) -> None:
        """Three services racing `alembic upgrade head` is an intermittent
        failure indistinguishable from an injected fault."""
        compose_file = self.load("docker-compose.yml")
        migrators = [
            name
            for name, spec in compose_file["services"].items()
            if str((spec.get("environment") or {}).get("RUN_MIGRATIONS", "")).lower() == "true"
        ]
        assert migrators == ["orders"]

    def test_only_the_agent_gets_the_docker_socket(self) -> None:
        """The plan reserves socket access for the agent."""
        compose_file = self.load("docker-compose.yml")
        with_socket = [
            name
            for name, spec in compose_file["services"].items()
            if any("docker.sock" in str(v) for v in (spec.get("volumes") or []))
        ]
        assert with_socket == ["agent"]
