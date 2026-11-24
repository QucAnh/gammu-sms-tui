"""Serialized wrapper around the gammu CLI."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import select
import subprocess
import tempfile
import termios
import threading
import time

from sms_tui.parser import Sms, merge_parts, parse_backup
from sms_tui.render import sms_segments

_LOG = logging.getLogger("sms_tui.backend")
_SIGNAL_DBM_RE = re.compile(r"Signal strength\s*:\s*(-?\d+)\s*dBm", re.IGNORECASE)
_NETWORK_LEVEL_RE = re.compile(r"Network level\s*:\s*(\d+)", re.IGNORECASE)
_NETWORK_STATE_RE = re.compile(r"Network state\s*:\s*(.+)", re.IGNORECASE)
_NETWORK_NAME_RE = re.compile(r"Network name for Gammu\s*:\s*(.+)", re.IGNORECASE)
_NETWORK_CODE_RE = re.compile(r"Network code\s*:\s*(.+)", re.IGNORECASE)

_AT_CSQ_RE = re.compile(r"\+CSQ:\s*(\d+),(\d+)")
_AT_CESQ_RE = re.compile(r"\+CESQ:\s*(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)")
_AT_XCESQ_RE = re.compile(r"\+XCESQ:\s*\d+,(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)")
_AT_COPS_RE = re.compile(r'\+COPS:\s*\d+,\d+,"([^"]*)"(?:,(\d+))?')
_AT_REG_RE = re.compile(r"\+CE?REG:\s*\d+,(\d+)")
_ACT_MAP = {
    "0": "2G",
    "1": "2G",
    "2": "3G",
    "3": "2G",
    "4": "3G",
    "5": "3G",
    "6": "3G",
    "7": "LTE",
    "11": "5G",
    "12": "5G",
}
_REG_MAP = {
    "1": "home",
    "5": "roaming",
    "2": "searching",
    "0": "no service",
    "3": "denied",
}


@dataclass(frozen=True)
class SignalStatus:
    """Modem network registration and signal strength snapshot."""

    network: str = ""
    state: str = ""
    dbm: int | None = None
    level: int | None = None


def parse_signal_output(text: str) -> SignalStatus:
    """Parse `gammu monitor 1` or `gammu networkinfo` stdout."""
    name_m = _NETWORK_NAME_RE.search(text)
    code_m = _NETWORK_CODE_RE.search(text)
    state_m = _NETWORK_STATE_RE.search(text)
    dbm_m = _SIGNAL_DBM_RE.search(text)
    lvl_m = _NETWORK_LEVEL_RE.search(text)

    network = ""
    if name_m:
        network = name_m.group(1).strip().strip('"')
    elif code_m:
        network = code_m.group(1).strip().strip('"')

    state = state_m.group(1).strip() if state_m else ""
    dbm = int(dbm_m.group(1)) if dbm_m else None
    level = int(lvl_m.group(1)) if lvl_m else None
    return SignalStatus(network=network, state=state, dbm=dbm, level=level)


def parse_at_signal(text: str) -> SignalStatus:
    """Parse combined `AT+CSQ`, `AT+CESQ`, `AT+COPS?`, `AT+CEREG?`, `AT+CREG?` output."""
    dbm: int | None = None
    level: int | None = None
    for csq_m in _AT_CSQ_RE.finditer(text):
        rssi = int(csq_m.group(1))
        if 0 <= rssi <= 31:
            dbm = -113 + 2 * rssi
            level = round(rssi * 100 / 31)
            break

    if dbm is None:
        for regex in (_AT_CESQ_RE, _AT_XCESQ_RE):
            for cesq_m in regex.finditer(text):
                rxlev, _, rscp, _, _, rsrp = (int(g) for g in cesq_m.groups())
                if 0 <= rsrp <= 97:
                    dbm = -140 + rsrp
                    level = max(0, min(100, round((dbm + 120) * 100 / 45)))
                    break
                if 0 <= rscp <= 96:
                    dbm = -120 + rscp
                    level = max(0, min(100, round((dbm + 115) * 100 / 50)))
                    break
                if 0 <= rxlev <= 63:
                    dbm = -110 + rxlev
                    level = max(0, min(100, round((dbm + 110) * 100 / 62)))
                    break
            if dbm is not None:
                break

    network = ""
    cops_m = _AT_COPS_RE.search(text)
    if cops_m:
        op = cops_m.group(1).strip()
        act = _ACT_MAP.get(cops_m.group(2) or "", "")
        network = f"{op} {act}".strip() if op and act else op

    regs = [m.group(1) for m in _AT_REG_RE.finditer(text)]
    state = ""
    for code in ("1", "5", "2", "3", "0"):
        if code in regs:
            state = _REG_MAP[code]
            break

    return SignalStatus(network=network, state=state, dbm=dbm, level=level)


def _read_gammurc_device(config: Path | None) -> Path | None:
    cfg = config or (Path.home() / ".gammurc")
    if not cfg.exists():
        return None
    for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "[")):
            continue
        if "=" not in stripped:
            continue
        key, val = stripped.split("=", 1)
        if key.strip().lower() in ("device", "port"):
            dev = val.strip().strip('"').strip("'")
            if dev:
                return Path(dev)
    return None


def _at_roundtrip(fd: int, cmd: bytes, timeout: float = 1.5) -> str:
    os.write(fd, cmd)
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rem = max(0.05, deadline - time.monotonic())
        rlist, _, _ = select.select([fd], [], [], min(0.2, rem))
        if not rlist:
            continue
        try:
            chunk = os.read(fd, 1024)
        except BlockingIOError:
            continue
        if not chunk:
            break
        buf.extend(chunk)
        if b"\nOK\r" in buf or b"\nOK\n" in buf or b"\r\nOK\r\n" in buf or b"ERROR" in buf:
            break
    return bytes(buf).decode("utf-8", errors="replace")


def _query_at_serial(device: Path, timeout: float = 1.5) -> str:
    """Send AT+CSQ/CESQ/COPS?/CEREG?/CREG? (and optional XCESQ?) over serial."""
    fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        try:
            attrs = termios.tcgetattr(fd)
            attrs[0] &= ~(
                termios.IGNBRK
                | termios.BRKINT
                | termios.PARMRK
                | termios.ISTRIP
                | termios.INLCR
                | termios.IGNCR
                | termios.ICRNL
                | termios.IXON
            )
            attrs[1] &= ~termios.OPOST
            attrs[2] &= ~(termios.CSIZE | termios.PARENB)
            attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
            attrs[4] = termios.B115200
            attrs[5] = termios.B115200
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIFLUSH)
        except termios.error:
            pass  # PTYs in tests may ignore some serial flags

        return _at_roundtrip(fd, b"AT+CSQ;+CESQ;+COPS?;+CEREG?;+CREG?\r", timeout=timeout)
    finally:
        os.close(fd)


class GammuError(RuntimeError):
    """Raised when a gammu CLI invocation fails or times out."""


class GammuBackend:
    """Thread-safe, single-process-at-a-time gammu CLI client."""

    def __init__(
        self,
        gammu_bin: str = "gammu",
        config: Path | None = None,
        auto_clear: bool = True,
    ) -> None:
        self.gammu_bin = gammu_bin
        self.config = Path(config) if config else None
        self.auto_clear = auto_clear
        self._lock = threading.Lock()

    def _run(self, *args: str, timeout: int = 60) -> str:
        cmd = [self.gammu_bin]
        if self.config is not None:
            cmd.extend(["-c", str(self.config)])
        cmd.extend(args)

        env = {**os.environ, "LC_ALL": "C.UTF-8"}
        _LOG.info("running: %s", " ".join(cmd))
        with self._lock:
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=env,
                    check=False,
                )
            except OSError as exc:
                _LOG.error("os error running %s: %s", args[0] if args else "gammu", exc)
                raise GammuError(f"gammu {' '.join(args)} failed: {exc}") from exc
            except subprocess.TimeoutExpired as exc:
                _LOG.error("timeout running %s after %ss", args[0] if args else "gammu", timeout)
                raise GammuError(f"gammu {' '.join(args)} timed out after {timeout}s") from exc

        if proc.returncode != 0:
            parts = [s.strip() for s in (proc.stderr, proc.stdout) if s and s.strip()]
            detail = " | ".join(parts) or f"exit {proc.returncode}"
            _LOG.error("gammu exit=%s stdout=%r stderr=%r", proc.returncode, proc.stdout, proc.stderr)
            raise GammuError(f"gammu {' '.join(args)} failed: {detail}")
        _LOG.info("gammu exit=0 (%s)", args[0] if args else "")
        return proc.stdout

    def sync(self, backup_path: Path | None = None) -> list[Sms]:
        """Export SMS via `gammu backupsms` (-yes to archive & free SIM, or -all)."""
        flag = "-yes" if self.auto_clear else "-all"
        if backup_path is not None:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            self._run("backupsms", str(backup_path), flag)
            return merge_parts(parse_backup(backup_path))

        with tempfile.NamedTemporaryFile(prefix="sms-tui-", suffix=".ini", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            self._run("backupsms", str(tmp_path), flag)
            return merge_parts(parse_backup(tmp_path))
        finally:
            tmp_path.unlink(missing_ok=True)

    def send(self, number: str, text: str) -> None:
        """Send an SMS using GSM-7 or UCS-2 (-unicode) automatically."""
        _, encoding = sms_segments(text)
        args = ["sendsms", "TEXT", number]
        if encoding == "ucs2":
            args.append("-unicode")
        args.extend(["-textutf8", text])
        self._run(*args)

    def clear_inbox(self) -> None:
        """Delete all SMS from SIM folder 1."""
        self._run("deleteallsms", "1")

    def identify(self) -> str:
        """Return `gammu identify` output."""
        return self._run("identify", timeout=20).strip()

    def signal_status(self) -> SignalStatus:
        """Query signal quality via AT serial probe, falling back to `gammu networkinfo`."""
        device = _read_gammurc_device(self.config)
        if device is not None and device.exists():
            try:
                with self._lock:
                    raw = _query_at_serial(device)
                status = parse_at_signal(raw)
                if status.dbm is not None or status.level is not None or status.network or status.state:
                    _LOG.info("signal AT probe: %s (raw=%r)", status, raw.strip())
                    return status
            except OSError as exc:
                _LOG.warning("AT signal probe failed on %s: %s", device, exc)

        out = self._run("networkinfo", timeout=15)
        return parse_signal_output(out)
