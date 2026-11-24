# gammu-sms-tui Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A terminal SMS client that reads, threads, archives, and sends SMS through the existing `gammu` CLI on `/dev/ttyXMM1`.

**Architecture:** `gammu` CLI behind one backend class; `backupsms` INI parsed into parts, multipart merged via UDH; SQLite archive; `curses` split-pane UI with gammu calls on a worker thread.

**Tech Stack:** Python 3.14 stdlib (`curses`, `sqlite3`, `subprocess`, `dataclasses`), pytest, gammu 1.45.

**Spec:** `docs/superpowers/specs/2026-11-24-gammu-sms-tui-design.md`

## Global Constraints

- Python >= 3.11; no runtime packages beyond stdlib and the `gammu` binary.
- Exactly one `gammu` process may touch `/dev/ttyXMM1` at a time.
- Read with `gammu backupsms <file> -all` (keeps messages on SIM).
- Send with `gammu sendsms TEXT <number> -textutf8 <text>`.
- Local DB at `~/.local/share/gammu-sms-tui/messages.db` (override via `SMS_TUI_DB`).
- Never delete individual SMS from SIM; only bulk `gammu deleteallsms 1` after confirmation.
- Decode text as UTF-8; preserve Vietnamese diacritics.

## Review Focus

- OTP/carrier SMS with alphanumeric sender (`VIETTEL`, bank names) thread correctly.
- Incomplete multipart (part evicted from SIM) shows available text, no crash.
- Unicode text > 70 chars and GSM-7 > 160 chars report correct segment counts.
- gammu errors (busy port, permission denied, no SIM) become a UI banner, not a traceback.
- Duplicate syncs do not duplicate rows in the archive.

---

### Task 1: Project skeleton + single-part parser

**Files:**
- Create: `pyproject.toml`
- Create: `src/sms_tui/__init__.py`
- Create: `src/sms_tui/parser.py`
- Create: `tests/test_parser.py`
- Create: `tests/fixtures/backup_single.ini`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class SmsPart: folder:int; state:str; number:str; sent:datetime; coding:str; text:str; udh:bytes|None`
  - `parse_backup(path: Path) -> list[SmsPart]`
  - `decode_text00(values: dict[str,str]) -> str`

- [ ] **Step 1: Write the failing test**

`tests/fixtures/backup_single.ini`:

```ini
[SMSBackup000]
#Hello from test
#
SMSC = "+84980200615"
Sent = 20261124T150926
State = UnRead
Number = "+84901234567"
Text00 = 00480065006C006C006F002000660072006F006D00200074006500730074000A
Coding = Default
Folder = 1
Length = 17
Class = -1
```

`tests/test_parser.py`:

```python
from datetime import datetime
from pathlib import Path
from sms_tui.parser import parse_backup, decode_text00

FIXTURES = Path(__file__).parent / "fixtures"

def test_parse_single_part():
    parts = parse_backup(FIXTURES / "backup_single.ini")
    assert len(parts) == 1
    p = parts[0]
    assert p.number == "+84901234567"
    assert p.folder == 1
    assert p.state == "UnRead"
    assert p.sent == datetime(2026, 11, 24, 15, 9, 26)
    assert p.text == "Hello from test"

def test_decode_text00_utf16be():
    assert decode_text00({"Text00": "00580069006E00200063006800E0006F"}) == "Xin chào"

def test_decode_text00_joins_numbered_parts():
    assert decode_text00({"Text00": "0041", "Text01": "0042"}) == "AB"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_parser.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'sms_tui'`

- [ ] **Step 3: Implement skeleton and parser**

`pyproject.toml`:

```toml
[project]
name = "gammu-sms-tui"
version = "0.1.0"
requires-python = ">=3.11"

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

`parser.py`: read the file with `encoding="utf-8", errors="replace"`. Split on section headers matching `^\[SMSBackup`. For each section, collect:
- comment text: consecutive lines after the header starting with `#` or `;`, strip the marker and one optional space, join with `\n`.
- key/value lines: split on first `=`, strip whitespace and surrounding quotes.
- `text`: comment block if non-empty, else `decode_text00(values)`.
- `sent`: parse `Sent` or `DateTime` with `datetime.strptime(v, "%Y%m%dT%H%M%S")`.
- `udh`: `bytes.fromhex(values["UDH"]) if values.get("UDH") else None`.

