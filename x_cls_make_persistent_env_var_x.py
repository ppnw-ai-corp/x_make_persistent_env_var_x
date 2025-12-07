from __future__ import annotations

import argparse
import getpass
import hashlib
import importlib
import json
import logging
import os
import shutil
import subprocess
import sys as _sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol, TypeVar, cast

from x_make_common_x.json_contracts import validate_payload

from x_make_persistent_env_var_x.json_contracts import (
    ERROR_SCHEMA,
    INPUT_SCHEMA,
    OUTPUT_SCHEMA,
)

if TYPE_CHECKING:
    import tkinter as tk
    from tkinter import messagebox
else:  # pragma: no cover - import guard to support headless environments
    try:
        import tkinter as tk  # type: ignore[import-not-found]
        from tkinter import messagebox  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        tk = cast("None", None)
        messagebox = cast("None", None)
    else:
        tk = cast("object", tk)
        messagebox = cast("object", messagebox)


class _TkRootProtocol(Protocol):
    def title(self, title: str) -> None: ...

    def geometry(self, geometry: str) -> None: ...

    def resizable(self, width: bool, height: bool) -> None: ...  # noqa: FBT001

    def protocol(self, name: str, func: Callable[[], object]) -> None: ...

    def mainloop(self) -> None: ...

    def destroy(self) -> None: ...

    def quit(self) -> None: ...


class _TkHasGrid(Protocol):
    def grid(self, *args: object, **kwargs: object) -> None: ...


class _TkHasPack(Protocol):
    def pack(self, *args: object, **kwargs: object) -> None: ...


class _TkFrameProtocol(_TkHasGrid, _TkHasPack, Protocol):
    def columnconfigure(self, index: int, weight: int) -> None: ...


class _TkLabelProtocol(_TkHasGrid, Protocol):
    def configure(self, **kwargs: object) -> None: ...


class _TkEntryProtocol(_TkHasGrid, Protocol):
    def configure(self, **kwargs: object) -> None: ...

    def get(self) -> str: ...

    def insert(self, index: int, value: str) -> None: ...


class _TkBooleanVarProtocol(Protocol):
    def get(self) -> bool: ...

    def set(self, value: bool) -> None: ...  # noqa: FBT001


class _TkStringVarProtocol(Protocol):
    def set(self, value: str) -> None: ...


class _TkCheckbuttonProtocol(_TkHasGrid, Protocol):
    pass


class _TkButtonProtocol(_TkHasPack, Protocol):
    def focus_set(self) -> None: ...


_FactoryT_co = TypeVar("_FactoryT_co", covariant=True)


class _TkFactory(Protocol[_FactoryT_co]):
    def __call__(self, *args: object, **kwargs: object) -> _FactoryT_co: ...


class _TkModuleProtocol(Protocol):
    Tk: _TkFactory[_TkRootProtocol]
    Frame: _TkFactory[_TkFrameProtocol]
    Label: _TkFactory[_TkLabelProtocol]
    Entry: _TkFactory[_TkEntryProtocol]
    BooleanVar: _TkFactory[_TkBooleanVarProtocol]
    Checkbutton: _TkFactory[_TkCheckbuttonProtocol]
    Button: _TkFactory[_TkButtonProtocol]
    StringVar: _TkFactory[_TkStringVarProtocol]


class _MessageboxProtocol(Protocol):
    def showwarning(self, title: str, message: str) -> None: ...

    def showinfo(self, title: str, message: str) -> None: ...

    def showerror(self, title: str, message: str) -> None: ...


if TYPE_CHECKING:
    _TK_MODULE: _TkModuleProtocol | None = cast("_TkModuleProtocol | None", tk)
    _MESSAGEBOX_MODULE: _MessageboxProtocol | None = cast(
        "_MessageboxProtocol | None", messagebox
    )
elif tk is None or messagebox is None:
    _TK_MODULE = None
    _MESSAGEBOX_MODULE = None
else:
    _TK_MODULE = cast("_TkModuleProtocol", tk)
    _MESSAGEBOX_MODULE = cast("_MessageboxProtocol", messagebox)


class _SchemaValidationError(Exception):
    message: str
    path: tuple[object, ...]
    schema_path: tuple[object, ...]


class _JsonSchemaModule(Protocol):
    ValidationError: type[_SchemaValidationError]


def _load_validation_error() -> type[_SchemaValidationError]:
    module = cast("_JsonSchemaModule", importlib.import_module("jsonschema"))
    return module.ValidationError


ValidationErrorType: type[_SchemaValidationError] = _load_validation_error()

_LOGGER = logging.getLogger("x_make")


def _try_emit(*emitters: Callable[[], None]) -> None:
    for emit in emitters:
        if _safe_call(emit):
            break


def _safe_call(action: Callable[[], object]) -> bool:
    try:
        action()
    except Exception:  # noqa: BLE001 - defensive guard around logging fallbacks
        return False
    return True


