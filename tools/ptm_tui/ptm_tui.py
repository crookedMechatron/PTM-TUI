#!/usr/bin/env python3
"""Textual PTM diagnostic dashboard for remote BRP control over SSH."""

from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import json
import os
import posixpath
import re
import shlex
import shutil
import signal
import sys
import tempfile
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, RichLog, Select, Static


CURRENT_WARNING_AMPS = 7.5
CURRENT_OVERCURRENT_AMPS = 8.0
TELEMETRY_STALE_SECONDS = 2.0
LOG_MAX_LINES = 1000
STATUS_REFRESH_SECONDS = 1.0
HISTORY_LIMIT = 240
DEFAULT_TEST_CONFIG = Path(__file__).resolve().parent / "test_config.json"
MANUAL_BOT_ID = "__manual__"
REMOTE_CLI_DIR_COMMANDS = {"ptm", "pms", "telemetry"}
REMOTE_BASE_PATH = (
    "$PATH:/usr/local/bin/control_cli:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$HOME/bin"
)
IMPORTANT_SERVICES = [
    "bot_system.service",
    "camera_system.service",
    "signalling_server.service",
    "mosquitto.service",
    "apache2.service",
    "enable_vcan.service",
    "zed_x_daemon.service",
    "docker.service",
    "containerd.service",
    "NetworkManager.service",
    "wpa_supplicant.service",
    "ssh.service",
]


@dataclass(slots=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass(slots=True)
class ParsedTelemetry:
    currents: dict[str, float] | None = None
    positions: dict[str, float] | None = None
    pms: dict[str, Any] | None = None
    pms_fallback: dict[str, Any] | None = None
    real_sections: dict[str, dict[str, Any]] | None = None


@dataclass(slots=True)
class ServiceStatus:
    name: str
    active: str
    enabled: str


@dataclass(slots=True)
class CurrentThresholds:
    warning_amps: float = CURRENT_WARNING_AMPS
    overcurrent_amps: float = CURRENT_OVERCURRENT_AMPS


@dataclass(slots=True)
class BotEntry:
    id: str
    host: str
    user: str = "aceng"
    ssh_port: int = 22
    password: str | None = None
    identity_file: Path | None = None
    ssh_backend: Literal["auto", "openssh", "paramiko"] = "auto"
    accept_new_host_key: bool = False

    @property
    def label(self) -> str:
        return f"{self.id} / {self.host}"


def load_test_config(path: Path) -> CurrentThresholds:
    if not path.exists():
        return CurrentThresholds()
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    motor = raw.get("motor_current_thresholds", raw) if isinstance(raw, dict) else {}
    return CurrentThresholds(
        warning_amps=float(motor.get("warning_amps", CURRENT_WARNING_AMPS)),
        overcurrent_amps=float(motor.get("overcurrent_amps", CURRENT_OVERCURRENT_AMPS)),
    )


def load_bot_entries(path: Path) -> list[BotEntry]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, list):
        raise ValueError("Bot config must be a JSON array")
    entries: list[BotEntry] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("host"):
            continue
        entries.append(
            BotEntry(
                id=str(item.get("id") or item["host"]),
                host=str(item["host"]),
                user=str(item.get("user", "aceng")),
                ssh_port=int(item.get("ssh_port", 22)),
                password=item.get("password"),
                identity_file=Path(item["identity_file"]) if item.get("identity_file") else None,
                ssh_backend=item.get("ssh_backend", "auto"),
                accept_new_host_key=bool(item.get("accept_new_host_key", False)),
            )
        )
    return entries


def manual_bot_entry(args: argparse.Namespace) -> BotEntry:
    return BotEntry(
        id=MANUAL_BOT_ID,
        host=args.host,
        user=args.user,
        ssh_port=args.ssh_port,
        password=args.ssh_password,
        identity_file=args.identity_file,
        ssh_backend=args.ssh_backend,
        accept_new_host_key=args.accept_new_host_key,
    )


def apply_bot_entry_to_args(args: argparse.Namespace, bot: BotEntry) -> None:
    args.host = bot.host
    args.user = bot.user
    args.ssh_port = bot.ssh_port
    args.ssh_password = bot.password
    args.identity_file = bot.identity_file
    args.ssh_backend = bot.ssh_backend
    args.accept_new_host_key = bot.accept_new_host_key
    args.selected_bot_id = bot.id


def can_switch_bot(ptm_started_by_ui: bool) -> bool:
    return not ptm_started_by_ui


def append_history(history: deque[float], value: Any, limit: int = HISTORY_LIMIT) -> None:
    try:
        history.append(float(value))
    except (TypeError, ValueError):
        return
    while len(history) > limit:
        history.popleft()


def append_telemetry_histories(parsed: ParsedTelemetry, histories: dict[str, deque[float]], limit: int = HISTORY_LIMIT) -> None:
    if parsed.currents:
        for key in ["I1", "I2", "I3", "I4"]:
            if key in parsed.currents:
                append_history(histories[key], parsed.currents[key], limit)
    supply = parsed.pms_fallback or parsed.pms
    if supply:
        if "V" in supply and float(supply.get("V") or 0) != 0:
            append_history(histories["supply_v"], supply["V"], limit)
        if "I" in supply and float(supply.get("I") or 0) != 0:
            append_history(histories["supply_i"], supply["I"], limit)


def render_sparkline(values: list[float] | deque[float], width: int = 40) -> str:
    blocks = "▁▂▃▄▅▆▇█"
    if not values:
        return ""
    sampled = list(values)[-width:]
    low = min(sampled)
    high = max(sampled)
    if high == low:
        return blocks[0] * len(sampled)
    scale = (len(blocks) - 1) / (high - low)
    return "".join(blocks[int((value - low) * scale)] for value in sampled)


def parse_number(value: str) -> float | int | str | bool | None:
    value = value.strip().strip('"').strip("'")
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.lower() in {"none", "null"}:
        return None
    try:
        numeric = float(value)
    except ValueError:
        return value
    return int(numeric) if numeric.is_integer() else numeric


def parse_braced_key_values(text: str) -> dict[str, Any]:
    """Parse loose telemetry fragments like {I1:1.2}, {'I1': 1.2}, or JSON objects."""
    fragment = extract_braced_fragment(text)
    if not fragment:
        return {}

    try:
        decoded = json.loads(fragment)
        return decoded if isinstance(decoded, dict) else {}
    except json.JSONDecodeError:
        pass

    try:
        decoded = ast.literal_eval(normalise_pythonish_literals(fragment))
        return decoded if isinstance(decoded, dict) else {}
    except (SyntaxError, ValueError):
        pass

    normalised = quote_unquoted_keys(normalise_pythonish_literals(fragment))
    try:
        decoded = ast.literal_eval(normalised)
        return decoded if isinstance(decoded, dict) else {}
    except (SyntaxError, ValueError):
        pass

    content = fragment[1:-1].strip()
    if not content:
        return {}

    values: dict[str, Any] = {}
    for item in content.split(","):
        if ":" not in item:
            continue
        key, raw_value = item.split(":", 1)
        key = key.strip().strip('"').strip("'")
        if key:
            values[key] = parse_number(raw_value)
    return values