`decode_text00`: concatenate `Text00`, `Text01`, ... in numeric order, `bytes.fromhex`, decode `utf-16-be`, strip trailing `\n`/`\x00`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_parser.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src tests
git commit -m "feat: parse gammu SMS backup into parts"
```

---

### Task 2: UDH concatenation + multipart merge

**Files:**
- Modify: `src/sms_tui/parser.py`
- Modify: `tests/test_parser.py`
- Create: `tests/fixtures/backup_multipart.ini`

**Interfaces:**
- Consumes: `SmsPart`, `parse_backup`
- Produces:
  - extend `SmsPart` with `concat_ref:int|None; concat_total:int|None; concat_seq:int|None`
  - `@dataclass(frozen=True) class Sms: folder:int; state:str; number:str; sent:datetime; coding:str; text:str; parts:int; incomplete:bool`
  - `merge_parts(parts: list[SmsPart]) -> list[Sms]`

- [ ] **Step 1: Write the failing tests**

Fixture: four sections `000`..`003`. `000` and `001` are the two parts of one message from `+84901234567`, `UDH = 050003AB0201` and `050003AB0202`, comment texts `Xin chào ` and `Việt Nam`. `002` is a standalone `VIETTEL` OTP. `003` is an orphan part `UDH = 050003CD0202` (only seq 2 of 2).

```python
from sms_tui.parser import parse_backup, merge_parts

def test_parse_udh_8bit():
    parts = parse_backup(FIXTURES / "backup_multipart.ini")
    p = parts[0]
    assert (p.concat_ref, p.concat_total, p.concat_seq) == (0xAB, 2, 1)

def test_merge_multipart_in_sequence_order():
    merged = merge_parts(parse_backup(FIXTURES / "backup_multipart.ini"))
    msg = next(m for m in merged if m.number == "+84901234567")
    assert msg.text == "Xin chào Việt Nam"
    assert msg.parts == 2 and msg.incomplete is False

def test_alphanumeric_sender_kept():
    merged = merge_parts(parse_backup(FIXTURES / "backup_multipart.ini"))
    assert any(m.number == "VIETTEL" for m in merged)

def test_incomplete_multipart_flagged():
    merged = merge_parts(parse_backup(FIXTURES / "backup_multipart.ini"))
    orphan = next(m for m in merged if m.incomplete)
    assert orphan.text == "tail only"
```

- [ ] **Step 2: Run tests, expect failure**

Run: `pytest tests/test_parser.py -v`
Expected: FAIL, `ImportError: cannot import name 'merge_parts'`

- [ ] **Step 3: Implement UDH decode and merge**

`_parse_udh(udh: bytes)`: walk IEs after the length byte; `IEI 0x00, len 3` → `(ref, total, seq)`; `IEI 0x08, len 4` → 16-bit `ref`, `total`, `seq`; ignore others.

`merge_parts`: group parts with `concat_ref is not None` by `(number, folder, concat_ref)`; sort each group by `concat_seq`; text is the concatenation of available part texts; `parts = max(concat_total)`; `incomplete = {seq} != set(range(1, total+1))`; `state = "Read" if all parts are "Read" else "UnRead"`; `sent` is the earliest part. Parts without concat become one `Sms` each (`parts=1`, `incomplete=False`). Preserve input order for standalone messages.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_parser.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/sms_tui/parser.py tests
git commit -m "feat: merge multipart SMS via UDH"
```

---

### Task 3: SQLite archive

**Files:**
- Create: `src/sms_tui/store.py`
- Create: `tests/test_store.py`

**Interfaces:**
- Consumes: `Sms` from Task 2
- Produces:
  - `@dataclass class Thread: peer:str; display_name:str|None; last_ts:datetime; unread:int; preview:str`
  - `@dataclass class Message: id:int; peer:str; direction:str; ts:datetime; text:str; parts:int; status:str`
  - `class Store: __init__(self, path: Path); add_messages(self, msgs: list[Sms]) -> int; add_outgoing(self, peer: str, text: str, status: str = "sending") -> int; set_status(self, message_id: int, status: str) -> None; list_threads(self, query: str = "") -> list[Thread]; list_messages(self, peer: str) -> list[Message]; close(self) -> None`