def _info(*args: object) -> None:
    message = " ".join(str(arg) for arg in args)
    with suppress(Exception):
        _LOGGER.info("%s", message)

    def _print() -> None:
        print(message)

    def _write_stdout() -> None:
        _sys.stdout.write(f"{message}\n")

    _try_emit(_print, _write_stdout)


def _error(*args: object) -> None:
    message = " ".join(str(arg) for arg in args)
    with suppress(Exception):
        _LOGGER.error("%s", message)

    def _print_stderr() -> None:
        print(message, file=_sys.stderr)

    def _write_stderr() -> None:
        _sys.stderr.write(f"{message}\n")

    def _print_fallback() -> None:
        print(message)

    _try_emit(_print_stderr, _write_stderr, _print_fallback)


Token = tuple[str, str]


@dataclass(slots=True)
class TokenSpec:
    name: str
    label: str | None
    required: bool

    @property
    def display_label(self) -> str:
        return self.label or self.name


SCHEMA_VERSION = "x_make_persistent_env_var_x.run/1.0"


_DEFAULT_TOKEN_SPECS: tuple[TokenSpec, ...] = (
    TokenSpec(name="TESTPYPI_API_TOKEN", label="TestPyPI API Token", required=True),
    TokenSpec(name="PYPI_API_TOKEN", label="PyPI API Token", required=True),
    TokenSpec(name="GITHUB_TOKEN", label="GitHub Token", required=True),
    TokenSpec(name="COPILOT_REQUESTS_PAT", label="Copilot Requests PAT", required=True),
    TokenSpec(name="SLACK_TOKEN", label="Slack Token", required=True),
    TokenSpec(name="SLACK_BOT_TOKEN", label="Slack Bot Token", required=False),
)


_DEFAULT_TOKENS: tuple[Token, ...] = tuple(
    (spec.name, spec.display_label) for spec in _DEFAULT_TOKEN_SPECS
)


@dataclass(slots=True)
class _RunOutcome:
    action: str
    results: list[dict[str, object]]
    tokens_total: int
    tokens_modified: int
    tokens_skipped: int
    tokens_failed: int
    exit_code: int
    messages: list[str]
    snapshot: dict[str, object]


def _timestamp() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _hash_value(value: str | None) -> str | None:
    if not value:
        return None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:16]


def _should_redact(name: str) -> bool:
    upper_name = name.upper()
    sensitive_markers = ("TOKEN", "SECRET", "PASSWORD", "KEY", "API")
    return any(marker in upper_name for marker in sensitive_markers)


def _display_value(name: str, value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if _should_redact(name):
        return "<hidden>"
    return value


def _token_plural(count: int) -> str:
    return "" if count == 1 else "s"


def _format_token_message(template: str, count: int) -> str:
    return template.format(count=count, plural=_token_plural(count))


def _exit_code_for_current(tokens_modified: int, tokens_failed: int) -> int:
    if tokens_failed:
        return 1
    if tokens_modified:
        return 0
    return 2


def _exit_code_for_values(tokens_failed: int) -> int:
    return 1 if tokens_failed else 0


def _build_token_specs(raw: object) -> tuple[TokenSpec, ...]:
    if not isinstance(raw, Sequence):
        return _DEFAULT_TOKEN_SPECS
    specs: list[TokenSpec] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        name_obj = entry.get("name")
        if not isinstance(name_obj, str) or not name_obj:
            continue
        if name_obj in seen:
            continue
        label_obj = entry.get("label")
        label = label_obj if isinstance(label_obj, str) and label_obj else None
        required_obj = entry.get("required")
        required = bool(required_obj) if isinstance(required_obj, bool) else False
        specs.append(TokenSpec(name=name_obj, label=label, required=required))
        seen.add(name_obj)
    return tuple(specs) if specs else _DEFAULT_TOKEN_SPECS


def _token_tuples(specs: Sequence[TokenSpec]) -> tuple[Token, ...]:
    return tuple((spec.name, spec.display_label) for spec in specs)


def _normalize_values(raw: object) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str) and value
    }


def _failure_payload(
    message: str,
    *,
    exit_code: int | None = None,
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"status": "failure", "message": message}
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if details:
        payload["details"] = dict(details)
    with suppress(ValidationErrorType):
        validate_payload(payload, ERROR_SCHEMA)
    return payload


