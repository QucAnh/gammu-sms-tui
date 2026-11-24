"""Split-pane curses TUI for gammu-sms-tui."""

from __future__ import annotations

import curses
from datetime import datetime
import itertools
import locale
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any

from sms_tui.backend import GammuBackend, GammuError, SignalStatus
from sms_tui.parser import merge_parts, parse_backup
from sms_tui.render import format_signal_bar, gsm7_septet_len, sms_segments, wrap_text
from sms_tui.store import Message, Store, Thread, normalize_peer

_AUTO_SYNC_SECONDS = 30.0
_BACKSPACE_KEYS = ("\x7f", "\b", curses.KEY_BACKSPACE)


def _safe_addstr(win: Any, y: int, x: int, text: str, attr: int = 0) -> None:
    """Write text clipped to window bounds, never crashing at bottom-right corner."""
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y or x < 0 or x >= max_x:
        return
    avail = max_x - x
    if y == max_y - 1:
        avail -= 1
    if avail <= 0:
        return
    sanitized = "".join(ch if ord(ch) >= 32 and ord(ch) != 127 else " " for ch in text)
    try:
        win.addnstr(y, x, sanitized, avail, attr)
    except curses.error:
        pass


def _format_ts(ts: datetime) -> str:
    if ts == datetime.min:
        return "??-?? ??:??"
    return ts.strftime("%m-%d %H:%M")