- [ ] **Step 1: Write the failing tests**

```python
from datetime import datetime
from pathlib import Path
from sms_tui.store import Store
from sms_tui.parser import Sms

def sms(number="VIETTEL", text="OTP 123", ts="2026-11-24T15:09:26", state="UnRead"):
    return Sms(folder=1, state=state, number=number, sent=datetime.fromisoformat(ts),
               coding="Default", text=text, parts=1, incomplete=False)

def test_add_and_dedup(tmp_path):
    s = Store(tmp_path / "t.db")
    assert s.add_messages([sms()]) == 1
    assert s.add_messages([sms()]) == 0
    s.close()

def test_threads_sorted_and_unread(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add_messages([sms(ts="2026-11-24T10:00:00"), sms(text="newer", ts="2026-11-24T11:00:00")])
    s.add_messages([sms(number="+84901234567", ts="2026-11-24T12:00:00")])
    threads = s.list_threads()
    assert [t.peer for t in threads] == ["+84901234567", "VIETTEL"]
    assert threads[1].unread == 2  # both VIETTEL messages differ in text, both stored
    s.close()

def test_outgoing_roundtrip_vietnamese(tmp_path):
    s = Store(tmp_path / "t.db")
    mid = s.add_outgoing("+84901234567", "Xin chào Việt Nam")
    s.set_status(mid, "sent")
    msgs = s.list_messages("+84901234567")
    assert msgs[0].direction == "out" and msgs[0].status == "sent"
    assert msgs[0].text == "Xin chào Việt Nam"
    s.close()

def test_search_matches_text_and_number(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add_messages([sms(text="your OTP is 123456")])
    assert s.list_threads(query="123456")
    assert s.list_threads(query="VIET")
    s.close()
```

Note: dedup fingerprint is `(peer, ts iso, text)`; the two VIETTEL messages in
`test_threads_sorted_and_unread` differ by text (`OTP 123` vs `newer`), so both
are stored and the thread has 2 messages.

- [ ] **Step 2: Run tests, expect failure**

Run: `pytest tests/test_store.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'sms_tui.store'`

- [ ] **Step 3: Implement store**

Schema (execute on connect):

```sql
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  peer TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('in','out')),
  ts TEXT NOT NULL,
  text TEXT NOT NULL,
  parts INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT '',
  fingerprint TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS threads (
  peer TEXT PRIMARY KEY,
  display_name TEXT,
  last_ts TEXT NOT NULL,
  unread INTEGER NOT NULL DEFAULT 0
);
```

Fingerprint: `sha256(f"{direction}|{peer}|{ts.isoformat()}|{text}".encode()).hexdigest()`.

`add_messages`: insert with `INSERT OR IGNORE`, count `cur.rowcount`; upsert thread with `last_ts = max(...)` and `unread = unread + 1` for `UnRead` incoming. `add_outgoing`: insert direction `out`, status, upsert thread without incrementing unread. `list_threads`: SELECT threads ordered by `last_ts DESC`, filter `query` case-insensitively against `peer`, `display_name`, or message text via `EXISTS`; `preview` is the latest message text truncated to 60 chars. `list_messages`: ordered `ts ASC, id ASC`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_store.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/sms_tui/store.py tests/test_store.py
git commit -m "feat: sqlite archive with dedup and threads"
```

---

### Task 4: gammu backend

**Files:**
- Create: `src/sms_tui/backend.py`
- Create: `tests/test_backend.py`

**Interfaces:**
- Consumes: `parse_backup`, `merge_parts`
- Produces:
  - `class GammuError(RuntimeError)`
  - `class GammuBackend: __init__(self, gammu_bin: str = "gammu", config: Path | None = None); sync(self) -> list[Sms]; send(self, number: str, text: str) -> None; clear_inbox(self) -> None; identify(self) -> str`

- [ ] **Step 1: Write the failing tests**

`tests/test_backend.py` writes a fake `gammu` executable into `tmp_path`:

```python
import os, stat, json
from pathlib import Path
from sms_tui.backend import GammuBackend, GammuError