class x_cls_make_persistent_env_var_x:  # noqa: N801 - legacy public API
    """Persistent environment variable setter (Windows user scope)."""

    def __init__(
        self,
        var: str = "",
        value: str = "",
        *,
        quiet: bool = False,
        ctx: object | None = None,
        **token_options: object,
    ) -> None:
        self.var = var
        self.value = value
        self.quiet = quiet
        allowed_keys = {"tokens", "token_specs"}
        unexpected = set(token_options) - allowed_keys
        if unexpected:
            unexpected_keys = ", ".join(sorted(unexpected))
            message = f"Unexpected token option(s): {unexpected_keys}"
            raise TypeError(message)
        tokens = cast("Sequence[Token] | None", token_options.get("tokens"))
        token_specs = cast(
            "Sequence[TokenSpec] | None", token_options.get("token_specs")
        )
        if token_specs is not None:
            resolved_specs = tuple(token_specs)
        elif tokens is not None:
            resolved_specs = tuple(
                TokenSpec(name=token_name, label=token_label, required=True)
                for token_name, token_label in tokens
            )
        else:
            resolved_specs = _DEFAULT_TOKEN_SPECS
        self.tokens = _token_tuples(resolved_specs)
        self.token_specs = resolved_specs
        self._ctx = ctx

    @property
    def context(self) -> object | None:
        return self._ctx

    def _is_verbose(self) -> bool:
        attr: object = getattr(self._ctx, "verbose", False)
        if isinstance(attr, bool):
            return attr
        return bool(attr)

    def _should_report(self) -> bool:
        return not self.quiet and self._is_verbose()

    def set_user_env(self) -> bool:
        command = (
            "[Environment]::SetEnvironmentVariable("
            f'"{self.var}", "{self.value}", "User")'
        )
        result = self.run_powershell(command)
        return result.returncode == 0

    def get_user_env(self) -> str | None:
        command = "[Environment]::GetEnvironmentVariable(" f'"{self.var}", "User")'
        result = self.run_powershell(command)
        if result.returncode != 0:
            return None
        value = (result.stdout or "").strip()
        return value or None

    @staticmethod
    def run_powershell(command: str) -> subprocess.CompletedProcess[str]:
        powershell = shutil.which("powershell") or "powershell"
        return subprocess.run(  # noqa: S603
            [powershell, "-Command", command],
            check=False,
            capture_output=True,
            text=True,
        )

    def persist_current(self) -> int:
        any_changed = any(self._persist_one(var) for var, _label in self.tokens)

        if any_changed:
            if self._should_report():
                _info(
                    "Done. Open a NEW PowerShell window for changes to take effect in "
                    "new shells."
                )
            return 0
        if self._should_report():
            _info("No variables were persisted.")
        return 2

    def _persist_one(self, var: str) -> bool:
        val = os.environ.get(var)
        if not val:
            if self._should_report():
                _info(f"{var}: not present in current shell; skipping")
            return False
        setter = type(self)(
            var, val, quiet=self.quiet, tokens=self.tokens, ctx=self._ctx
        )
        ok = setter.set_user_env()
        if ok:
            if self._should_report():
                _info(
                    f"{var}: persisted to User environment (will appear in new shells)"
                )
            return True
        if self._should_report():
            _error(f"{var}: failed to persist to User environment")
        return False

    def apply_gui_values(
        self, values: Mapping[str, str]
    ) -> tuple[list[tuple[str, bool, str | None]], bool]:
        return self._apply_gui_values(values)

    def _apply_gui_values(
        self, values: Mapping[str, str]
    ) -> tuple[list[tuple[str, bool, str | None]], bool]:
        summaries: list[tuple[str, bool, str | None]] = []
        ok_all = True
        for var, _label in self.tokens:
            val = values.get(var, "")
            if not val:
                summaries.append((var, False, "<empty>"))
                ok_all = False
                continue
            obj = type(self)(
                var, val, quiet=self.quiet, tokens=self.tokens, ctx=self._ctx
            )
            ok = obj.set_user_env()
            stored = obj.get_user_env()
            summaries.append((var, ok, stored))
            if not (ok and stored == val):
                ok_all = False
        return summaries, ok_all

    def run_gui(self) -> int:
        tk_mod, messagebox_mod = _resolve_tkinter()
        if tk_mod is not None and messagebox_mod is not None:
            dialog = _TokenDialog(
                controller=self,
                tk=tk_mod,
                messagebox=messagebox_mod,
            )
            return dialog.run()

        values = _prompt_for_values(self.tokens, quiet=self.quiet)
        if values is None:
            return self._abort_gui_run("No values captured; aborting.")
        if not values:
            return self._abort_gui_run("No values provided; aborting.")

        summaries, ok_all = self._apply_gui_values(values)
        self._report_gui_results(summaries)

        if not ok_all:
            if not self.quiet:
                _info("Some values were not set correctly.")
            return 1
        if not self.quiet:
            _info(
                "All values set. Open a NEW PowerShell window "
                "for changes to take effect."
            )
        return 0

    def _abort_gui_run(self, message: str) -> int:
        if not self.quiet:
            _info(message)
        return 2

    def _report_gui_results(
        self, summaries: Sequence[tuple[str, bool, str | None]]
    ) -> None:
        if self.quiet:
            return
        _info("Results:")
        for var, ok, stored in summaries:
            shown = "<not set>" if stored in {None, "", "<empty>"} else "<hidden>"
            _info(f"- {var}: set={'yes' if ok else 'no'} | stored={shown}")


def _resolve_tkinter() -> tuple[_TkModuleProtocol | None, _MessageboxProtocol | None]:
    return _TK_MODULE, _MESSAGEBOX_MODULE


