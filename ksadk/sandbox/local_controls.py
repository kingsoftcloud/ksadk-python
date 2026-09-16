"""Risk controls shared by same-container local process backends.

These checks and limits reduce common mistakes in trusted or semi-trusted code.
They are deliberately not presented as a security sandbox.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from selectors import EVENT_READ, DefaultSelector

from ksadk._process import terminate_process_group

_CONTROL_PREFIX = "KSADK_LOCAL_PROCESS_"
CONTROL_ENABLED_ENV = f"{_CONTROL_PREFIX}CONTROLS"
ENV_ALLOWLIST_ENV = f"{_CONTROL_PREFIX}ENV_ALLOWLIST"
CPU_SECONDS_ENV = f"{_CONTROL_PREFIX}CPU_SECONDS"
ADDRESS_SPACE_BYTES_ENV = f"{_CONTROL_PREFIX}ADDRESS_SPACE_BYTES"
MAX_PROCESSES_ENV = f"{_CONTROL_PREFIX}MAX_PROCESSES"
MAX_OPEN_FILES_ENV = f"{_CONTROL_PREFIX}MAX_OPEN_FILES"
MAX_FILE_BYTES_ENV = f"{_CONTROL_PREFIX}MAX_FILE_BYTES"
MAX_OUTPUT_BYTES_ENV = f"{_CONTROL_PREFIX}MAX_OUTPUT_BYTES"
WALL_SECONDS_ENV = f"{_CONTROL_PREFIX}WALL_SECONDS"

_DEFAULTS = {
    CPU_SECONDS_ENV: 120,
    ADDRESS_SPACE_BYTES_ENV: 1024 * 1024 * 1024,
    MAX_PROCESSES_ENV: 64,
    MAX_OPEN_FILES_ENV: 256,
    MAX_FILE_BYTES_ENV: 64 * 1024 * 1024,
    MAX_OUTPUT_BYTES_ENV: 1024 * 1024,
}
_BASE_ENV_NAMES = ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT")
_BLOCKED_SHELL_COMMANDS = frozenset({"kill", "killall", "nohup", "pkill", "setsid"})
_SCRIPT_SUFFIXES = frozenset({".py", ".sh", ".bash"})


@dataclass(frozen=True)
class PolicyFinding:
    rule: str
    detail: str
    path: str = ""
    line: int | None = None

    def reason(self) -> str:
        location = f" ({Path(self.path).name}:{self.line})" if self.path and self.line else ""
        return f"{self.rule}: {self.detail}{location}"


@dataclass(frozen=True)
class LocalControlSettings:
    cpu_seconds: int = _DEFAULTS[CPU_SECONDS_ENV]
    address_space_bytes: int = _DEFAULTS[ADDRESS_SPACE_BYTES_ENV]
    max_processes: int = _DEFAULTS[MAX_PROCESSES_ENV]
    max_open_files: int = _DEFAULTS[MAX_OPEN_FILES_ENV]
    max_file_bytes: int = _DEFAULTS[MAX_FILE_BYTES_ENV]
    max_output_bytes: int = _DEFAULTS[MAX_OUTPUT_BYTES_ENV]
    wall_seconds: int = 900
    extra_env_names: tuple[str, ...] = ()

    @classmethod
    def from_trusted_host_env(cls) -> "LocalControlSettings":
        values = {
            name: _positive_int_from_env(name, default) for name, default in _DEFAULTS.items()
        }
        return cls(
            cpu_seconds=values[CPU_SECONDS_ENV],
            address_space_bytes=values[ADDRESS_SPACE_BYTES_ENV],
            max_processes=values[MAX_PROCESSES_ENV],
            max_open_files=values[MAX_OPEN_FILES_ENV],
            max_file_bytes=values[MAX_FILE_BYTES_ENV],
            max_output_bytes=values[MAX_OUTPUT_BYTES_ENV],
            wall_seconds=_positive_int_from_env(WALL_SECONDS_ENV, 900),
            extra_env_names=_parse_env_names(os.environ.get(ENV_ALLOWLIST_ENV, "")),
        )

    @classmethod
    def from_runtime_env(cls) -> "LocalControlSettings":
        def value(name: str) -> int:
            raw = os.environ.get(name)
            if raw is None:
                raise ValueError(f"Missing trusted local-process control: {name}")
            parsed = int(raw)
            if parsed < 1:
                raise ValueError(f"Invalid trusted local-process control: {name}")
            return parsed

        return cls(
            cpu_seconds=value(CPU_SECONDS_ENV),
            address_space_bytes=value(ADDRESS_SPACE_BYTES_ENV),
            max_processes=value(MAX_PROCESSES_ENV),
            max_open_files=value(MAX_OPEN_FILES_ENV),
            max_file_bytes=value(MAX_FILE_BYTES_ENV),
            max_output_bytes=value(MAX_OUTPUT_BYTES_ENV),
            wall_seconds=value(WALL_SECONDS_ENV),
            extra_env_names=_parse_env_names(os.environ.get(ENV_ALLOWLIST_ENV, "")),
        )

    def runtime_environment(self) -> dict[str, str]:
        return {
            CONTROL_ENABLED_ENV: "1",
            ENV_ALLOWLIST_ENV: ",".join(self.extra_env_names),
            CPU_SECONDS_ENV: str(self.cpu_seconds),
            ADDRESS_SPACE_BYTES_ENV: str(self.address_space_bytes),
            MAX_PROCESSES_ENV: str(self.max_processes),
            MAX_OPEN_FILES_ENV: str(self.max_open_files),
            MAX_FILE_BYTES_ENV: str(self.max_file_bytes),
            MAX_OUTPUT_BYTES_ENV: str(self.max_output_bytes),
            WALL_SECONDS_ENV: str(self.wall_seconds),
        }

    def launcher_arguments(self) -> list[str]:
        payload = json.dumps(
            {
                "cpu_seconds": self.cpu_seconds,
                "address_space_bytes": self.address_space_bytes,
                "max_processes": self.max_processes,
                "max_open_files": self.max_open_files,
                "max_file_bytes": self.max_file_bytes,
            },
            sort_keys=True,
        )
        launcher_path = str(Path(__file__).with_name("local_launcher.py").resolve())
        return [sys.executable, "-I", "-S", launcher_path, "--limits", payload]


@dataclass
class BoundedProcessResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    output_limit_exceeded: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    cleanup_error: str | None = None
    controls: dict[str, object] = field(default_factory=dict)


def controls_enabled() -> bool:
    return os.environ.get(CONTROL_ENABLED_ENV) == "1"


def build_script_environment(
    source: dict[str, str],
    *,
    settings: LocalControlSettings,
    required: dict[str, str] | None = None,
) -> dict[str, str]:
    allowed_names = set(_BASE_ENV_NAMES) | set(settings.extra_env_names)
    result = {name: str(source[name]) for name in allowed_names if name in source}
    result.setdefault("PATH", os.defpath)
    result.setdefault("LANG", "C.UTF-8")
    result["CI"] = "1"
    result.update({name: str(value) for name, value in (required or {}).items()})
    return result


def check_command(
    args: list[str] | str,
    *,
    cwd: Path,
    allowed_roots: tuple[Path, ...],
) -> PolicyFinding | None:
    roots = tuple(root.expanduser().resolve() for root in allowed_roots)
    if isinstance(args, str):
        finding = _check_shell_text(args, path=None, roots=roots, cwd=cwd.resolve())
        if finding:
            return finding
        tokens = _shell_tokens(args)
    else:
        tokens = [str(item) for item in args]
    finding = _check_tokens(tokens, roots=roots, cwd=cwd.resolve(), path=None, line=None)
    if finding:
        return finding
    return _scan_direct_entry_scripts(tokens, cwd=cwd.resolve(), roots=roots)


def run_bounded_process(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int | float | None,
    settings: LocalControlSettings,
    start_new_session: bool,
) -> BoundedProcessResult:
    read_fd, write_fd = os.pipe()
    launcher = settings.launcher_arguments() + ["--report-fd", str(write_fd), "--", *args]
    process: subprocess.Popen[bytes] | None = None
    stdout = bytearray()
    stderr = bytearray()
    stdout_truncated = False
    stderr_truncated = False
    timed_out = False
    output_limited = False
    cleanup_error: str | None = None
    started = time.monotonic()
    selector = DefaultSelector()
    try:
        process = subprocess.Popen(
            launcher,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=start_new_session,
            pass_fds=(write_fd,),
        )
    except BaseException:
        os.close(read_fd)
        selector.close()
        raise
    finally:
        os.close(write_fd)
    try:
        assert process.stdout is not None and process.stderr is not None
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, EVENT_READ)
        while True:
            elapsed = time.monotonic() - started
            if timeout is not None and elapsed >= timeout:
                timed_out = True
                break
            for key, _ in selector.select(0.05):
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = stdout if key.fileobj is process.stdout else stderr
                remaining = settings.max_output_bytes - len(target)
                if remaining > 0:
                    target.extend(chunk[:remaining])
                if len(chunk) > max(remaining, 0):
                    if key.fileobj is process.stdout:
                        stdout_truncated = True
                    else:
                        stderr_truncated = True
                    output_limited = True
            if output_limited or process.poll() is not None:
                break
        if timed_out or output_limited:
            if start_new_session:
                cleanup_error = terminate_process_group(process)
            else:
                _terminate_direct_child(process)
        elif start_new_session:
            cleanup_error = terminate_process_group(process)
        else:
            process.wait(timeout=1)
        stdout_truncated = (
            _drain_ready(process.stdout, stdout, settings.max_output_bytes) or stdout_truncated
        )
        stderr_truncated = (
            _drain_ready(process.stderr, stderr, settings.max_output_bytes) or stderr_truncated
        )
        output_limited = output_limited or stdout_truncated or stderr_truncated
        # The launcher closes its report descriptor before exec. Read the tiny
        # report only after the bounded command lifecycle has completed so a
        # stalled launcher cannot suspend wall-time or output enforcement.
        report = _read_control_report(read_fd)
    finally:
        os.close(read_fd)
        selector.close()
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
    exit_code = process.returncode
    if output_limited:
        exit_code = 122
    return BoundedProcessResult(
        stdout=bytes(stdout).decode("utf-8", errors="replace"),
        stderr=bytes(stderr).decode("utf-8", errors="replace"),
        exit_code=exit_code,
        timed_out=timed_out,
        output_limit_exceeded=output_limited,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        cleanup_error=cleanup_error,
        controls=report,
    )


def _positive_int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _parse_env_names(raw: str) -> tuple[str, ...]:
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"Invalid environment allowlist name: {name}")
        if name.startswith(_CONTROL_PREFIX):
            raise ValueError("Local-process control variables cannot be passed to Skill scripts")
        if name not in names:
            names.append(name)
    return tuple(names)


def _shell_tokens(text: str) -> list[str]:
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars="();<>|&")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        return list(lexer)
    except ValueError:
        return []


def _check_shell_text(
    text: str,
    *,
    path: Path | None,
    roots: tuple[Path, ...],
    cwd: Path,
) -> PolicyFinding | None:
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = _shell_tokens(line)
        if _contains_background_operator(line):
            return PolicyFinding(
                "shell.background_process",
                "background process syntax is not allowed",
                str(path or ""),
                line_number,
            )
        finding = _check_tokens(
            tokens,
            roots=roots,
            cwd=cwd,
            path=path,
            line=line_number,
        )
        if finding:
            return finding
    return None


def _check_tokens(
    tokens: list[str],
    *,
    roots: tuple[Path, ...],
    cwd: Path,
    path: Path | None,
    line: int | None,
) -> PolicyFinding | None:
    for index in sorted(_command_positions(tokens)):
        command = Path(tokens[index]).name.lower()
        arguments = tokens[index + 1 : _command_end(tokens, index + 1)]
        if command in _BLOCKED_SHELL_COMMANDS:
            return PolicyFinding(
                f"shell.blocked_command.{command}",
                f"command {command!r} is not allowed by the local-process policy",
                str(path or ""),
                line,
            )
        if command == "rm" and _rm_is_recursive_force(arguments):
            return PolicyFinding(
                "shell.recursive_force_remove",
                "recursive forced removal is not allowed",
                str(path or ""),
                line,
            )
        if _is_python_command(command) and "-c" in arguments:
            option_index = arguments.index("-c")
            if option_index + 1 >= len(arguments):
                continue
            finding = _check_python(
                arguments[option_index + 1],
                path or Path("<command>"),
                roots,
                cwd,
            )
            if finding:
                return finding
        if _is_shell_command(command) and "-c" in arguments:
            option_index = arguments.index("-c")
            if option_index + 1 >= len(arguments):
                continue
            finding = _check_shell_text(
                arguments[option_index + 1],
                path=path,
                roots=roots,
                cwd=cwd,
            )
            if finding:
                return finding
    return None


def _scan_direct_entry_scripts(
    tokens: list[str], *, cwd: Path, roots: tuple[Path, ...]
) -> PolicyFinding | None:
    for index in sorted(_command_positions(tokens)):
        command_token = tokens[index]
        command = Path(command_token).name.lower()
        arguments = tokens[index + 1 : _command_end(tokens, index + 1)]
        script_token = _direct_entry_script(command_token, command, arguments)
        if script_token is None:
            continue
        candidate = Path(script_token)
        if not candidate.is_absolute():
            candidate = cwd / script_token
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved.is_file() or not _inside_any(resolved, roots):
            continue
        try:
            content = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if resolved.suffix.lower() == ".py":
            finding = _check_python(content, resolved, roots, cwd)
        else:
            finding = _check_shell_text(content, path=resolved, roots=roots, cwd=cwd)
        if finding:
            return finding
    return None


def _direct_entry_script(command_token: str, command: str, arguments: list[str]) -> str | None:
    if Path(command_token).suffix.lower() in _SCRIPT_SUFFIXES:
        return command_token
    if not (_is_python_command(command) or _is_shell_command(command)):
        return None
    if "-c" in arguments or "-m" in arguments:
        return None
    for argument in arguments:
        if argument == "--":
            continue
        if argument.startswith("-"):
            continue
        return argument if Path(argument).suffix.lower() in _SCRIPT_SUFFIXES else None
    return None


def _check_python(
    source: str,
    path: Path,
    roots: tuple[Path, ...],
    cwd: Path,
) -> PolicyFinding | None:
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return None
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func, aliases)
        if name in {"os.kill", "os.killpg", "os.setsid"}:
            return PolicyFinding(
                "python.process_control",
                f"call to {name} is not allowed",
                str(path),
                node.lineno,
            )
        if name in {"os.popen", "os.system"} and node.args:
            shell_source = _literal_string(node.args[0])
            if shell_source is not None:
                finding = _check_shell_text(
                    shell_source,
                    path=path,
                    roots=roots,
                    cwd=cwd,
                )
                if finding:
                    return finding
        if name in {
            "subprocess.call",
            "subprocess.check_call",
            "subprocess.check_output",
            "subprocess.Popen",
            "subprocess.run",
        }:
            if name == "subprocess.Popen" and _keyword_is_literal_true(node, "start_new_session"):
                return PolicyFinding(
                    "python.detached_process",
                    "subprocess start_new_session=True is not allowed",
                    str(path),
                    node.lineno,
                )
            command = _literal_subprocess_command(node.args[0]) if node.args else None
            if isinstance(command, list):
                finding = _check_tokens(
                    command,
                    roots=roots,
                    cwd=cwd,
                    path=path,
                    line=node.lineno,
                )
                if finding:
                    return finding
            elif isinstance(command, str) and _keyword_is_literal_true(node, "shell"):
                finding = _check_shell_text(
                    command,
                    path=path,
                    roots=roots,
                    cwd=cwd,
                )
                if finding:
                    return finding
        if name == "shutil.rmtree" and node.args:
            value = _literal_string(node.args[0])
            if value is not None:
                candidate = Path(value).expanduser()
                if not candidate.is_absolute():
                    candidate = cwd / candidate
                if not _inside_any(candidate, roots):
                    return PolicyFinding(
                        "path.destructive_outside_workspace",
                        f"shutil.rmtree target {value!r} is outside the allowed roots",
                        str(path),
                        node.lineno,
                    )
    return None


def _call_name(node: ast.expr, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _literal_string(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_subprocess_command(node: ast.expr) -> list[str] | str | None:
    text = _literal_string(node)
    if text is not None:
        return text
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    result: list[str] = []
    for item in node.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            return None
        result.append(item.value)
    return result


def _keyword_is_literal_true(node: ast.Call, name: str) -> bool:
    return any(
        keyword.arg == name
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in node.keywords
    )


def _is_python_command(command: str) -> bool:
    return command == "python" or command.startswith("python3")


def _is_shell_command(command: str) -> bool:
    return command in {"bash", "dash", "sh"}


def _inside_any(path: Path, roots: tuple[Path, ...]) -> bool:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    return any(resolved == root or root in resolved.parents for root in roots)


def _terminate_direct_child(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def _drain_ready(stream: object, target: bytearray, limit: int) -> bool:
    descriptor = stream.fileno()  # type: ignore[attr-defined]
    truncated = False
    # A descendant may retain and continuously write the descriptor. Never wait
    # for EOF after the direct child exits.
    deadline = time.monotonic() + 0.05
    while time.monotonic() < deadline:
        remaining = limit - len(target)
        try:
            chunk = os.read(descriptor, min(65536, max(remaining + 1, 1)))
        except BlockingIOError:
            break
        if not chunk:
            break
        if remaining > 0:
            target.extend(chunk[:remaining])
        if len(chunk) > max(remaining, 0):
            truncated = True
            break
    else:
        # The deadline was consumed by a writer that kept the inherited pipe
        # active, so the returned capture is necessarily incomplete.
        truncated = True
    return truncated


def _rm_is_recursive_force(tokens: list[str]) -> bool:
    options = ""
    for token in tokens:
        if token in {";", "&&", "||", "|"}:
            break
        if token == "--":
            break
        if token.startswith("-"):
            options += token.lstrip("-")
    return "r" in options.lower() and "f" in options.lower()


def _command_positions(tokens: list[str]) -> set[int]:
    positions: set[int] = set()
    expect_command = True
    for index, token in enumerate(tokens):
        if token in {";", "&&", "||", "|", "(", ")"}:
            expect_command = True
            continue
        if expect_command and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
            continue
        if expect_command:
            positions.add(index)
            expect_command = False
    return positions


def _command_end(tokens: list[str], start: int) -> int:
    for index in range(start, len(tokens)):
        if tokens[index] in {";", "&&", "||", "|", ")"}:
            return index
    return len(tokens)


def _contains_background_operator(line: str) -> bool:
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "&":
            previous = line[index - 1] if index else ""
            following = line[index + 1] if index + 1 < len(line) else ""
            if previous not in {"&", ">"} and following != "&":
                return True
    return False


def _read_control_report(read_fd: int) -> dict[str, object]:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(read_fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    if not chunks:
        return {"status": "failed", "failed": ["launcher_report_missing"]}
    try:
        payload = json.loads(b"".join(chunks))
    except json.JSONDecodeError:
        return {"status": "failed", "failed": ["launcher_report_invalid"]}
    return (
        payload
        if isinstance(payload, dict)
        else {"status": "failed", "failed": ["launcher_report_invalid"]}
    )


def resource_limit_reason(exit_code: int | None) -> str | None:
    if exit_code is None or exit_code >= 0:
        return None
    signal_number = -exit_code
    if signal_number == getattr(signal, "SIGXCPU", -1):
        return "cpu_time"
    if signal_number == getattr(signal, "SIGXFSZ", -1):
        return "file_size"
    return None