class App:
    """Interactive split-pane SMS client state machine and curses renderer."""

    def __init__(
        self,
        store: Store,
        backend: GammuBackend,
        auto_sync_interval: float = _AUTO_SYNC_SECONDS,
        staging_dir: Path | None = None,
    ) -> None:
        self.store = store
        self.backend = backend
        self.auto_sync_interval = auto_sync_interval
        self.staging_dir = Path(staging_dir) if staging_dir else None

        self.threads: list[Thread] = []
        self.selected_idx: int = 0
        self.selected_peer: str | None = None
        self.messages: list[Message] = []
        self.msg_scroll: int = 0  # offset from bottom (0 = pinned to newest)
        self.focus: str = "threads"  # "threads" | "messages"
        self.mode: str = "list"  # "list" | "compose" | "new_number" | "search" | "confirm_clear"

        self.compose_peer: str = ""
        self.input_buf: str = ""
        self.search_query: str = ""
        self.status_msg: str = "Starting..."
        self.error_msg: str = ""
        self.signal_text: str = "Signal: checking..."
        self.signal_level: int | None = None
        self.signal_state: str = ""
        self._signal_pending: bool = True
        self._has_256_colors: bool = False
        self.last_sync: str = "never"
        self.running: bool = True

        self._cmd_seq = itertools.count()
        self._cmd_queue: queue.PriorityQueue[tuple[int, int, str, Any]] = queue.PriorityQueue()
        self._result_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._sync_queued = False
        self._signal_queued = False
        self._worker: threading.Thread | None = None
        self._last_auto_sync = 0.0

        self._recover_pending_backup()
        self.reload_threads()

    def _recover_pending_backup(self) -> None:
        """Import any staged backup left from an interrupted sync before unlinking."""
        if self.staging_dir is None:
            return
        pending = self.staging_dir / "pending-sync.ini"
        if not pending.exists():
            return
        msgs = merge_parts(parse_backup(pending))
        if self._staged_backup_unparsed(pending, len(msgs)):
            self.error_msg = "Staged backup parsed 0 messages; kept for inspection"
            return
        self.store.add_messages(msgs)
        pending.unlink(missing_ok=True)

    @staticmethod
    def _staged_backup_unparsed(pending: Path | None, parsed: int) -> bool:
        """True when a non-empty staged backup produced no messages at all."""
        if pending is None or parsed > 0 or not pending.exists():
            return False
        return pending.stat().st_size > 0

    def start_worker(self) -> None:
        """Start the single-threaded gammu worker if not already running."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def stop_worker(self) -> None:
        """Signal the worker thread to exit cleanly."""
        self.running = False
        self._cmd_queue.put((0, next(self._cmd_seq), "quit", None))
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=1.5)

    def _enqueue(self, priority: int, kind: str, payload: Any = None) -> None:
        if kind == "sync":
            if self._sync_queued:
                return
            self._sync_queued = True
        elif kind == "signal":
            if self._signal_queued:
                return
            self._signal_queued = True
        self._cmd_queue.put((priority, next(self._cmd_seq), kind, payload))

    def _worker_loop(self) -> None:
        while True:
            _, _, kind, payload = self._cmd_queue.get()
            if kind == "quit":
                return
            if kind == "sync":
                pending = self.staging_dir / "pending-sync.ini" if self.staging_dir else None
                try:
                    msgs = self.backend.sync(backup_path=pending)
                    self._result_queue.put(("sync_ok", (msgs, pending)))
                except GammuError as exc:
                    self._result_queue.put(("sync_err", str(exc)))
                finally:
                    self._sync_queued = False
            elif kind == "send":
                msg_id, number, text = payload
                try:
                    self.backend.send(number, text)
                    self._result_queue.put(("send_ok", msg_id))
                except GammuError as exc:
                    self._result_queue.put(("send_err", (msg_id, str(exc))))
            elif kind == "clear":
                try:
                    self.backend.clear_inbox()
                    self._result_queue.put(("clear_ok", None))
                except GammuError as exc:
                    self._result_queue.put(("clear_err", str(exc)))
            elif kind == "signal":
                try:
                    sig = self.backend.signal_status()
                    self._result_queue.put(("signal_ok", sig))
                except GammuError as exc:
                    self._result_queue.put(("signal_err", str(exc)))
                finally:
                    self._signal_queued = False

    def drain_results(self) -> bool:
        """Apply completed worker results on the UI thread. Returns True if state changed."""
        changed = False
        while True:
            try:
                kind, payload = self._result_queue.get_nowait()
            except queue.Empty:
                break
            changed = True
            if kind == "sync_ok":
                msgs, pending = payload
                inserted = self.store.add_messages(msgs)
                if self._staged_backup_unparsed(pending, len(msgs)):
                    self.error_msg = "Staged backup parsed 0 messages; kept for inspection"
                    self.status_msg = "Sync incomplete"
                else:
                    if pending is not None:
                        pending.unlink(missing_ok=True)
                    self.error_msg = ""
                    self.status_msg = f"Last sync: {self.last_sync} (+{inserted} new)"
                self.last_sync = datetime.now().strftime("%H:%M:%S")
                self.reload_threads()
            elif kind == "sync_err":
                self.error_msg = payload
                self.status_msg = "Sync failed"
            elif kind == "send_ok":
                self.store.set_status(payload, "sent")
                self.error_msg = ""
                self.status_msg = "Message sent"
                self.reload_threads()
            elif kind == "send_err":
                msg_id, err = payload
                self.store.set_status(msg_id, "failed")
                self.error_msg = err
                self.status_msg = "Send failed"
                self.reload_threads()
            elif kind == "clear_ok":
                self.error_msg = ""
                self.status_msg = "SIM inbox cleared (local archive intact)"
            elif kind == "clear_err":
                self.error_msg = payload
                self.status_msg = "Clear SIM failed"
            elif kind == "signal_ok":
                sig: SignalStatus = payload
                self._signal_pending = False
                self.signal_level = sig.level
                if self.signal_level is None and sig.dbm is not None:
                    self.signal_level = max(0, min(100, round((sig.dbm + 113) * 100 / 62)))
                self.signal_state = sig.state
                self.signal_text = format_signal_bar(sig.network, sig.state, sig.dbm, sig.level)
            elif kind == "signal_err":
                self._signal_pending = False
                self.signal_level = None
                self.signal_state = ""
                self.signal_text = "Signal: unavailable"
        return changed

    def reload_threads(self) -> None:
        """Refresh thread list and current conversation from SQLite."""
        self.threads = self.store.list_threads(self.search_query)
        if not self.threads:
            self.selected_idx = 0
            if self.selected_peer:
                self.messages = self.store.list_messages(self.selected_peer)
            else:
                self.messages = []
            return

        if self.selected_peer:
            for idx, th in enumerate(self.threads):
                if th.peer == self.selected_peer:
                    self.selected_idx = idx
                    break
            else:
                self.selected_idx = min(self.selected_idx, len(self.threads) - 1)
                self.selected_peer = self.threads[self.selected_idx].peer
        else:
            self.selected_idx = min(self.selected_idx, len(self.threads) - 1)
            self.selected_peer = self.threads[self.selected_idx].peer

        self.messages = self.store.list_messages(self.selected_peer)

    def _select_index(self, idx: int) -> None:
        if not self.threads:
            return
        idx = max(0, min(idx, len(self.threads) - 1))
        self.selected_idx = idx
        peer = self.threads[idx].peer
        self.selected_peer = peer
        self.msg_scroll = 0
        self.store.mark_read(peer)
        self.reload_threads()

    def request_sync(self) -> None:
        self.status_msg = "Syncing..."
        self._last_auto_sync = time.monotonic()
        self._enqueue(5, "sync")
        self._enqueue(6, "signal")

    def maybe_auto_sync(self) -> None:
        if self.mode in ("compose", "new_number", "confirm_clear"):
            return
        if time.monotonic() - self._last_auto_sync >= self.auto_sync_interval:
            self.request_sync()

    def _submit_compose(self) -> None:
        body = self.input_buf.strip("\r\n")
        peer = normalize_peer(self.compose_peer)
        if not peer or not body:
            self.mode = "list"
            self.input_buf = ""
            return
        msg_id = self.store.add_outgoing(peer, body, status="sending")
        self.store.mark_read(peer)
        self.selected_peer = peer
        self.msg_scroll = 0
        self.input_buf = ""
        self.mode = "list"
        self.status_msg = f"Sending to {peer}..."
        self.error_msg = ""
        self.reload_threads()
        self._enqueue(1, "send", (msg_id, peer, body))

    def handle_key(self, ch: int | str) -> bool:
        """Process one key event. Returns False when the user quits."""
        if self.mode == "confirm_clear":
            if ch in ("y", "Y"):
                self.status_msg = "Clearing SIM inbox..."
                self._enqueue(1, "clear")
            else:
                self.status_msg = "Clear SIM cancelled"
            self.mode = "list"
            return True

        if self.mode == "search":
            if ch in ("\x1b", 27):
                self.search_query = ""
                self.mode = "list"
                self.reload_threads()
            elif ch in ("\n", "\r", curses.KEY_ENTER):
                self.mode = "list"
            elif ch in _BACKSPACE_KEYS:
                self.search_query = self.search_query[:-1]
                self.reload_threads()
            elif isinstance(ch, str) and ch.isprintable():
                self.search_query += ch
                self.reload_threads()
            return True

        if self.mode == "new_number":
            if ch in ("\x1b", 27):
                self.input_buf = ""
                self.mode = "list"
            elif ch in ("\n", "\r", curses.KEY_ENTER):
                target = normalize_peer(self.input_buf)
                self.input_buf = ""
                if target:
                    self.compose_peer = target
                    self.mode = "compose"
                else:
                    self.mode = "list"
            elif ch in _BACKSPACE_KEYS:
                self.input_buf = self.input_buf[:-1]
            elif isinstance(ch, str) and ch.isprintable():
                self.input_buf += ch
            return True

        if self.mode == "compose":
            if ch in ("\x1b", 27):
                self.input_buf = ""
                self.mode = "list"
                self.status_msg = "Compose cancelled"
            elif ch in ("\x18", 24):  # Ctrl-X
                self._submit_compose()
            elif ch in ("\n", "\r", curses.KEY_ENTER):
                self.input_buf += "\n"
            elif ch in _BACKSPACE_KEYS:
                self.input_buf = self.input_buf[:-1]
            elif isinstance(ch, str) and ch.isprintable():
                self.input_buf += ch
            return True

        # Normal "list" mode
        if ch in ("q", "Q"):
            return False
        if ch in ("\t", 9):
            self.focus = "messages" if self.focus == "threads" else "threads"
        elif ch in ("j", curses.KEY_DOWN):
            if self.focus == "threads":
                self._select_index(self.selected_idx + 1)
            else:
                self.msg_scroll = max(0, self.msg_scroll - 1)
        elif ch in ("k", curses.KEY_UP):
            if self.focus == "threads":
                self._select_index(self.selected_idx - 1)
            else:
                self.msg_scroll += 1
        elif ch in ("\n", "\r", curses.KEY_ENTER, "i"):
            if self.threads:
                peer = self.threads[self.selected_idx].peer
                self.store.mark_read(peer)
                self.compose_peer = peer
                self.input_buf = ""
                self.mode = "compose"
                self.reload_threads()
        elif ch == "n":
            self.input_buf = ""
            self.mode = "new_number"
        elif ch == "/":
            self.mode = "search"
        elif ch == "r":
            self.request_sync()
        elif ch == "c":
            self.mode = "confirm_clear"
        return True

    def _build_message_lines(self, width: int) -> list[tuple[str, int]]:
        lines: list[tuple[str, int]] = []
        wrap_w = max(10, width - 4)
        for msg in self.messages:
            who = "You" if msg.direction == "out" else msg.peer
            tag = f" [{msg.status}]" if msg.direction == "out" and msg.status else ""
            if msg.parts > 1:
                tag += f" ({msg.parts}p)"
            header = f"[{_format_ts(msg.ts)}] {who}{tag}"
            lines.append((header, 2 if msg.direction == "out" else 1))
            for wline in wrap_text(msg.text, wrap_w):
                lines.append((f"  {wline}", 0))
            lines.append(("", 0))
        return lines

    def signal_stage(self) -> str:
        """Return semantic signal stage: 'pending', 'good', 'warn', or 'bad'."""
        if self._signal_pending:
            return "pending"
        if self.signal_level is not None:
            if self.signal_level >= 60:
                return "good"
            if self.signal_level >= 25:
                return "warn"
            return "bad"
        if self.signal_state in ("home", "home network"):
            return "good"
        if self.signal_state in ("roaming", "roaming network"):
            return "warn"
        return "bad"

    def _signal_attr(self) -> int:
        stage = self.signal_stage()
        if stage == "pending":
            return curses.A_REVERSE
        if self._has_256_colors:
            pair = {"good": 6, "warn": 7, "bad": 8}[stage]
            return curses.color_pair(pair) | curses.A_BOLD
        if stage == "good":
            return curses.A_REVERSE
        if stage == "warn":
            return curses.color_pair(4) | curses.A_BOLD | curses.A_REVERSE
        return curses.color_pair(3) | curses.A_BOLD

    def draw(self, stdscr: Any) -> None:
        stdscr.erase()
        max_y, max_x = stdscr.getmaxyx()
        if max_y < 6 or max_x < 24:
            _safe_addstr(stdscr, 0, 0, "Terminal too small")
            stdscr.refresh()
            return

        # Top bar
        if self.error_msg:
            banner = f" ERROR: {self.error_msg}"
            top_attr = curses.color_pair(3) | curses.A_BOLD
        else:
            banner = f" gammu-sms-tui | {self.status_msg}"
            top_attr = curses.A_REVERSE
        _safe_addstr(stdscr, 0, 0, banner.ljust(max_x), top_attr)

        footer_h = 4 if self.mode == "compose" else 2
        body_y = 1
        body_h = max(3, max_y - 1 - footer_h)
        left_w = max(18, min(36, int(max_x * 0.32)))
        right_x = left_w + 1
        right_w = max(10, max_x - right_x)

        # Divider
        for row in range(body_y, body_y + body_h):
            _safe_addstr(stdscr, row, left_w, "│")

        # Left pane header & threads
        left_hdr = f"Threads ({len(self.threads)})"
        if self.search_query:
            left_hdr += f" [/{self.search_query}]"
        hdr_attr = curses.A_BOLD | (curses.A_UNDERLINE if self.focus == "threads" else 0)
        _safe_addstr(stdscr, body_y, 1, left_hdr[: left_w - 1], hdr_attr)

        rows_avail = max(0, (body_y + body_h - 1) - (body_y + 1))
        items_per_screen = max(1, rows_avail // 2)
        start_idx = max(0, self.selected_idx - items_per_screen + 1)
        y = body_y + 1
        for idx in range(start_idx, len(self.threads)):
            if y + 1 >= body_y + body_h:
                break
            th = self.threads[idx]
            is_sel = idx == self.selected_idx
            mark = "*" if th.unread > 0 else " "
            label = th.display_name or th.peer
            ts_str = _format_ts(th.last_ts)
            row1 = f"{mark} {label}"
            if len(row1) + len(ts_str) + 2 < left_w:
                row1 = row1.ljust(left_w - len(ts_str) - 2) + ts_str
            attr = curses.A_REVERSE if is_sel else (curses.A_BOLD if th.unread > 0 else 0)
            _safe_addstr(stdscr, y, 0, row1[:left_w].ljust(left_w), attr)
            _safe_addstr(stdscr, y + 1, 2, th.preview[: max(1, left_w - 3)], attr if is_sel else curses.A_DIM)
            y += 2

        # Right pane header & messages
        peer_title = self.selected_peer or "(no thread selected)"
        r_attr = curses.A_BOLD | (curses.A_UNDERLINE if self.focus == "messages" else 0)
        _safe_addstr(stdscr, body_y, right_x + 1, peer_title[: right_w - 2], r_attr)

        msg_lines = self._build_message_lines(right_w)
        msg_rows = max(1, body_h - 1)
        max_scroll = max(0, len(msg_lines) - msg_rows)
        self.msg_scroll = min(self.msg_scroll, max_scroll)
        end_line = len(msg_lines) - self.msg_scroll
        start_line = max(0, end_line - msg_rows)
        for i, (line_txt, color_idx) in enumerate(msg_lines[start_line:end_line]):
            attr = curses.color_pair(color_idx) if color_idx else 0
            _safe_addstr(stdscr, body_y + 1 + i, right_x + 1, line_txt, attr)

        # Bottom bar: signal line + prompt/hints
        signal_y = max_y - footer_h
        _safe_addstr(stdscr, signal_y, 0, f" {self.signal_text}".ljust(max_x), self._signal_attr())
        footer_y = signal_y + 1
        if self.mode == "compose":
            segs, enc = sms_segments(self.input_buf)
            septets = gsm7_septet_len(self.input_buf)
            used = septets if septets is not None else len(self.input_buf.encode("utf-16-be")) // 2
            limit = (160 if segs == 1 else segs * 153) if enc == "gsm7" else (70 if segs == 1 else segs * 67)
            meta = f" To: {self.compose_peer}  [{used}/{limit} | {segs} SMS | {enc.upper()}]"
            _safe_addstr(stdscr, footer_y, 0, meta.ljust(max_x), curses.A_REVERSE)
            preview_line = self.input_buf.splitlines()[-1] if self.input_buf else ""
            if self.input_buf.endswith("\n"):
                preview_line = ""
            _safe_addstr(stdscr, footer_y + 1, 0, f" > {preview_line}_")
            _safe_addstr(stdscr, footer_y + 2, 0, " Ctrl-X:Send  Enter:Newline  Esc:Cancel", curses.A_DIM)
        elif self.mode == "new_number":
            _safe_addstr(stdscr, footer_y, 0, f" New SMS To: {self.input_buf}_  (Enter:Compose  Esc:Cancel)", curses.A_REVERSE)
        elif self.mode == "search":
            _safe_addstr(stdscr, footer_y, 0, f" Search: {self.search_query}_  (Enter:Keep  Esc:Clear)", curses.A_REVERSE)
        elif self.mode == "confirm_clear":
            _safe_addstr(stdscr, footer_y, 0, " Clear SIM inbox (folder 1)? Local archive stays intact. [y/N]", curses.color_pair(3) | curses.A_BOLD)
        else:
            hints = " j/k:Move  Tab:Pane  Enter:Reply  n:New  /:Search  r:Sync  q:Quit"
            _safe_addstr(stdscr, footer_y, 0, hints, curses.A_DIM)

        stdscr.refresh()

    def run(self) -> None:
        """Run the curses event loop."""
        locale.setlocale(locale.LC_ALL, "")
        os.environ.setdefault("ESCDELAY", "25")
        curses.wrapper(self._curses_main)

    def _curses_main(self, stdscr: Any) -> None:
        curses.curs_set(0)
        stdscr.timeout(250)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_WHITE, curses.COLOR_RED)
            curses.init_pair(4, curses.COLOR_YELLOW, -1)
            curses.init_pair(5, curses.COLOR_RED, -1)
            if curses.COLORS >= 256 and curses.COLOR_PAIRS > 8:
                curses.init_pair(6, 16, 42)   # black on emerald green (good)
                curses.init_pair(7, 16, 214)  # black on amber/yellow (warn)
                curses.init_pair(8, 231, 196) # white on red (bad)
                self._has_256_colors = True

        self.start_worker()
        self.request_sync()
        try:
            while self.running:
                self.drain_results()
                self.maybe_auto_sync()
                self.draw(stdscr)
                try:
                    ch = stdscr.get_wch()
                except curses.error:
                    continue
                if ch == curses.KEY_RESIZE:
                    continue
                if not self.handle_key(ch):
                    break
        finally:
            self.stop_worker()