def _prompt_for_values(
    tokens: Sequence[Token], *, quiet: bool
) -> dict[str, str] | None:
    if not quiet:
        print("GUI unavailable. Falling back to console prompts.")
        print(
            "Provide secrets for each token. Leave blank to skip and keep existing "
            "user-scoped values."
        )
    collected: dict[str, str] = {}
    capture_any = False
    for var, label in tokens:
        prompt = f"{label} ({var})?: "
        try:
            value = getpass.getpass(prompt)
        except (EOFError, KeyboardInterrupt):
            if not quiet:
                print("Aborted.")
            return None
        if value:
            collected[var] = value
            capture_any = True
    if not capture_any:
        return {}
    return collected


class _TokenDialog:
    """Encapsulate the Tkinter dialog orchestration."""

    def __init__(
        self,
        *,
        controller: x_cls_make_persistent_env_var_x,
        tk: _TkModuleProtocol,
        messagebox: _MessageboxProtocol,
    ) -> None:
        self._controller = controller
        self._tk = tk
        self._messagebox = messagebox
        self._exit_code = 2
        self._entries: dict[str, _TkEntryProtocol] = {}
        self._status_var: _TkStringVarProtocol | None = None
        self._status_label: _TkLabelProtocol | None = None
        self._show_var: _TkBooleanVarProtocol | None = None
        self._window: _TkRootProtocol | None = None
        self._frame: _TkFrameProtocol | None = None
        self._prefill = _collect_prefill(
            controller.tokens,
            ctx=controller.context,
            quiet=controller.quiet,
        )

    def run(self) -> int:
        tk_mod = self._tk
        root = tk_mod.Tk()
        root.title("Persist Environment Tokens")
        root.geometry("460x320")
        root.resizable(width=False, height=False)
        self._window = root

        frame = tk_mod.Frame(root, padx=16, pady=16)
        frame.pack(fill="both", expand=True)
        self._frame = frame

        self._build_form()
        root.protocol("WM_DELETE_WINDOW", self._handle_cancel)

        try:
            root.mainloop()
        finally:
            with suppress(Exception):
                root.destroy()

        return self._exit_code

    # --- construction helpers -------------------------------------------------

    def _build_form(self) -> None:
        if self._frame is None:
            message = "Dialog frame is not initialised"
            raise RuntimeError(message)
        frame = self._frame
        tk_mod = self._tk
        controller = self._controller

        for idx, spec in enumerate(controller.token_specs):
            label = tk_mod.Label(frame, text=spec.display_label)
            label.grid(row=idx, column=0, sticky="w", pady=4)

            entry = tk_mod.Entry(frame, show="*")
            entry.grid(row=idx, column=1, sticky="ew", pady=4)
            stored_value = self._prefill.get(spec.name)
            if stored_value:
                entry.insert(0, stored_value)
            self._entries[spec.name] = entry

        frame.columnconfigure(1, weight=1)
        self._build_visibility_control()
        self._build_status_area()
        self._build_button_row()

    def _build_visibility_control(self) -> None:
        tk_mod = self._tk
        if self._frame is None:
            message = "Dialog frame is not initialised"
            raise RuntimeError(message)
        toggle_row = len(self._controller.token_specs)
        self._show_var = tk_mod.BooleanVar(value=False)

        toggle = tk_mod.Checkbutton(
            self._frame,
            text="Show values",
            variable=self._show_var,
            command=self._toggle_visibility,
        )
        toggle.grid(
            row=toggle_row,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(8, 4),
        )

    def _build_status_area(self) -> None:
        tk_mod = self._tk
        status_row = len(self._controller.token_specs) + 1
        self._status_var = tk_mod.StringVar(value="")
        status_label = tk_mod.Label(
            self._frame,
            textvariable=self._status_var,
            fg="#555",
            wraplength=400,
            justify="left",
        )
        status_label.grid(
            row=status_row,
            column=0,
            columnspan=2,
            sticky="w",
        )
        self._status_label = status_label

    def _build_button_row(self) -> None:
        tk_mod = self._tk
        button_row = len(self._controller.token_specs) + 2
        if self._frame is None:
            message = "Dialog frame is not initialised"
            raise RuntimeError(message)
        frame = tk_mod.Frame(self._frame)
        frame.grid(
            row=button_row,
            column=0,
            columnspan=2,
            sticky="e",
            pady=(12, 0),
        )

        cancel_button = tk_mod.Button(frame, text="Cancel", command=self._handle_cancel)
        cancel_button.pack(side="right")

        persist_button = tk_mod.Button(
            frame,
            text="Set Tokens",
            command=self._handle_persist,
        )
        persist_button.pack(side="right", padx=(8, 0))
        persist_button.focus_set()

    # --- callbacks ------------------------------------------------------------

    def _toggle_visibility(self) -> None:
        if self._show_var is None:
            message = "Visibility toggle not initialised"
            raise RuntimeError(message)
        mask = "" if self._show_var.get() else "*"
        for entry in self._entries.values():
            entry.configure(show=mask)

    def _handle_cancel(self) -> None:
        self._finalize(2)

    def _handle_persist(self) -> None:
        self._show_status("")
        messagebox_mod = self._messagebox
        provided, backfill, missing = self._collect_inputs()

        if missing:
            messagebox_mod.showwarning(
                "Tokens required",
                "Provide values for: " + ", ".join(missing),
            )
            return

        if not provided and not backfill:
            messagebox_mod.showinfo(
                "No values provided",
                "Provide at least one token value before persisting.",
            )
            return

        aggregated_messages: list[str] = []
        had_failure = False

        if provided:
            ok, exit_code, messages = self._apply(
                action="persist-values",
                tokens=[
                    spec
                    for spec in self._controller.token_specs
                    if spec.name in provided
                ],
                values=provided,
            )
            if not ok:
                return
            aggregated_messages.extend(messages)
            had_failure = exit_code != 0

        if not had_failure and backfill:
            ok, exit_code, messages = self._apply(
                action="persist-current",
                tokens=[
                    spec
                    for spec in self._controller.token_specs
                    if spec.name in backfill
                ],
                values=None,
            )
            if not ok:
                return
            aggregated_messages.extend(messages)
            had_failure = exit_code != 0

        if had_failure:
            summary = aggregated_messages or [
                "Token persistence reported an error. Adjust the values and try again.",
            ]
            self._show_status("\n".join(summary), is_error=True)
            return

        success_messages = aggregated_messages or [
            (
                "Token persistence succeeded. "
                "Open a new PowerShell window for fresh shells."
            ),
        ]
        messagebox_mod.showinfo(
            "Tokens persisted",
            "\n".join(success_messages),
        )
        self._finalize(0)

    # --- internal helpers -----------------------------------------------------

    def _collect_inputs(self) -> tuple[dict[str, str], set[str], list[str]]:
        provided: dict[str, str] = {}
        session_backfill: set[str] = set()
        missing_required: list[str] = []

        for spec in self._controller.token_specs:
            value = self._entries[spec.name].get().strip()
            if value:
                provided[spec.name] = value
                continue
            session_value = os.environ.get(spec.name)
            if session_value:
                session_backfill.add(spec.name)
                continue
            if spec.required:
                display = spec.display_label or spec.name
                missing_required.append(display)

        return provided, session_backfill, missing_required

    def _apply(
        self,
        *,
        action: str,
        tokens: Sequence[TokenSpec],
        values: Mapping[str, str] | None,
    ) -> tuple[bool, int, list[str]]:
        parameters_payload: dict[str, object] = {
            "action": action,
            "tokens": [
                {
                    "name": spec.name,
                    "label": spec.display_label,
                    "required": spec.required,
                }
                for spec in tokens
            ],
            "quiet": self._controller.quiet,
            "include_existing": True,
        }
        if values is not None:
            parameters_payload["values"] = dict(values)

        payload: dict[str, object] = {
            "command": "x_make_persistent_env_var_x",
            "parameters": parameters_payload,
        }

        messagebox_mod = self._messagebox
        result = main_json(payload, ctx=self._controller.context)
        if result.get("status") != "success":
            message = (
                str(result.get("message"))
                if result.get("message")
                else "Token persistence failed."
            )
            details = result.get("details")
            if isinstance(details, Mapping):
                breakdown = ", ".join(
                    f"{key}: {value}" for key, value in details.items()
                )
                if breakdown:
                    message = f"{message}\n{breakdown}"
            messagebox_mod.showerror("Persistence failed", message)
            return False, 2, []

        summary = result.get("summary")
        exit_code = 1
        if isinstance(summary, Mapping):
            code_obj = summary.get("exit_code")
            if isinstance(code_obj, int):
                exit_code = code_obj
        messages: list[str] = []
        raw_messages = result.get("messages")
        if isinstance(raw_messages, Sequence):
            messages = [str(item) for item in raw_messages if item]
        return True, exit_code, messages

    def _show_status(self, message: str, *, is_error: bool = False) -> None:
        if self._status_var is None or self._status_label is None:
            error_message = "Status widgets not initialised"
            raise RuntimeError(error_message)
        self._status_var.set(message)
        self._status_label.configure(fg="#a33" if is_error else "#555")

    def _finalize(self, code: int) -> None:
        self._exit_code = code
        if self._window is not None:
            self._window.quit()


