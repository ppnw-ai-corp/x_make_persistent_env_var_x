from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from typing import TYPE_CHECKING, Protocol, cast
from unittest.mock import patch

import x_make_persistent_env_var_x.x_cls_make_persistent_env_var_x as module
from x_make_persistent_env_var_x.x_cls_make_persistent_env_var_x import (
    main_json,
    x_cls_make_persistent_env_var_x,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping


class TryEmit(Protocol):
    def __call__(self, *emitters: Callable[[], None]) -> None: ...


class ExpectationFailedError(AssertionError):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class ExpectationMismatchError(AssertionError):
    def __init__(self, label: str, expected: object, actual: object) -> None:
        super().__init__(f"{label}: expected {expected!r}, got {actual!r}")


MISSING_EXIT_CODE = 2


def expect(condition: object, message: str) -> None:
    if not bool(condition):
        raise ExpectationFailedError(message)


def expect_equal(actual: object, expected: object, *, label: str) -> None:
    if actual != expected:
        raise ExpectationMismatchError(label, expected, actual)


def test_safe_call_and_try_emit() -> None:
    calls: list[str] = []

    def raise_error() -> None:
        error_message = "boom"
        raise RuntimeError(error_message)

    def record_success() -> None:
        calls.append("success")

    def should_not_run() -> None:
        calls.append("unreachable")

    safe_call_attr = "_safe_call"
    try_emit_attr = "_try_emit"

    safe_call = cast(
        "Callable[[Callable[[], None]], bool]", getattr(module, safe_call_attr)
    )
    try_emit = cast("TryEmit", getattr(module, try_emit_attr))

    expect(
        not safe_call(raise_error),
        "safe_call should return False on exceptions",
    )
    expect(safe_call(record_success), "safe_call should return True on success")
    expect_equal(calls, ["success"], label="calls after safe_call")

    calls.clear()
    try_emit(raise_error, record_success, should_not_run)
    expect_equal(calls, ["success"], label="calls after try_emit")


def test_default_token_specs_include_slack() -> None:
    instance = x_cls_make_persistent_env_var_x(quiet=True)
    slack_spec = next(
        (spec for spec in instance.token_specs if spec.name == "SLACK_TOKEN"),
        None,
    )
    expect(slack_spec is not None, "Slack token spec should be present")
    if slack_spec is None:  # pragma: no cover - defensive narrow for type checkers
        error_message = "Slack token spec unexpectedly missing"
        raise RuntimeError(error_message)
    expect(slack_spec.required, "Slack token must be marked as required")


def test_default_token_specs_include_slack_bot_token() -> None:
    instance = x_cls_make_persistent_env_var_x(quiet=True)
    slack_bot_spec = next(
        (spec for spec in instance.token_specs if spec.name == "SLACK_BOT_TOKEN"),
        None,
    )
    expect(slack_bot_spec is not None, "Slack bot token spec should be present")
    if slack_bot_spec is None:  # pragma: no cover - defensive narrow for type checkers
        error_message = "Slack bot token spec unexpectedly missing"
        raise RuntimeError(error_message)
    expect(
        not slack_bot_spec.required,
        "Slack bot token must remain optional for future workflows",
    )


def test_default_token_specs_include_copilot_pat() -> None:
    instance = x_cls_make_persistent_env_var_x(quiet=True)
    copilot_spec = next(
        (spec for spec in instance.token_specs if spec.name == "COPILOT_REQUESTS_PAT"),
        None,
    )
    expect(copilot_spec is not None, "Copilot Requests PAT spec should be present")
    if copilot_spec is None:  # pragma: no cover - defensive narrow for type checkers
        error_message = "Copilot Requests PAT spec unexpectedly missing"
        raise RuntimeError(error_message)
    expect(copilot_spec.required, "Copilot Requests PAT must be marked as required")


def test_persist_current_sets_present_variables() -> None:
    state: dict[str, str] = {}
    tokens: list[tuple[str, str]] = [("FOO", "Foo token")]

    def fake_run(command: str) -> subprocess.CompletedProcess[str]:
        parts = command.split('"')
        if "SetEnvironmentVariable" in command:
            state[parts[1]] = parts[3]
            return subprocess.CompletedProcess(
                ["powershell", "-Command", command],
                returncode=0,
                stdout="",
                stderr="",
            )
        if "GetEnvironmentVariable" in command:
            value = state.get(parts[1], "")
            return subprocess.CompletedProcess(
                ["powershell", "-Command", command],
                returncode=0,
                stdout=value,
                stderr="",
            )
        unexpected_command = f"Unexpected command: {command}"
        raise AssertionError(unexpected_command)

    with (
        override_environ({"FOO": "secret"}),
        patch.object(
            x_cls_make_persistent_env_var_x,
            "run_powershell",
            new=staticmethod(fake_run),
        ),
    ):
        inst = x_cls_make_persistent_env_var_x(tokens=tokens, quiet=True)
        exit_code = inst.persist_current()

    expect(exit_code == 0, "persist_current should succeed for present variable")
    expect_equal(state.get("FOO"), "secret", label="persisted FOO value")


def test_persist_current_skips_missing_variables() -> None:
    tokens: list[tuple[str, str]] = [("FOO", "Foo token")]

    def raise_run(command: str) -> subprocess.CompletedProcess[str]:
        raise AssertionError(command)

    with (
        override_environ({}),
        patch.object(
            x_cls_make_persistent_env_var_x,
            "run_powershell",
            new=staticmethod(raise_run),
        ),
    ):
        inst = x_cls_make_persistent_env_var_x(tokens=tokens, quiet=True)
        exit_code = inst.persist_current()

    expect(
        exit_code == MISSING_EXIT_CODE,
        "persist_current should return 2 for missing variable",
    )


def test_main_json_persist_values_success() -> None:
    state: dict[str, str] = {}

    def fake_run(command: str) -> subprocess.CompletedProcess[str]:
        parts = command.split('"')
        if "SetEnvironmentVariable" in command:
            state[parts[1]] = parts[3]
            return subprocess.CompletedProcess(
                ["powershell", "-Command", command],
                returncode=0,
                stdout="",
                stderr="",
            )
        if "GetEnvironmentVariable" in command:
            value = state.get(parts[1], "")
            return subprocess.CompletedProcess(
                ["powershell", "-Command", command],
                returncode=0,
                stdout=value,
                stderr="",
            )
        unexpected = f"Unexpected command: {command}"
        raise AssertionError(unexpected)

    with patch.object(
        x_cls_make_persistent_env_var_x,
        "run_powershell",
        new=staticmethod(fake_run),
    ):
        payload = {
            "command": "x_make_persistent_env_var_x",
            "parameters": {
                "action": "persist-values",
                "tokens": [
                    {"name": "ALPHA", "label": "Alpha", "required": True},
                    {"name": "BETA", "label": "Beta", "required": False},
                ],
                "values": {"ALPHA": "value-alpha", "BETA": "value-beta"},
                "include_existing": True,
            },
        }
        result = main_json(payload)

    expect_equal(result.get("status"), "success", label="result status")
    summary = cast("dict[str, object]", result.get("summary", {}))
    exit_code = summary.get("exit_code")
    expect_equal(exit_code, 0, label="summary exit_code")
    expect_equal(state.get("ALPHA"), "value-alpha", label="persisted ALPHA")
    expect_equal(state.get("BETA"), "value-beta", label="persisted BETA")


def test_main_json_invalid_payload_returns_failure() -> None:
    payload = {
        "command": "x_make_persistent_env_var_x",
        "parameters": {"action": "unsupported"},
    }
    result = main_json(payload)

    expect_equal(result.get("status"), "failure", label="failure status")
    expect_equal(result.get("exit_code"), 2, label="failure exit code")


@contextmanager
def override_environ(values: Mapping[str, str]) -> Iterator[None]:
    original = dict(os.environ)
    os.environ.clear()
    os.environ.update(values)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)