def extract_braced_fragment(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for index, char in enumerate(text[start:], start=start):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def normalise_pythonish_literals(text: str) -> str:
    text = re.sub(r"\btrue\b", "True", text, flags=re.IGNORECASE)
    text = re.sub(r"\bfalse\b", "False", text, flags=re.IGNORECASE)
    text = re.sub(r"\bnull\b", "None", text, flags=re.IGNORECASE)
    return text


def quote_unquoted_keys(text: str) -> str:
    return re.sub(r"([,{]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r"\1'\2':", text)


def numeric_subset(values: dict[str, Any], keys: list[str]) -> dict[str, float]:
    subset: dict[str, float] = {}
    for key in keys:
        value = values.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            # Values are displayed as reported; some BRP builds may emit mA rather than A.
            subset[key] = float(value)
        except (TypeError, ValueError):
            continue
    return subset


def parse_ptmcur_line(line: str) -> dict[str, float] | None:
    if "PTMcur:" not in line:
        return None
    values = numeric_subset(parse_braced_key_values(line.split("PTMcur:", 1)[1]), ["I1", "I2", "I3", "I4"])
    return values or None


def parse_ptmpos_line(line: str) -> dict[str, float] | None:
    if "PTMpos:" not in line:
        return None
    values = numeric_subset(parse_braced_key_values(line.split("PTMpos:", 1)[1]), ["P1", "P2", "P3", "P4"])
    return values or None


def parse_pmsptm_line(line: str) -> dict[str, Any] | None:
    if "PMSPTM:" not in line:
        return None
    values = parse_braced_key_values(line.split("PMSPTM:", 1)[1])
    if not values:
        values = parse_labeled_tuple_section(extract_named_section(line, "PMSPTM") or "")
        if "relayPTM" in values:
            values["relayPTM"] = bool(values["relayPTM"])
    return values or None


def parse_tuple_value(value_text: str) -> float | list[float]:
    parts = [part.strip() for part in value_text.split(",")]
    values = [float(part) for part in parts if part]
    if len(values) == 1:
        return values[0]
    return values


def parse_labeled_tuple_section(section_text: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for raw_value, label in re.findall(r"\(([^)]*)\)\s*([A-Za-z][A-Za-z0-9_]*)", section_text):
        try:
            parsed = parse_tuple_value(raw_value)
        except ValueError:
            continue
        if label in {"relayPTM", "relay1", "relay2"} and isinstance(parsed, (int, float)):
            values[label] = bool(parsed)
        else:
            values[label] = parsed
    return values


def extract_named_section(line: str, section_name: str) -> str | None:
    match = re.search(rf"(?:^|;)\s*{re.escape(section_name)}:\s*(.*?)(?=\s*;\s*[A-Za-z][A-Za-z0-9_ ]*:|$)", line)
    return match.group(1).strip() if match else None


def parse_pms_line(line: str) -> dict[str, Any] | None:
    section = extract_named_section(line, "PMS")
    if section is None:
        return None
    values = parse_labeled_tuple_section(section)
    return values or None


def parse_ptm_section(line: str) -> tuple[dict[str, float] | None, dict[str, float] | None]:
    section = extract_named_section(line, "PTM")
    if section is None:
        return None, None
    values = parse_labeled_tuple_section(section)
    currents = tuple_to_named_values(values.get("I"), "I")
    positions = tuple_to_named_values(values.get("P"), "P")
    return currents or None, positions or None


def tuple_to_named_values(value: Any, prefix: str) -> dict[str, float]:
    if isinstance(value, (int, float)):
        sequence = [float(value)]
    elif isinstance(value, list):
        sequence = [float(item) for item in value]
    else:
        return {}
    return {f"{prefix}{index + 1}": number for index, number in enumerate(sequence[:4])}


def pmsptm_supply_is_zero(values: dict[str, Any] | None) -> bool:
    if not values:
        return True
    for key in ["V", "I", "battery"]:
        try:
            if float(values.get(key, 0)) != 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def pms_has_supply(values: dict[str, Any] | None) -> bool:
    if not values:
        return False
    for key in ["V", "I", "battery"]:
        try:
            if float(values.get(key, 0)) != 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def parse_real_telemetry_line(line: str) -> ParsedTelemetry:
    pmsptm = parse_pmsptm_line(line)
    pms = parse_pms_line(line)
    currents, positions = parse_ptm_section(line)
    real_sections: dict[str, dict[str, Any]] = {}
    if pmsptm is not None:
        real_sections["PMSPTM"] = pmsptm
    if pms is not None:
        real_sections["PMS"] = pms
    if currents is not None or positions is not None:
        real_sections["PTM"] = {"currents": currents or {}, "positions": positions or {}}
    return ParsedTelemetry(
        currents=currents,
        positions=positions,
        pms=pmsptm,
        pms_fallback=pms if pmsptm_supply_is_zero(pmsptm) and pms_has_supply(pms) else None,
        real_sections=real_sections or None,
    )


def parse_telemetry_line(line: str) -> ParsedTelemetry:
    real = parse_real_telemetry_line(line)
    currents = real.currents or parse_ptmcur_line(line)
    positions = real.positions or parse_ptmpos_line(line)
    pms = real.pms or parse_pmsptm_line(line)
    return ParsedTelemetry(
        currents=currents,
        positions=positions,
        pms=pms,
        pms_fallback=real.pms_fallback,
        real_sections=real.real_sections,
    )


def parse_service_statuses(output: str) -> list[ServiceStatus]:
    services: list[ServiceStatus] = []
    pattern = re.compile(r"^(?P<name>\S+)\s+active=(?P<active>\S+)\s+enabled=(?P<enabled>\S+)")
    for line in output.splitlines():
        match = pattern.search(line.strip())
        if match:
            services.append(
                ServiceStatus(
                    name=match.group("name"),
                    active=match.group("active"),
                    enabled=match.group("enabled"),
                )
            )
    return services


def diagnostics_command() -> list[str]:
    quoted_services = " ".join(shlex.quote(service) for service in IMPORTANT_SERVICES)
    script = (
        f"for svc in {quoted_services}; do "
        "printf '%-32s active=%-10s enabled=%s\\n' "
        '"$svc" '
        '"$(systemctl is-active "$svc" 2>/dev/null)" '
        '"$(systemctl is-enabled "$svc" 2>/dev/null)"; '
        "done"
    )
    return ["/bin/sh", "-lc", script]


def cli_probe_command() -> list[str]:
    script = (
        "echo PATH=$PATH; "
        "for cmd in ptm pms telemetry candump; do "
        'printf "%-10s " "$cmd"; '
        'command -v "$cmd" || true; '
        "done"
    )
    return ["/bin/sh", "-lc", script]


def evidence_path() -> Path:
    base = Path(__file__).resolve().parent / "evidence"
    base.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return base / f"ptm_tui_{timestamp}.jsonl"


def bot_evidence_path(bot_id: str | None = None) -> Path:
    base = Path(__file__).resolve().parent / "evidence"
    base.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = ""
    if bot_id:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", bot_id)
        suffix = f"_{safe}"
    return base / f"ptm_tui_{timestamp}{suffix}.jsonl"


class EvidenceLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, event: str, **fields: Any) -> None:
        record = {"ts": datetime.now().isoformat(timespec="milliseconds"), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")


class RemoteSshRunner:
    def __init__(
        self,
        *,
        host: str,
        user: str,
        ssh_port: int,
        connect_timeout: int,
        remote_cli_dir: str | None,
        identity_file: Path | None,
        ssh_password: str | None,
        ssh_backend: Literal["auto", "openssh", "paramiko"],
        accept_new_host_key: bool,
        dry_run: bool,
    ) -> None:
        self.host = host
        self.user = user
        self.ssh_port = ssh_port
        self.connect_timeout = connect_timeout
        self.remote_cli_dir = remote_cli_dir
        self.identity_file = identity_file
        self.ssh_password = ssh_password
        self.ssh_backend = ssh_backend
        self.accept_new_host_key = accept_new_host_key
        self.dry_run = dry_run
        self.ssh_path = shutil.which("ssh")
        self._active_processes: set[asyncio.subprocess.Process] = set()
        self._askpass_path = self._ensure_askpass_helper() if ssh_password else None

    def check_ssh_available(self) -> None:
        if self.dry_run or self.using_paramiko:
            return
        if not self.ssh_path:
            raise RuntimeError("OpenSSH client 'ssh' was not found on this laptop. Install Windows OpenSSH or add ssh.exe to PATH.")

    @property
    def using_paramiko(self) -> bool:
        if self.ssh_backend == "paramiko":
            return True
        if self.ssh_backend == "openssh":
            return False
        return bool(self.ssh_password)

    def remote_command(self, command: list[str]) -> str:
        if not command:
            raise ValueError("Remote command cannot be empty")
        remote_parts = command.copy()
        if self.remote_cli_dir and remote_parts[0] in REMOTE_CLI_DIR_COMMANDS:
            remote_parts[0] = posixpath.join(self.remote_cli_dir.rstrip("/"), remote_parts[0])
        command_text = " ".join(shlex.quote(part) for part in remote_parts)
        return f"PATH={REMOTE_BASE_PATH}; export PATH; {command_text}"

    def ssh_args(self, command: list[str]) -> list[str]:
        remote = self.remote_command(command)
        args = [
            self.ssh_path or "ssh",
            "-p",
            str(self.ssh_port),
            "-o",
            "BatchMode=no",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
        ]
        if self.accept_new_host_key:
            args.extend(["-o", "StrictHostKeyChecking=accept-new"])
        if self.identity_file:
            args.extend(["-i", str(self.identity_file)])
        args.extend([f"{self.user}@{self.host}", remote])
        return args

    def ssh_env(self) -> dict[str, str] | None:
        if not self.ssh_password or not self._askpass_path:
            return None
        env = os.environ.copy()
        env["SSH_ASKPASS"] = str(self._askpass_path)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env["DISPLAY"] = env.get("DISPLAY", "ptm_tui")
        env["PTM_TUI_SSH_PASSWORD"] = self.ssh_password
        return env

    def _ensure_askpass_helper(self) -> Path:
        helper = Path(tempfile.gettempdir()) / "ptm_tui_ssh_askpass.cmd"
        helper.write_text(
            "@echo off\r\n"
            "powershell.exe -NoProfile -Command "
            "\"[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); Write-Output $env:PTM_TUI_SSH_PASSWORD\"\r\n",
            encoding="utf-8",
        )
        return helper

    async def run_once(self, command: list[str]) -> CommandResult:
        args = self.ssh_args(command)
        if self.dry_run:
            return CommandResult(args=args, returncode=0, stdout=f"DRY-RUN: {' '.join(args)}\n", stderr="")
        if self.using_paramiko:
            return await asyncio.to_thread(self._paramiko_run_once, command, args)

        self.check_ssh_available()
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL if self.ssh_password else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.ssh_env(),
            )
            self._active_processes.add(proc)
            stdout, stderr = await proc.communicate()
            return CommandResult(
                args=args,
                returncode=proc.returncode,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
            )
        except asyncio.CancelledError:
            if proc is not None:
                await self._terminate_process(proc)
            raise
        finally:
            if proc is not None:
                self._active_processes.discard(proc)

    async def stream(self, command: list[str]) -> AsyncIterator[str]:
        args = self.ssh_args(command)
        if self.dry_run:
            async for line in self._dry_run_stream(command, args):
                yield line
            return
        if self.using_paramiko:
            async for line in self._paramiko_stream(command):
                yield line
            return

        self.check_ssh_available()
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL if self.ssh_password else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.ssh_env(),
        )
        self._active_processes.add(proc)
        assert proc.stdout is not None
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                yield raw.decode(errors="replace").rstrip("\r\n")
        finally:
            await self._terminate_process(proc)
            self._active_processes.discard(proc)

    async def close(self) -> None:
        processes = list(self._active_processes)
        if not processes:
            return
        await asyncio.gather(*(self._terminate_process(proc) for proc in processes), return_exceptions=True)
        self._active_processes.clear()

    def _paramiko_client(self):
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError(
                "Password auth uses the optional Paramiko backend. Run: pip install -r tools\\ptm_tui\\requirements.txt"
            ) from exc

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        known_hosts = Path.home() / ".ssh" / "known_hosts"
        known_hosts.parent.mkdir(parents=True, exist_ok=True)
        if known_hosts.exists():
            client.load_host_keys(str(known_hosts))
        else:
            known_hosts.touch()
            client.load_host_keys(str(known_hosts))
        if self.accept_new_host_key:
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())

        key_filename = str(self.identity_file) if self.identity_file else None
        client.connect(
            hostname=self.host,
            port=self.ssh_port,
            username=self.user,
            password=self.ssh_password,
            key_filename=key_filename,
            timeout=self.connect_timeout,
            banner_timeout=self.connect_timeout,
            auth_timeout=self.connect_timeout,
            look_for_keys=not bool(self.ssh_password),
            allow_agent=not bool(self.ssh_password),
        )
        return client

    def _paramiko_run_once(self, command: list[str], args: list[str]) -> CommandResult:
        client = self._paramiko_client()
        try:
            stdin, stdout, stderr = client.exec_command(self.remote_command(command), timeout=None)
            stdin.close()
            out = stdout.read().decode(errors="replace")
            err = stderr.read().decode(errors="replace")
            returncode = stdout.channel.recv_exit_status()
            return CommandResult(args=args, returncode=returncode, stdout=out, stderr=err)
        finally:
            client.close()

    async def _paramiko_stream(self, command: list[str]) -> AsyncIterator[str]:
        client = await asyncio.to_thread(self._paramiko_client)
        stdin = stdout = stderr = None
        try:
            stdin, stdout, stderr = await asyncio.to_thread(lambda: client.exec_command(self.remote_command(command)))
            stdin.close()
            while True:
                line = await asyncio.to_thread(stdout.readline)
                if not line:
                    break
                if isinstance(line, bytes):
                    line = line.decode(errors="replace")
                yield line.rstrip("\r\n")
        finally:
            with contextlib.suppress(Exception):
                if stdout is not None:
                    stdout.channel.close()
            with contextlib.suppress(Exception):
                if stderr is not None:
                    stderr.channel.close()
            with contextlib.suppress(Exception):
                client.close()

    async def _terminate_process(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            with contextlib.suppress(Exception):
                await proc.wait()
            return
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()

    async def _dry_run_stream(self, command: list[str], args: list[str]) -> AsyncIterator[str]:
        yield f"DRY-RUN stream: {' '.join(args)}"
        counter = 0
        is_can = command and command[0] == "candump"
        while True:
            await asyncio.sleep(0.5)
            counter += 1
            if is_can:
                yield f"({counter}.000001) {command[-1]} 123#1122334455667788"
            else:
                current = 1.0 + (counter % 8) * 0.35
                yield f"PTMcur: {{I1:{current:.2f}, I2:{current + 0.1:.2f}, I3:{current + 0.2:.2f}, I4:{current + 0.3:.2f}}}"
                yield f"PTMpos: {{P1:{counter}, P2:{counter + 1}, P3:{counter + 2}, P4:{counter + 3}}}"
                yield 'PMSPTM: {"I":1.4, "V":24.1, "battery":87, "relayPTM":true}'


class PtmDashboard(App[None]):
    CSS = """
    Screen {
        background: #101316;
        color: #e8ecef;
    }

    #status {
        height: 3;
        padding: 0 1;
        background: #18323a;
        color: #f3fbfd;
        text-style: bold;
    }

    #controls {
        height: 6;
        padding: 0 1;
        background: #151b1f;
    }

    .control_group {
        width: 1fr;
        margin-right: 1;
    }

    .control_status {
        height: 1;
        text-align: center;
        text-style: bold;
    }

    .control_group Button {
        width: 100%;
    }

    #exit_safety {
        width: 1fr;
        height: 3;
        padding-top: 1;
    }

    #bot_select_group {
        width: 2fr;
        margin-right: 1;
    }

    #bot_select {
        width: 100%;
    }

    #relay_toggle {
        min-width: 24;
    }

    #ptm_toggle {
        min-width: 24;
    }

    #main {
        height: 1fr;
    }

    .pane_title {
        height: 1;
        padding: 0 1;
        background: #243138;
        color: #f3fbfd;
        text-style: bold;
    }

    #left, #right {
        width: 1fr;
        padding: 1;
    }

    .panel {
        border: solid #4b6870;
        padding: 0 1;
        margin-bottom: 1;
    }

    #log {
        height: 12;
        border: solid #4b6870;
        padding: 0 1;
    }

    DataTable {
        height: auto;
        min-height: 7;
        margin-bottom: 1;
    }

    #can_panel {
        height: 10;
    }

    #trend_panel {
        min-height: 9;
    }
    """

    BINDINGS = [
        ("a", "arm", "Arm/Disarm"),
        ("p", "relay_on", "Relay On"),
        ("o", "relay_off", "Relay Off"),
        ("s", "start_ptm", "Start PTM"),
        ("x", "stop_ptm", "Stop PTM"),
        ("r", "clear_warnings", "Clear Warnings"),
        ("d", "refresh_diagnostics", "Diagnostics"),
        ("c", "toggle_can", "CAN"),
        ("q", "quit", "Quit"),
    ]

    armed = reactive(False)
    ssh_status = reactive("UNKNOWN")
    telemetry_status = reactive("STARTING")

    def __init__(self, args: argparse.Namespace, runner: RemoteSshRunner, evidence: EvidenceLog, thresholds: CurrentThresholds) -> None:
        super().__init__()
        self.args = args
        self.runner = runner
        self.evidence = evidence
        self.thresholds = thresholds
        self.ptm_started_by_ui = False
        self.relay_status: bool | None = None
        self.relay_command_pending: str | None = None
        self.last_pms_at: datetime | None = None
        self.ptm_status: bool | None = None
        self.exit_relay_off_enabled = True
        self.exit_safety_done = False
        self.stop_sent_for_overcurrent = False
        self.last_telemetry_at: datetime | None = None
        self.currents: dict[str, float] = {}
        self.positions: dict[str, float] = {}
        self.pms: dict[str, Any] = {}
        self.pms_fallback: dict[str, Any] = {}
        self.services: list[ServiceStatus] = []
        self.warnings: list[str] = []
        self.can_visible = bool(args.enable_can)
        self.bot_entries: dict[str, BotEntry] = {bot.id: bot for bot in getattr(args, "bot_entries", [])}
        self.current_bot_id = getattr(args, "selected_bot_id", MANUAL_BOT_ID)
        self.telemetry_worker: Any = None
        self.can_worker: Any = None
        self.histories: dict[str, deque[float]] = {
            "I1": deque(maxlen=HISTORY_LIMIT),
            "I2": deque(maxlen=HISTORY_LIMIT),
            "I3": deque(maxlen=HISTORY_LIMIT),
            "I4": deque(maxlen=HISTORY_LIMIT),
            "supply_v": deque(maxlen=HISTORY_LIMIT),
            "supply_i": deque(maxlen=HISTORY_LIMIT),
        }

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="status")
        with Horizontal(id="controls"):
            with Vertical(id="bot_select_group"):
                yield Static("Bot", classes="control_status")
                yield Select(self.bot_select_options(), value=self.current_bot_id, id="bot_select")
            with Vertical(classes="control_group"):
                yield Static("Relay: UNKNOWN", id="relay_status", classes="control_status")
                yield Button("Relay On", id="relay_toggle", variant="primary")
            with Vertical(classes="control_group"):
                yield Static("PTM: UNKNOWN", id="ptm_status", classes="control_status")
                yield Button("Start PTM", id="ptm_toggle", variant="success")
            yield Checkbox("Relay off on exit", True, id="exit_safety")
        with Horizontal(id="main"):
            with VerticalScroll(id="left"):
                yield Static("PTM Status", classes="pane_title")
                yield Static(id="ptm_panel", classes="panel")
                yield Static("Motor Currents", classes="pane_title")
                yield DataTable(id="currents")
                yield Static("Telemetry Trends", classes="pane_title")
                yield Static(id="trend_panel", classes="panel")
                yield Static("Axis Positions", classes="pane_title")
                yield DataTable(id="positions")
                yield Static("PMS / Supply", classes="pane_title")
                yield Static(id="pms_panel", classes="panel")
            with VerticalScroll(id="right"):
                yield Static("BRP Services", classes="pane_title")
                yield Static(id="service_panel", classes="panel")
                yield Static("Warnings / Faults", classes="pane_title")
                yield Static(id="warnings_panel", classes="panel")
                yield Static("CAN Frames", id="can_title", classes="pane_title")
                yield RichLog(id="can_panel", classes="panel", max_lines=200, wrap=True)
                yield Static("Command / Event Log", classes="pane_title")
                yield RichLog(id="log", max_lines=LOG_MAX_LINES, wrap=True)
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "PTM Diagnostic Dashboard"
        self._setup_tables()
        if not self.args.enable_can:
            self.query_one("#can_title", Static).display = False
            self.query_one("#can_panel", RichLog).display = False
        self.refresh_view()
        self.log_event("INFO", f"Evidence log: {self.evidence.path}")
        self.log_event(
            "INFO",
            f"Motor thresholds: warning>{self.thresholds.warning_amps:g}, overcurrent>{self.thresholds.overcurrent_amps:g}",
        )
        backend = "paramiko" if self.runner.using_paramiko else "openssh"
        password_state = "configured" if self.args.ssh_password else "not configured"
        self.log_event("INFO", f"SSH backend: {backend}; password: {password_state}")
        self.evidence.write("app_start", host=self.args.host, user=self.args.user, dry_run=self.args.dry_run)
        self._install_signal_handlers()
        self.set_interval(0.5, self.check_telemetry_freshness)
        self.set_interval(STATUS_REFRESH_SECONDS, self.refresh_control_status)
        self.start_stream_workers()
        self.run_worker(self.check_cli_tools(), name="cli_probe", exclusive=False)
        if self.args.enable_service_diagnostics and not self.args.disable_service_diagnostics:
            self.run_worker(self.check_connection(), name="connection", exclusive=False)

    def start_stream_workers(self) -> None:
        self.telemetry_worker = self.run_worker(self.telemetry_loop(), name="telemetry", exclusive=False)
        if self.args.enable_can:
            self.can_worker = self.run_worker(self.can_loop(), name="can", exclusive=False)

    def bot_select_options(self) -> list[tuple[str, str]]:
        options = [(bot.label, bot.id) for bot in self.bot_entries.values()]
        if self.current_bot_id not in self.bot_entries:
            options.insert(0, (f"Manual / {self.args.host}", self.current_bot_id))
        return options

    def _setup_tables(self) -> None:
        currents = self.query_one("#currents", DataTable)
        currents.add_column("Motor", key="motor")
        currents.add_column("Current A", key="current")
        currents.add_column("State", key="state")
        for motor in ["M1", "M2", "M3", "M4"]:
            currents.add_row(motor, "-", "UNKNOWN", key=motor)

        positions = self.query_one("#positions", DataTable)
        positions.add_column("Axis", key="axis")
        positions.add_column("Position", key="position")
        for axis in ["P1", "P2", "P3", "P4"]:
            positions.add_row(axis, "-", key=axis)

    def _install_signal_handlers(self) -> None:
        try:
            signal.signal(signal.SIGINT, lambda *_: self.call_from_thread(self.request_exit))
        except ValueError:
            pass

    def request_exit(self) -> None:
        self.run_worker(self.stop_before_exit(), name="sigint_stop", exclusive=True)

    async def stop_before_exit(self) -> None:
        await self.run_exit_safety("exit")
        await self.runner.close()
        self.exit()

    async def on_unmount(self) -> None:
        await self.run_exit_safety("unmount")
        await self.runner.close()
        self.evidence.write("app_exit")

    async def action_quit(self) -> None:
        await self.stop_before_exit()

    def action_arm(self) -> None:
        self.armed = not self.armed
        self.log_event("ARM" if self.armed else "DISARM", "Local UI armed" if self.armed else "Local UI disarmed")
        self.evidence.write("arm_state", armed=self.armed)
        self.refresh_view()

    async def action_start_ptm(self) -> None:
        if not self.armed:
            self.add_warning("Start blocked: local UI is disarmed")
            return
        command = ["ptm", "-i"]
        result = await self.run_command("PTM START", command)
        if result.returncode == 0:
            self.ptm_started_by_ui = True
            self.ptm_status = True
        self.refresh_view()

    async def action_stop_ptm(self) -> None:
        await self.issue_stop("operator")

    async def action_toggle_ptm(self) -> None:
        if self.ptm_status is True:
            await self.issue_stop("operator")
        else:
            await self.action_start_ptm()

    async def issue_stop(self, reason: str) -> None:
        command = ["ptm", "-o"]
        result = await self.run_command(f"PTM STOP ({reason})", command)
        if result.returncode == 0:
            self.ptm_started_by_ui = False
            self.ptm_status = False
        self.refresh_view()

    async def action_relay_on(self) -> None:
        result = await self.run_command("PTM RELAY ON", ["pms", "-t", "1"])
        if result.returncode == 0:
            self.relay_command_pending = "ON"
        self.refresh_view()

    async def action_relay_off(self) -> None:
        result = await self.run_command("PTM RELAY OFF", ["pms", "-t", "0"])
        if result.returncode == 0:
            self.relay_command_pending = "OFF"
        self.refresh_view()

    async def action_toggle_relay(self) -> None:
        if self.relay_known_on():
            await self.action_relay_off()
        else:
            await self.action_relay_on()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "relay_toggle":
            await self.action_toggle_relay()
        elif event.button.id == "ptm_toggle":
            await self.action_toggle_ptm()

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "bot_select":
            return
        new_bot_id = str(event.value)
        if new_bot_id == self.current_bot_id:
            return
        await self.select_bot(new_bot_id)

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "exit_safety":
            self.exit_relay_off_enabled = bool(event.value)
            self.evidence.write("exit_relay_off_state", enabled=self.exit_relay_off_enabled)

    async def select_bot(self, new_bot_id: str) -> None:
        selector = self.query_one("#bot_select", Select)
        if not can_switch_bot(self.ptm_started_by_ui):
            self.add_warning("Bot switch blocked: stop PTM before changing bot")
            selector.value = self.current_bot_id
            return
        bot = self.bot_entries.get(new_bot_id)
        if bot is None:
            self.add_warning(f"Bot switch failed: unknown bot '{new_bot_id}'")
            selector.value = self.current_bot_id
            return

        previous_bot = self.current_bot_id
        self.log_event("INFO", f"Switching bot: {previous_bot} -> {bot.id}")
        await self.stop_stream_workers()
        await self.runner.close()
        apply_bot_entry_to_args(self.args, bot)
        self.current_bot_id = bot.id
        self.runner = make_runner(self.args)
        self.evidence = EvidenceLog(bot_evidence_path(bot.id))
        self.clear_live_state()
        self.evidence.write("bot_selected", previous_bot=previous_bot, new_bot=bot.id, host=bot.host)
        self.log_event("INFO", f"Evidence log: {self.evidence.path}")
        try:
            self.runner.check_ssh_available()
        except RuntimeError as exc:
            self.ssh_status = "SSH FAILED"
            self.add_warning(str(exc))
            return
        self.start_stream_workers()
        self.run_worker(self.check_cli_tools(), name="cli_probe", exclusive=False)
        self.refresh_view()

    async def stop_stream_workers(self) -> None:
        for worker in [self.telemetry_worker, self.can_worker]:
            if worker is not None:
                with contextlib.suppress(Exception):
                    worker.cancel()
        self.telemetry_worker = None
        self.can_worker = None

    def clear_live_state(self) -> None:
        self.ssh_status = "SWITCHING"
        self.telemetry_status = "STARTING"
        self.last_telemetry_at = None
        self.last_pms_at = None
        self.currents.clear()
        self.positions.clear()
        self.pms.clear()
        self.pms_fallback.clear()
        self.services.clear()
        self.relay_status = None
        self.relay_command_pending = None
        self.ptm_status = None
        self.stop_sent_for_overcurrent = False
        for history in self.histories.values():
            history.clear()

    async def run_exit_safety(self, reason: str) -> None:
        if self.exit_safety_done:
            return
        self.exit_safety_done = True
        if self.ptm_started_by_ui:
            await self.issue_stop(reason)
        if self.exit_relay_off_enabled:
            self.log_event("CMD", f"Exit safety relay off ({reason})")
            self.evidence.write("exit_safety_relay_off_start", reason=reason)
            result = await self.run_command(f"PTM RELAY OFF ({reason})", ["pms", "-t", "0"])
            self.evidence.write("exit_safety_relay_off_finish", reason=reason, returncode=result.returncode)

    def action_clear_warnings(self) -> None:
        self.warnings.clear()
        self.stop_sent_for_overcurrent = False
        self.log_event("INFO", "Local warnings cleared")
        self.refresh_view()

    def action_toggle_can(self) -> None:
        if not self.args.enable_can:
            self.log_event("INFO", "CAN panel is disabled; relaunch with --enable-can")
            return
        self.can_visible = not self.can_visible
        self.query_one("#can_panel", RichLog).display = self.can_visible

    def action_refresh_diagnostics(self) -> None:
        if self.args.disable_service_diagnostics:
            self.log_event("INFO", "Service diagnostics are disabled")
            return
        self.run_worker(self.check_connection(), name="service_diagnostics", exclusive=True)

    async def run_command(self, label: str, command: list[str]) -> CommandResult:
        command_text = " ".join(command)
        self.log_event("CMD", f"{label}: {command_text}")
        self.log_event("CMD", f"Remote: {self.runner.remote_command(command)}")
        self.evidence.write("command_start", label=label, command=command)
        try:
            result = await self.runner.run_once(command)
        except Exception as exc:
            self.ssh_status = "ERROR"
            self.add_warning(f"{label} failed: {exc}")
            self.evidence.write("command_error", label=label, error=str(exc))
            return CommandResult(args=[], returncode=1, stdout="", stderr=str(exc))

        status = "OK" if result.returncode == 0 else f"FAILED {result.returncode}"
        self.ssh_status = status
        if result.stdout.strip():
            self.log_event("OUT", result.stdout.strip())
        if result.stderr.strip():
            self.log_event("ERR", result.stderr.strip())
        if result.returncode != 0:
            self.add_warning(f"{label} failed with return code {result.returncode}")
        self.evidence.write(
            "command_finish",
            label=label,
            command=command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        self.refresh_view()
        return result

    async def check_connection(self) -> None:
        result = await self.run_command("SERVICE DIAGNOSTICS", diagnostics_command())
        if result.returncode == 0:
            self.ssh_status = "CONNECTED"
            self.services = parse_service_statuses(result.stdout)
            for service in self.services:
                if service.active not in {"active", "activating"}:
                    self.add_warning(f"{service.name} active={service.active}")
            if not self.services:
                self.add_warning("Service diagnostics returned no parseable status lines")
        self.refresh_view()

    async def check_cli_tools(self) -> None:
        await self.run_command("CLI TOOL PROBE", cli_probe_command())

    async def telemetry_loop(self) -> None:
        command = [
            "telemetry",
            "--port_grpc",
            str(self.args.grpc_port),
            "--port_mqtt",
            str(self.args.mqtt_port),
            "--sampling_interval",
            str(self.args.sampling_ms),
            "--console_messages",
        ]
        self.log_event("CMD", "Starting telemetry SSH stream")
        try:
            async for line in self.runner.stream(command):
                self.last_telemetry_at = datetime.now()
                self.telemetry_status = "LIVE"
                self.log_event("TEL", line)
                self.evidence.write("telemetry", line=line)
                parsed = parse_telemetry_line(line)
                if self.telemetry_line_had_unparsed_tag(line, parsed):
                    self.log_event("WARN", f"PARSE FAILED: {self.telemetry_tag(line)} line did not match expected structure")
                    self.evidence.write("telemetry_parse_failed", tag=self.telemetry_tag(line), line=line)
                self.apply_telemetry(parsed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ssh_status = "SSH FAILED"
            self.telemetry_status = "ERROR"
            self.add_warning(f"Telemetry stream failed: {exc}")
            self.evidence.write("stream_error", stream="telemetry", error=str(exc))
        finally:
            self.refresh_view()

    async def can_loop(self) -> None:
        target = self.args.can_interface
        if self.args.can_filter:
            target = f"{target},{self.args.can_filter}"
        command = ["candump", "-tz", target]
        self.log_event("CMD", "Starting CAN SSH stream")
        can_panel = self.query_one("#can_panel", RichLog)
        try:
            async for line in self.runner.stream(command):
                can_panel.write(Text(line, style="cyan"))
                self.evidence.write("can", line=line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.add_warning(f"CAN stream failed: {exc}")
            self.evidence.write("stream_error", stream="can", error=str(exc))
        finally:
            self.refresh_view()

    def apply_telemetry(self, parsed: ParsedTelemetry) -> None:
        append_telemetry_histories(parsed, self.histories, HISTORY_LIMIT)
        if parsed.currents:
            self.currents.update(parsed.currents)
            self.check_current_limits()
            self.evidence.write("telemetry_parsed_ptmcur", values=parsed.currents)
        if parsed.positions:
            self.positions.update(parsed.positions)
            self.evidence.write("telemetry_parsed_ptmpos", values=parsed.positions)
        if parsed.pms:
            self.pms.update(parsed.pms)
            self.last_pms_at = datetime.now()
            self.evidence.write("telemetry_parsed_pmsptm", values=parsed.pms)
            relay = self.pms.get("relayPTM")
            if isinstance(relay, bool):
                if self.relay_status is not relay:
                    self.evidence.write("relay_state_changed", relayPTM=relay)
                self.relay_status = relay
                self.relay_command_pending = None
        if parsed.pms_fallback:
            # PMS fallback for supply display only. This is not PTM motor current.
            self.pms_fallback.update(parsed.pms_fallback)
            self.evidence.write("telemetry_parsed_pms_fallback", values=parsed.pms_fallback)
        if parsed.real_sections:
            self.evidence.write("telemetry_parsed_real_line", sections=parsed.real_sections)
        self.refresh_view()

    def refresh_control_status(self) -> None:
        self.refresh_view()

    def telemetry_line_had_unparsed_tag(self, line: str, parsed: ParsedTelemetry) -> bool:
        if "PTMcur:" in line:
            return parsed.currents is None
        if "PTMpos:" in line:
            return parsed.positions is None
        if "PMSPTM:" in line:
            return parsed.pms is None
        if extract_named_section(line, "PTM") is not None:
            return parsed.currents is None and parsed.positions is None
        if extract_named_section(line, "PMS") is not None:
            return parsed.pms_fallback is None and parse_pms_line(line) is None
        return False

    def telemetry_tag(self, line: str) -> str:
        for tag in ["PTMcur", "PTMpos", "PMSPTM", "PTM", "PMS"]:
            if f"{tag}:" in line or extract_named_section(line, tag) is not None:
                return tag
        return "telemetry"

    def check_current_limits(self) -> None:
        for key, current in self.currents.items():
            if current > self.thresholds.overcurrent_amps:
                self.add_warning(f"OVERCURRENT {key}: {current:.2f} A")
                if not self.stop_sent_for_overcurrent:
                    self.stop_sent_for_overcurrent = True
                    self.run_worker(self.issue_stop("overcurrent"), name="overcurrent_stop", exclusive=True)
            elif current > self.thresholds.warning_amps:
                self.add_warning(f"WARNING {key}: {current:.2f} A")

    def check_telemetry_freshness(self) -> None:
        if self.last_telemetry_at is None:
            return
        age = (datetime.now() - self.last_telemetry_at).total_seconds()
        if age > TELEMETRY_STALE_SECONDS:
            self.telemetry_status = "TELEMETRY STALE"
            self.refresh_view()

    def add_warning(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
            self.log_event("WARN", message)
            self.evidence.write("warning", message=message)
        self.refresh_view()

    def log_event(self, level: str, message: str) -> None:
        log = self.query_one("#log", RichLog)
        stamp = datetime.now().strftime("%H:%M:%S")
        style = {"WARN": "yellow", "ERR": "red", "CMD": "bold blue", "TEL": "cyan"}.get(level, "white")
        log.write(Text(f"[{stamp}] {level:<5} {message}", style=style))

    def refresh_view(self) -> None:
        if not self.is_mounted:
            return
        status = self.query_one("#status", Static)
        armed = "ARMED" if self.armed else "DISARMED"
        ptm = self.ptm_status_text()
        status.update(
            f"BRP {self.args.user}@{self.args.host}:{self.args.ssh_port} | SSH {self.ssh_status} | "
            f"Telemetry {self.telemetry_status} | {armed}"
        )

        self.query_one("#ptm_panel", Static).update(
            f"PTM status: {ptm}\nTelemetry gRPC port: {self.args.grpc_port}"
        )

        currents_table = self.query_one("#currents", DataTable)
        for index, motor in enumerate(["M1", "M2", "M3", "M4"]):
            key = f"I{index + 1}"
            value = self.currents.get(key)
            if value is None:
                display, state = Text("-", style="dim"), Text("UNKNOWN", style="dim")
            elif value > self.thresholds.overcurrent_amps:
                display, state = Text(f"{value:.2f}", style="bold red"), Text("OVERCURRENT", style="bold red")
            elif value > self.thresholds.warning_amps:
                display, state = Text(f"{value:.2f}", style="orange1"), Text("WARNING", style="orange1")
            else:
                display, state = Text(f"{value:.2f}", style="green"), Text("OK", style="green")
            currents_table.update_cell(motor, "current", display)
            currents_table.update_cell(motor, "state", state)

        positions_table = self.query_one("#positions", DataTable)
        for axis in ["P1", "P2", "P3", "P4"]:
            value = self.positions.get(axis)
            positions_table.update_cell(axis, "position", "-" if value is None else f"{value:g}")

        pms_lines = []
        for key in ["V", "I", "battery", "relayPTM"]:
            if key in self.pms:
                pms_lines.append(f"{key}: {self.pms[key]}")
        if self.pms_fallback:
            pms_lines.append("Supply source: PMS fallback")
            for key in ["V", "I", "battery", "temp", "RH", "relay1", "relay2"]:
                if key in self.pms_fallback:
                    pms_lines.append(f"PMS {key}: {self.pms_fallback[key]}")
        self.query_one("#pms_panel", Static).update("PMS\n" + ("\n".join(pms_lines) if pms_lines else "No PMS telemetry yet"))

        self.query_one("#trend_panel", Static).update(self.render_trends())

        self.query_one("#relay_status", Static).update(f"Relay: {self.relay_status_text()}")
        relay_button = self.query_one("#relay_toggle", Button)
        if self.relay_known_on():
            relay_button.label = "Relay Off"
            relay_button.variant = "warning"
        else:
            relay_button.label = "Relay On"
            relay_button.variant = "primary"

        self.query_one("#ptm_status", Static).update(f"PTM: {self.ptm_status_text()}")
        ptm_button = self.query_one("#ptm_toggle", Button)
        if self.ptm_status is True:
            ptm_button.label = "Stop PTM"
            ptm_button.variant = "error"
        else:
            ptm_button.label = "Start PTM"
            ptm_button.variant = "success" if self.armed else "default"

        if self.services:
            service_lines = [f"{svc.name}: {svc.active}/{svc.enabled}" for svc in self.services[:12]]
        else:
            service_lines = ["No service diagnostics yet"]
        self.query_one("#service_panel", Static).update("BRP Services\n" + "\n".join(service_lines))

        warnings_text = "\n".join(self.warnings[-12:]) if self.warnings else "No local warnings"
        self.query_one("#warnings_panel", Static).update("Warnings / Faults\n" + warnings_text)

    def relay_status_text(self) -> str:
        if self.last_pms_at is not None:
            age = (datetime.now() - self.last_pms_at).total_seconds()
            if age > TELEMETRY_STALE_SECONDS:
                return "STALE"
        if self.relay_command_pending:
            return f"COMMAND SENT {self.relay_command_pending}"
        if self.relay_status is True:
            return "ON"
        if self.relay_status is False:
            return "OFF"
        return "UNKNOWN"

    def relay_known_on(self) -> bool:
        if self.relay_status is not True:
            return False
        if self.last_pms_at is None:
            return False
        age = (datetime.now() - self.last_pms_at).total_seconds()
        return age <= TELEMETRY_STALE_SECONDS

    def ptm_status_text(self) -> str:
        if self.ptm_status is True:
            return "ON"
        if self.ptm_status is False:
            return "OFF"
        return "UNKNOWN"

    def render_trends(self) -> str:
        current_lines = ["Current trend"]
        for label, key in [("M1", "I1"), ("M2", "I2"), ("M3", "I3"), ("M4", "I4")]:
            current_lines.append(f"{label} {render_sparkline(self.histories[key]) or '-'}")
        supply_source = "PMS fallback" if self.pms_fallback else "PMSPTM"
        supply_lines = ["", f"Supply trend ({supply_source})"]
        supply_lines.append(f"V  {render_sparkline(self.histories['supply_v']) or '-'}")
        supply_lines.append(f"I  {render_sparkline(self.histories['supply_i']) or '-'}")
        return "\n".join(current_lines + supply_lines)


def load_bot_config(path: Path, bot_id: str) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        bots = json.load(handle)
    if not isinstance(bots, list):
        raise ValueError("Bot config must be a JSON array")
    for bot in bots:
        if isinstance(bot, dict) and bot.get("id") == bot_id:
            return bot
    raise ValueError(f"Bot '{bot_id}' was not found in {path}")


def default_bots_config_path() -> Path:
    base = Path(__file__).resolve().parent
    bots = base / "bots.json"
    return bots if bots.exists() else base / "bots.example.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PTM diagnostic dashboard for remote BRP control over SSH")
    parser.add_argument("--host", help="BRP IP address or hostname")
    parser.add_argument("--user", default="aceng", help="SSH username")
    parser.add_argument("--ssh-port", type=int, default=22, help="SSH port")
    parser.add_argument("--grpc-port", type=int, default=50051, help="Remote gRPC port")
    parser.add_argument("--mqtt-port", type=int, default=1883, help="Remote MQTT port")
    parser.add_argument("--sampling-ms", type=int, default=100, help="Telemetry sampling interval in ms")
    parser.add_argument("--ptm-timeout", type=int, default=5, help="PTM start timeout in seconds")
    parser.add_argument("--test-config", type=Path, default=DEFAULT_TEST_CONFIG, help="Local JSON config for display/test thresholds")
    parser.add_argument("--remote-cli-dir", help="Optional remote directory containing ptm/pms/telemetry")
    parser.add_argument("--identity-file", type=Path, help="Optional SSH private key path")
    parser.add_argument("--ssh-password", help="Optional SSH password. Prefer bot config or SSH keys for field use.")
    parser.add_argument(
        "--ssh-backend",
        choices=["auto", "openssh", "paramiko"],
        default="auto",
        help="SSH backend. auto uses Paramiko when a password is configured, otherwise OpenSSH.",
    )
    parser.add_argument(
        "--accept-new-host-key",
        action="store_true",
        help="Automatically trust first-seen SSH host keys using StrictHostKeyChecking=accept-new",
    )
    parser.add_argument("--dry-run", action="store_true", help="Log commands and use simulated streams without a BRP")
    parser.add_argument("--enable-can", action="store_true", help="Open an informational remote candump stream")
    parser.add_argument("--can-interface", default="can0", help="CAN interface for candump")
    parser.add_argument("--can-filter", help="Optional candump filter appended as interface,filter")
    parser.add_argument("--enable-service-diagnostics", action="store_true", help="Run service diagnostics automatically on startup")
    parser.add_argument("--disable-service-diagnostics", action="store_true", help="Disable service diagnostics, including the d key")
    parser.add_argument("--ssh-connect-timeout", type=int, default=5, help="SSH connect timeout in seconds")
    parser.add_argument("--bots-config", type=Path, default=default_bots_config_path(), help="Path to bots JSON config")
    parser.add_argument("--bot", help="Bot id to load from --bots-config")
    return parser


def resolve_args(args: argparse.Namespace) -> argparse.Namespace:
    entries = load_bot_entries(args.bots_config)
    entries_by_id = {entry.id: entry for entry in entries}
    if args.bot:
        if args.bot not in entries_by_id:
            raise SystemExit(f"Bot '{args.bot}' was not found in {args.bots_config}")
        apply_bot_entry_to_args(args, entries_by_id[args.bot])
    elif args.host:
        args.selected_bot_id = MANUAL_BOT_ID
    elif entries:
        apply_bot_entry_to_args(args, entries[0])
    if not args.host:
        raise SystemExit("--host is required unless --bot resolves one from --bots-config")
    if args.host and getattr(args, "selected_bot_id", None) == MANUAL_BOT_ID:
        manual = manual_bot_entry(args)
        entries = [manual] + [entry for entry in entries if entry.id != manual.id]
    args.bot_entries = entries
    return args


def make_runner(args: argparse.Namespace) -> RemoteSshRunner:
    return RemoteSshRunner(
        host=args.host,
        user=args.user,
        ssh_port=args.ssh_port,
        connect_timeout=args.ssh_connect_timeout,
        remote_cli_dir=args.remote_cli_dir,
        identity_file=args.identity_file,
        ssh_password=args.ssh_password,
        ssh_backend=args.ssh_backend,
        accept_new_host_key=args.accept_new_host_key,
        dry_run=args.dry_run,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = resolve_args(parser.parse_args(argv))
    evidence = EvidenceLog(bot_evidence_path(getattr(args, "selected_bot_id", None)))
    thresholds = load_test_config(args.test_config)
    runner = make_runner(args)
    try:
        runner.check_ssh_available()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    PtmDashboard(args, runner, evidence, thresholds).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