def _collect_prefill(
    tokens: Sequence[Token], *, ctx: object | None, quiet: bool
) -> dict[str, str]:
    prefill: dict[str, str] = {}
    for var, _label in tokens:
        cur = x_cls_make_persistent_env_var_x(
            var, quiet=quiet, tokens=tokens, ctx=ctx
        ).get_user_env()
        if cur:
            prefill[var] = cur
    return prefill


def _collect_user_environment(
    token_specs: Sequence[TokenSpec],
    *,
    quiet: bool,
    ctx: object | None,
) -> dict[str, str | None]:
    snapshot: dict[str, str | None] = {}
    token_pairs = _token_tuples(token_specs)
    for spec in token_specs:
        reader = x_cls_make_persistent_env_var_x(
            spec.name,
            "",
            quiet=quiet,
            tokens=token_pairs,
            token_specs=token_specs,
            ctx=ctx,
        )
        snapshot[spec.name] = reader.get_user_env()
    return snapshot


def _persist_current_for_spec(
    spec: TokenSpec,
    token_pairs: Sequence[Token],
    token_specs: Sequence[TokenSpec],
    *,
    quiet: bool,
    ctx: object | None,
) -> tuple[dict[str, object], int, int, int]:
    session_value = os.environ.get(spec.name)
    reader = x_cls_make_persistent_env_var_x(
        spec.name,
        "",
        quiet=quiet,
        tokens=token_pairs,
        token_specs=token_specs,
        ctx=ctx,
    )
    before = reader.get_user_env()

    if not session_value:
        missing_entry = {
            "name": spec.name,
            "label": spec.display_label,
            "status": "skipped",
            "attempted": False,
            "stored": _display_value(spec.name, before),
            "stored_hash": _hash_value(before),
            "message": "variable missing from current session",
            "changed": False,
        }
        return missing_entry, 0, 1, 0

    setter = x_cls_make_persistent_env_var_x(
        spec.name,
        session_value,
        quiet=quiet,
        tokens=token_pairs,
        token_specs=token_specs,
        ctx=ctx,
    )
    ok = setter.set_user_env()
    after = setter.get_user_env()
    entry: dict[str, object] = {
        "name": spec.name,
        "label": spec.display_label,
        "attempted": True,
        "stored": _display_value(spec.name, after),
        "stored_hash": _hash_value(after),
    }
    if not ok or after != session_value:
        entry.update(
            {
                "status": "failed",
                "message": "failed to persist value",
                "changed": False,
            }
        )
        return entry, 0, 0, 1

    changed = before != after
    entry.update(
        {
            "status": "persisted" if changed else "unchanged",
            "message": "updated" if changed else "already current",
            "changed": changed,
        }
    )
    return entry, int(changed), 0, 0