def fake_gammu(tmp_path, body, exit_code=0):
    p = tmp_path / "gammu"
    p.write_text("#!/usr/bin/env python3\nimport sys, json\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p

def test_sync_parses_backup(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "backup_single.ini"
    p = fake_gammu(tmp_path, f"open(sys.argv[2],'w').write(open({str(fixture)!r}).read())")
    be = GammuBackend(gammu_bin=str(p))
    msgs = be.sync()
    assert len(msgs) == 1 and msgs[0].number == "+84901234567"

def test_send_uses_textutf8(tmp_path):
    log = tmp_path / "args.json"
    p = fake_gammu(tmp_path, f"open({str(log)!r},'w').write(json.dumps(sys.argv))")
    GammuBackend(gammu_bin=str(p)).send("+84901234567", "Xin chào")
    args = json.loads(log.read_text())
    assert args[1:4] == ["sendsms", "TEXT", "+84901234567"]
    assert "-textutf8" in args and "Xin chào" in args

def test_failure_raises_gammu_error(tmp_path):
    p = fake_gammu(tmp_path, "sys.stderr.write('Error: busy\\n'); sys.exit(1)")
    try:
        GammuBackend(gammu_bin=str(p)).send("+8490", "x")
        assert False
    except GammuError as e:
        assert "busy" in str(e)

def test_clear_inbox_uses_deleteallsms(tmp_path):
    log = tmp_path / "args.json"
    p = fake_gammu(tmp_path, f"open({str(log)!r},'w').write(json.dumps(sys.argv))")
    GammuBackend(gammu_bin=str(p)).clear_inbox()
    assert json.loads(log.read_text())[1:] == ["deleteallsms", "1"]
```

- [ ] **Step 2: Run tests, expect failure**

Run: `pytest tests/test_backend.py -v`
Expected: FAIL, module missing

- [ ] **Step 3: Implement backend**

`_run(*args)` calls `subprocess.run([bin, *(["-c", str(config)] if config else []), *args], capture_output=True, text=True, timeout=60, env={**os.environ, "LC_ALL": "C.UTF-8"})`; on non-zero raise `GammuError(f"gammu {' '.join(args)} failed: {(stderr or stdout).strip()}")`. `sync` creates a `tempfile.NamedTemporaryFile(suffix=".ini")`, calls `backupsms <name> -all`, parses, merges, returns. `identify` returns stdout.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_backend.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/sms_tui/backend.py tests/test_backend.py
git commit -m "feat: gammu CLI backend"
```

---

### Task 5: Pure UI helpers (segments, wrapping)

**Files:**
- Create: `src/sms_tui/render.py`
- Create: `tests/test_render.py`

**Interfaces:**
- Produces:
  - `sms_segments(text: str) -> tuple[int, str]` → `(segments, "gsm7"|"ucs2")`
  - `wrap_text(text: str, width: int) -> list[str]`

- [ ] **Step 1: Write the failing tests**

```python
from sms_tui.render import sms_segments, wrap_text

def test_gsm7_limits():
    assert sms_segments("a" * 160) == (1, "gsm7")
    assert sms_segments("a" * 161) == (2, "gsm7")
    assert sms_segments("a" * 306) == (2, "gsm7")

def test_unicode_limits():
    assert sms_segments("ế" * 70) == (1, "ucs2")
    assert sms_segments("ế" * 71) == (2, "ucs2")

def test_wrap_preserves_words():
    lines = wrap_text("Xin chào Việt Nam hôm nay", 10)
    assert all(len(line) <= 10 for line in lines)
    assert "".join(lines).replace(" ", "") == "XinchàoViệtNamhômnay"
```

- [ ] **Step 2: Run tests, expect failure**

Run: `pytest tests/test_render.py -v`
Expected: FAIL, module missing

- [ ] **Step 3: Implement**

GSM-7 if every character is in the GSM 03.38 basic set plus `\n`, else UCS-2. Single part 160/70, multipart 153/67 per segment; `segments = ceil(len / per_segment)`, minimum 1. `wrap_text`: split on spaces, greedy fill, break words longer than width.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_render.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/sms_tui/render.py tests/test_render.py
git commit -m "feat: sms segment counter and text wrapping"
```

---

### Task 6: curses application

**Files:**
- Create: `src/sms_tui/tui.py`
- Create: `src/sms_tui/__main__.py`

**Interfaces:**
- Consumes: `Store`, `GammuBackend`, `render`
- Produces: `class App: __init__(self, store: Store, backend: GammuBackend); run(self) -> None`; `main() -> int` in `__main__.py`.

- [ ] **Step 1: Write the layout and state machine**

`App` holds `threads`, `selected_thread`, `messages`, `scroll`, `mode` (`"list"`, `"compose"`, `"confirm_clear"`), `input_buf`, `status`. Draw: top status line; left pane 30% thread rows (`peer` or `display_name`, `preview`, unread `*`); right pane wrapped message lines with `[You]`/`[peer]` and timestamp; bottom bar with segment counter when composing.

- [ ] **Step 2: Wire keys**

`q` quit; `j/k` and arrows move; `Tab` focus; `Enter` open thread / reply; `n` new number; `/` search prompt; `r` sync; `c` clear-SIM confirm (`y` confirms, any other key cancels); `Esc` cancel; `Ctrl-S` send in compose.

- [ ] **Step 3: Worker sync**

Start one daemon thread that calls `backend.sync()`, inserts into `Store`, and posts `("ok"|"err", payload)` to a `queue.Queue`. The UI drains the queue each loop tick (`stdscr.timeout(250)`). Sending runs `backend.send()` in the same worker, then `store.set_status(id, "sent")`; on `GammuError` set `"failed"` and banner text. While compose is open, do not start a sync (port exclusivity).

- [ ] **Step 4: Entrypoint**

`__main__.py`: resolve DB path from `SMS_TUI_DB` or `~/.local/share/gammu-sms-tui/messages.db`, create parent, `Store`, `GammuBackend`, `curses.wrapper(App(...).run)`, exit 0/1.

- [ ] **Step 5: Manual run**

Run: `PYTHONPATH=src python -m sms_tui`
Expected: split-pane window; existing threads listed; `r` refreshes; `q` exits cleanly restoring the terminal.

- [ ] **Step 6: Commit**

```bash
git add src/sms_tui/tui.py src/sms_tui/__main__.py
git commit -m "feat: curses sms client"
```

---

### Task 7: Docs, fixtures, real-device smoke test

**Files:**
- Create: `README.md`
- Create: `tests/fixtures/local/.gitkeep` (real captures live here, gitignored)
- Modify: `.gitignore`

**Interfaces:** none new.

- [ ] **Step 1: Capture a real backup**

```bash
gammu backupsms /tmp/real-inbox.ini -all
cp /tmp/real-inbox.ini tests/fixtures/local/real-inbox.ini
```

Confirm the parser handles it: `PYTHONPATH=src python -c "from sms_tui.parser import parse_backup, merge_parts; print(merge_parts(parse_backup('tests/fixtures/local/real-inbox.ini')))"`.
Add `tests/fixtures/local/` to `.gitignore` so personal SMS never enter git.

- [ ] **Step 2: Write README**

Cover: requirement that `qanh` is in `uucp` (`sudo usermod -aG uucp qanh`, then re-login) or run with sudo; `~/.gammurc` must point at `/dev/ttyXMM1` with `at115200`; run command; keybind table; SIM clearing warning; note that `AT+CPIN?`/`getallsms` can be misleading on RPC-mode firmware and that `backupsms` is the source of truth for this app.

- [ ] **Step 3: Smoke test on device**

1. `gammu -c ~/.gammurc identify` succeeds.
2. App lists the Viettel thread as 2 messages, not 31 parts.
3. Send `test` to own number, confirm arrival.
4. Archive then `c` to clear SIM inbox; confirm app still shows the thread.

- [ ] **Step 4: Commit**

```bash
git add README.md .gitignore tests/fixtures/local/.gitkeep
git commit -m "docs: usage, permissions, device smoke test"
```
