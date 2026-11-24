from datetime import datetime
from pathlib import Path
import time

from sms_tui.backend import GammuError, SignalStatus
from sms_tui.parser import Sms
from sms_tui.store import Store
from sms_tui.tui import App


class FakeBackend:
    def __init__(self, msgs=None, fail_send=False, fail_signal=False):
        self.msgs = msgs or []
        self.fail_send = fail_send
        self.fail_signal = fail_signal
        self.sent = []
        self.cleared = 0

    def sync(self, backup_path=None):
        if backup_path is not None:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_text("[SMSBackup000]\n#stub\n", encoding="utf-8")
        return list(self.msgs)

    def send(self, number: str, text: str):
        if self.fail_send:
            raise GammuError("port busy")
        self.sent.append((number, text))

    def clear_inbox(self):
        self.cleared += 1

    def signal_status(self):
        if self.fail_signal:
            raise GammuError("signal unsupported")
        return SignalStatus(network="Viettel", state="home network", dbm=-69, level=80)


def _wait_drain(app: App, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    got = False
    while time.monotonic() < deadline:
        if app.drain_results():
            got = True
            time.sleep(0.02)
            app.drain_results()
            return
        time.sleep(0.01)
    if not got:
        raise AssertionError("worker result timed out")


def test_app_sync_compose_ctrl_x_and_send(tmp_path):
    store = Store(tmp_path / "t.db")
    incoming = Sms(
        folder=1,
        state="UnRead",
        number="+84901234567",
        sent=datetime(2026, 11, 24, 15, 0, 0),
        coding="Default",
        text="Alo",
        parts=1,
        incomplete=False,
    )
    be = FakeBackend(msgs=[incoming])
    app = App(store=store, backend=be)  # type: ignore[arg-type]
    app.start_worker()
    try:
        app.request_sync()
        _wait_drain(app)
        assert len(app.threads) == 1
        assert app.threads[0].unread == 1

        # Open compose on selected thread, type Vietnamese, send with Ctrl-X
        app.handle_key("\n")
        assert app.mode == "compose"
        assert app.threads[0].unread == 0
        for ch in "Xin chào":
            app.handle_key(ch)
        app.handle_key("\x18")
        assert app.mode == "list"

        _wait_drain(app)
        assert be.sent == [("+84901234567", "Xin chào")]
        msgs = store.list_messages("+84901234567")
        assert msgs[-1].status == "sent"
        rendered = app._build_message_lines(60)
        assert any("Xin chào" in line for line, _ in rendered)
    finally:
        app.stop_worker()
        store.close()


def test_app_send_error_sets_banner_and_failed_status(tmp_path):
    store = Store(tmp_path / "t.db")
    be = FakeBackend(fail_send=True)
    app = App(store=store, backend=be)  # type: ignore[arg-type]
    app.start_worker()
    try:
        app.handle_key("n")
        for ch in "0901234567":
            app.handle_key(ch)
        app.handle_key("\n")
        assert app.mode == "compose"
        for ch in "hi":
            app.handle_key(ch)
        app.handle_key("\x18")

        _wait_drain(app)
        assert "port busy" in app.error_msg
        assert store.list_messages("+84901234567")[0].status == "failed"
    finally:
        app.stop_worker()
        store.close()


def test_app_confirm_clear_inbox(tmp_path):
    store = Store(tmp_path / "t.db")
    be = FakeBackend()
    app = App(store=store, backend=be)  # type: ignore[arg-type]
    app.start_worker()
    try:
        app.handle_key("c")
        assert app.mode == "confirm_clear"
        app.handle_key("n")
        assert app.mode == "list"
        assert be.cleared == 0

        app.handle_key("c")
        app.handle_key("y")
        _wait_drain(app)
        assert be.cleared == 1
    finally:
        app.stop_worker()
        store.close()


def test_app_recovers_staged_pending_backup_on_startup(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "backup_single.ini"
    staging = tmp_path / "state"
    staging.mkdir()
    pending = staging / "pending-sync.ini"
    pending.write_text(fixture.read_text(), encoding="utf-8")

    store = Store(tmp_path / "t.db")
    app = App(store=store, backend=FakeBackend(), staging_dir=staging)  # type: ignore[arg-type]
    try:
        assert not pending.exists()
        assert len(app.threads) == 1
        assert app.threads[0].peer == "+84901234567"
    finally:
        store.close()


def test_app_unlinks_staging_backup_after_sync(tmp_path):
    staging = tmp_path / "state"
    store = Store(tmp_path / "t.db")
    incoming = Sms(
        folder=1,
        state="UnRead",
        number="+84901234567",
        sent=datetime(2026, 11, 24, 15, 0, 0),
        coding="Default",
        text="Alo",
        parts=1,
        incomplete=False,
    )
    app = App(store=store, backend=FakeBackend(msgs=[incoming]), staging_dir=staging)  # type: ignore[arg-type]
    app.start_worker()
    try:
        app.request_sync()
        _wait_drain(app)
        assert not (staging / "pending-sync.ini").exists()
        assert len(app.threads) == 1
        assert "Viettel" in app.signal_text
    finally:
        app.stop_worker()
        store.close()


def test_app_signal_failure_does_not_set_error_banner(tmp_path):
    store = Store(tmp_path / "t.db")
    app = App(store=store, backend=FakeBackend(fail_signal=True))  # type: ignore[arg-type]
    assert app.signal_stage() == "pending"
    app.start_worker()
    try:
        app.request_sync()
        _wait_drain(app)
        assert app.error_msg == ""
        assert app.signal_text == "Signal: unavailable"
        assert app.signal_stage() == "bad"
    finally:
        app.stop_worker()
        store.close()


def test_app_signal_stage_colors_by_state_and_level(tmp_path):
    store = Store(tmp_path / "t.db")
    app = App(store=store, backend=FakeBackend())  # type: ignore[arg-type]
    try:
        app._signal_pending = False
        app.signal_level = None
        app.signal_state = "home"
        assert app.signal_stage() == "good"

        app.signal_state = "roaming"
        assert app.signal_stage() == "warn"

        app.signal_state = "no service"
        assert app.signal_stage() == "bad"

        app.signal_level = 70
        assert app.signal_stage() == "good"

        app.signal_level = 40
        assert app.signal_stage() == "warn"

        app.signal_level = 10
        assert app.signal_stage() == "bad"
    finally:
        store.close()


def test_app_keeps_staging_backup_when_sync_parses_empty(tmp_path):
    staging = tmp_path / "state"
    store = Store(tmp_path / "t.db")
    app = App(store=store, backend=FakeBackend(msgs=[]), staging_dir=staging)  # type: ignore[arg-type]
    app.start_worker()
    try:
        app.request_sync()
        _wait_drain(app)
        assert (staging / "pending-sync.ini").exists()
        assert "0 messages" in app.error_msg
    finally:
        app.stop_worker()
        store.close()


def test_app_keeps_unparsable_staged_backup(tmp_path):
    staging = tmp_path / "state"
    staging.mkdir()
    pending = staging / "pending-sync.ini"
    pending.write_text(" [SMSBackup000]\nSMSC = \"+8498\"\n", encoding="utf-8")

    store = Store(tmp_path / "t.db")
    app = App(store=store, backend=FakeBackend(), staging_dir=staging)  # type: ignore[arg-type]
    try:
        assert pending.exists()
        assert "0 messages" in app.error_msg
    finally:
        store.close()