def _persist_value_for_spec(  # noqa: PLR0913 - persistence flow needs explicit context parameters
    spec: TokenSpec,
    provided: str | None,
    token_pairs: Sequence[Token],
    token_specs: Sequence[TokenSpec],
    *,
    quiet: bool,
    ctx: object | None,
) -> tuple[dict[str, object], int, int, int]:
    reader = x_cls_make_persistent_env_var_x(
        spec.name,
        "",
        quiet=quiet,
        tokens=token_pairs,
        token_specs=token_specs,
        ctx=ctx,
    )
    before = reader.get_user_env()
    entry: dict[str, object] = {
        "name": spec.name,
        "label": spec.display_label,
    }

    if not provided:
        status = "failed" if spec.required else "skipped"
        message = (
            "required value missing" if status == "failed" else "no value provided"
        )
        entry.update(
            {
                "status": status,
                "attempted": False,
                "stored": _display_value(spec.name, before),
                "stored_hash": _hash_value(before),
                "message": message,
                "changed": False,
            }
        )
        modified = 0
        skipped = int(status == "skipped")
        failed = int(status == "failed")
        return entry, modified, skipped, failed

    setter = x_cls_make_persistent_env_var_x(
        spec.name,
        provided,
        quiet=quiet,
        tokens=token_pairs,
        token_specs=token_specs,
        ctx=ctx,
    )
    ok = setter.set_user_env()
    after = setter.get_user_env()
    entry.update(
        {
            "attempted": True,
            "stored": _display_value(spec.name, after),
            "stored_hash": _hash_value(after),
        }
    )
    if not ok or after != provided:
        entry.update(
            {
                "status": "failed",
                "message": "failed to persist value",
                "changed": False,
            }
        )
        return entry, 0, 0, 1

    changed = before != after
    entry.update(
        {
            "status": "persisted" if changed else "unchanged",
            "message": "updated" if changed else "already current",
            "changed": changed,
        }
    )
    return entry, int(changed), 0, 0


def _perform_persist_current(
    token_specs: Sequence[TokenSpec],
    *,
    quiet: bool,
    include_existing: bool,
    ctx: object | None,
) -> _RunOutcome:
    token_specs = tuple(token_specs)
    token_pairs = _token_tuples(token_specs)
    results: list[dict[str, object]] = []
    tokens_modified = 0
    tokens_skipped = 0
    tokens_failed = 0

    for spec in token_specs:
        entry, modified, skipped, failed = _persist_current_for_spec(
            spec,
            token_pairs,
            token_specs,
            quiet=quiet,
            ctx=ctx,
        )
        results.append(entry)
        tokens_modified += modified
        tokens_skipped += skipped
        tokens_failed += failed

    exit_code = _exit_code_for_current(tokens_modified, tokens_failed)

    messages: list[str] = []
    if tokens_modified:
        messages.append(
            _format_token_message(
                "Persisted {count} token{plural} from session", tokens_modified
            )
        )
    if tokens_skipped:
        messages.append(
            _format_token_message(
                "Skipped {count} token{plural} (missing session value)",
                tokens_skipped,
            )
        )
    if tokens_failed:
        messages.append(
            _format_token_message(
                "Failed to persist {count} token{plural}", tokens_failed
            )
        )

    snapshot_user = _collect_user_environment(token_specs, quiet=quiet, ctx=ctx)
    snapshot: dict[str, object] = {
        "user": {
            name: _display_value(name, value) for name, value in snapshot_user.items()
        }
    }
    if include_existing:
        snapshot["session"] = {
            spec.name: _display_value(spec.name, os.environ.get(spec.name))
            for spec in token_specs
        }

    return _RunOutcome(
        action="persist-current",
        results=results,
        tokens_total=len(token_specs),
        tokens_modified=tokens_modified,
        tokens_skipped=tokens_skipped,
        tokens_failed=tokens_failed,
        exit_code=exit_code,
        messages=messages,
        snapshot=snapshot,
    )


def _perform_persist_values(
    token_specs: Sequence[TokenSpec],
    values: Mapping[str, str],
    *,
    quiet: bool,
    include_existing: bool,
    ctx: object | None,
) -> _RunOutcome:
    token_specs = tuple(token_specs)
    token_pairs = _token_tuples(token_specs)
    results: list[dict[str, object]] = []
    provided_redacted = {
        name: _display_value(name, value) for name, value in values.items()
    }

    tokens_modified = 0
    tokens_skipped = 0
    tokens_failed = 0

    for spec in token_specs:
        entry, modified, skipped, failed = _persist_value_for_spec(
            spec,
            values.get(spec.name),
            token_pairs,
            token_specs,
            quiet=quiet,
            ctx=ctx,
        )
        results.append(entry)
        tokens_modified += modified
        tokens_skipped += skipped
        tokens_failed += failed

    snapshot_user = _collect_user_environment(token_specs, quiet=quiet, ctx=ctx)
    snapshot: dict[str, object] = {
        "user": {
            name: _display_value(name, value) for name, value in snapshot_user.items()
        },
        "provided": provided_redacted,
    }
    if include_existing:
        snapshot["session"] = {
            spec.name: _display_value(spec.name, os.environ.get(spec.name))
            for spec in token_specs
        }

    exit_code = _exit_code_for_values(tokens_failed)

    messages: list[str] = []
    if tokens_modified:
        messages.append(
            _format_token_message("Persisted {count} token{plural}", tokens_modified)
        )
    if tokens_skipped:
        messages.append(
            _format_token_message("Skipped {count} token{plural}", tokens_skipped)
        )
    if tokens_failed:
        messages.append(
            _format_token_message(
                "Failed to persist {count} token{plural}", tokens_failed
            )
        )

    return _RunOutcome(
        action="persist-values",
        results=results,
        tokens_total=len(token_specs),
        tokens_modified=tokens_modified,
        tokens_skipped=tokens_skipped,
        tokens_failed=tokens_failed,
        exit_code=exit_code,
        messages=messages,
        snapshot=snapshot,
    )


def _perform_inspect(
    token_specs: Sequence[TokenSpec],
    *,
    quiet: bool,
    include_existing: bool,
    ctx: object | None,
) -> _RunOutcome:
    token_specs = tuple(token_specs)
    snapshot_user = _collect_user_environment(token_specs, quiet=quiet, ctx=ctx)
    results: list[dict[str, object]] = []
    for spec in token_specs:
        stored = snapshot_user.get(spec.name)
        results.append(
            {
                "name": spec.name,
                "label": spec.display_label,
                "status": "unchanged",
                "attempted": False,
                "stored": _display_value(spec.name, stored),
                "stored_hash": _hash_value(stored),
                "message": "inspected",
                "changed": False,
            }
        )

    snapshot: dict[str, object] = {
        "user": {
            name: _display_value(name, value) for name, value in snapshot_user.items()
        }
    }
    if include_existing:
        snapshot["session"] = {
            spec.name: _display_value(spec.name, os.environ.get(spec.name))
            for spec in token_specs
        }

    messages = ["Inspection completed"]

    return _RunOutcome(
        action="inspect",
        results=results,
        tokens_total=len(token_specs),
        tokens_modified=0,
        tokens_skipped=0,
        tokens_failed=0,
        exit_code=0,
        messages=messages,
        snapshot=snapshot,
    )


def main_json(
    payload: Mapping[str, object], *, ctx: object | None = None
) -> dict[str, object]:
    try:
        validate_payload(payload, INPUT_SCHEMA)
    except ValidationErrorType as exc:
        error = exc
        return _failure_payload(
            "input payload failed validation",
            exit_code=2,
            details={
                "error": error.message,
                "path": [str(part) for part in error.path],
                "schema_path": [str(part) for part in error.schema_path],
            },
        )

    parameters_obj = payload.get("parameters", {})
    parameters = cast("Mapping[str, object]", parameters_obj)

    action_obj = parameters.get("action")
    action = cast("str", action_obj)
    quiet_obj = parameters.get("quiet", False)
    quiet = bool(quiet_obj) if not isinstance(quiet_obj, bool) else quiet_obj
    include_existing_obj = parameters.get("include_existing", False)
    include_existing = (
        bool(include_existing_obj)
        if not isinstance(include_existing_obj, bool)
        else include_existing_obj
    )
    notes_obj = parameters.get("notes")
    notes = notes_obj if isinstance(notes_obj, str) and notes_obj else None

    token_specs = _build_token_specs(parameters.get("tokens"))
    values = _normalize_values(parameters.get("values"))

    if action == "persist-current":
        outcome = _perform_persist_current(
            token_specs,
            quiet=quiet,
            include_existing=include_existing,
            ctx=ctx,
        )
    elif action == "persist-values":
        outcome = _perform_persist_values(
            token_specs,
            values,
            quiet=quiet,
            include_existing=include_existing,
            ctx=ctx,
        )
    elif action == "inspect":
        outcome = _perform_inspect(
            token_specs,
            quiet=quiet,
            include_existing=include_existing,
            ctx=ctx,
        )
    else:  # pragma: no cover - schema restricts action values
        return _failure_payload(
            "unsupported action",
            exit_code=1,
            details={"action": action},
        )

    summary: dict[str, object] = {
        "action": outcome.action,
        "tokens_total": outcome.tokens_total,
        "tokens_modified": outcome.tokens_modified,
        "tokens_skipped": outcome.tokens_skipped,
        "tokens_failed": outcome.tokens_failed,
        "exit_code": outcome.exit_code,
        "quiet": quiet,
    }
    if include_existing:
        summary["include_existing"] = True

    snapshot = dict(outcome.snapshot)
    if notes:
        snapshot.setdefault("notes", notes)

    result: dict[str, object] = {
        "status": "success",
        "schema_version": SCHEMA_VERSION,
        "generated_at": _timestamp(),
        "summary": summary,
        "results": outcome.results,
        "messages": outcome.messages,
        "environment_snapshot": snapshot,
    }

    try:
        validate_payload(result, OUTPUT_SCHEMA)
    except ValidationErrorType as exc:
        error = exc
        return _failure_payload(
            "generated output failed schema validation",
            exit_code=1,
            details={
                "error": error.message,
                "path": [str(part) for part in error.path],
                "schema_path": [str(part) for part in error.schema_path],
            },
        )

    return result


def _load_json_payload(file_path: str | None) -> dict[str, object]:
    def _load_stream(stream: IO[str]) -> dict[str, object]:
        payload_obj: object = json.load(stream)
        if not isinstance(payload_obj, Mapping):
            message = "JSON payload must be a mapping"
            raise TypeError(message)
        typed_payload = cast("Mapping[str, object]", payload_obj)
        return dict(typed_payload)

    if file_path:
        with Path(file_path).open("r", encoding="utf-8") as handle:
            return _load_stream(handle)
    return _load_stream(_sys.stdin)


def _run_cli(args: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description="x_make_persistent_env_var_x runtime dispatcher"
    )
    parser.add_argument(
        "--launch-gui",
        action="store_true",
        help="Launch the Tkinter dialog instead of processing JSON payloads.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Read JSON payload from stdin.",
    )
    parser.add_argument(
        "--json-file",
        type=str,
        help="Path to JSON payload file.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress informational logging when launching the GUI.",
    )
    parsed = parser.parse_args(args)

    namespace = cast("Mapping[str, object]", vars(parsed))
    launch_gui = bool(namespace.get("launch_gui", False))
    read_from_stdin = bool(namespace.get("json", False))
    json_file_value = namespace.get("json_file")
    json_file = json_file_value if isinstance(json_file_value, str) else None
    quiet = bool(namespace.get("quiet", False))

    if launch_gui and (read_from_stdin or json_file):
        parser.error("--launch-gui cannot be combined with JSON input flags.")

    if launch_gui:
        runner = x_cls_make_persistent_env_var_x("", "", quiet=quiet)
        try:
            return runner.run_gui()
        except RuntimeError as exc:  # Handles missing Tkinter dependencies.
            _error(str(exc))
            return 1

    if not (read_from_stdin or json_file):
        parser.error("JSON input required. Use --json for stdin or --json-file <path>.")

    payload = _load_json_payload(None if read_from_stdin else json_file)
    payload.setdefault("command", "x_make_persistent_env_var_x")
    result = main_json(payload)
    json.dump(result, _sys.stdout, indent=2)
    _sys.stdout.write("\n")

    if result.get("status") == "success":
        summary = result.get("summary")
        if isinstance(summary, Mapping):
            exit_code_obj = summary.get("exit_code")
            if isinstance(exit_code_obj, int):
                return exit_code_obj
        return 0

    failure_exit_obj = result.get("exit_code")
    if isinstance(failure_exit_obj, int):
        return failure_exit_obj
    return 1


def run_cli(args: Sequence[str]) -> int:
    return _run_cli(list(args))


__all__ = ["main_json", "run_cli", "x_cls_make_persistent_env_var_x"]


if __name__ == "__main__":
    _sys.exit(_run_cli(_sys.argv[1:]))
