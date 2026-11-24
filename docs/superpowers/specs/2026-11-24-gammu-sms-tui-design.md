# gammu-sms-tui — Design

Date: 2026-11-24
Status: approved-in-chat (user supplied plan, corrected below)

## Intent

A small terminal SMS client for the XMM7360 modem in this laptop, readable at a
glance. Gammu is the engine. The pain today: `gammu getallsms` prints each part
of a concatenated SMS as its own numbered block, so one Viettel notice shows up
as 31 blocks.

## Goals

- Read SMS from the modem, with multipart messages reassembled into one message.
- Thread messages by phone number.
- Compose and send SMS (UTF-8, automatic segmentation).
- Keep a local archive, so clearing the SIM does not lose history.
- Zero new runtime dependencies beyond `gammu` (already installed).
- Split-pane `curses` UI: threads left, conversation right, compose bar bottom.

## Non-goals

- MMS, calls, contacts sync, delivery reports UI.
- GUI. `modem-manager-gui` cannot drive this RPC-mode modem; `wammu` is AUR-only.
- Multi-modem support.
- Notification daemon integration (optional later).

## Constraints

- Exclusive port: one `gammu` process at a time on `/dev/ttyXMM1`.
- Port access needs membership in `uucp` (or root). Documented in README.
- SIM storage is small (~30 slots). Archive locally, then clear SIM.
- `backupsms` has **no Location field**, so per-message deletion from SIM is not
  possible from backup data. Deletion is bulk "clear SIM inbox".

## Approaches considered

1. **Python + `curses` + `gammu` CLI** (chosen). Stdlib only, no new packages.
   Parsing `backupsms` is INI with a documented format.
2. Python + `urwid` + `gammu` CLI. Nicer widgets, but `urwid` is not in Arch
   repos (AUR), against the "light and simple" requirement.
3. Shell + `dialog` around `gammu`. Smallest, but no threads/search/scroll.

`python-gammu` would remove the parser, but the user asked for the CLI engine
and zero extra packages. The parser is isolated behind `parser.py`, so swapping
to `python-gammu` later is one file plus the backend.

## How it works

- **Sync:** `gammu backupsms <tmp> -all` (backs up all folders, keeps messages on
  the SIM). Parse each `SMSBackupNNN` section into one `SmsPart`.
- **Text:** prefer the decoded comment lines right after the section header;
  fall back to `Text00..TextNN` hex, decoded as UTF-16BE. Tests pin both.
- **Multipart:** decode UDH concatenation IE (0x00, or 0x08 for 16-bit ref),
  group by `(number, folder, ref)`, sort by sequence, join text.
- **Store:** SQLite at `~/.local/share/gammu-sms-tui/messages.db`. Tables
  `threads` and `messages`. Dedup by SHA-256 fingerprint of
  `(direction, peer, timestamp, text)`. Sent messages are logged locally.
- **Send:** `gammu sendsms TEXT <number> -textutf8 <text>`.
- **Clear SIM:** `gammu deleteallsms 1` after a successful import, with an
  explicit confirmation in the UI.
- **UI loop:** curses; a worker thread performs gammu calls and pushes results
  through a queue, so the UI never blocks on the modem.

## Decisions left to the builder

- Exact curses colors and box drawing.
- Whether background sync is a thread plus `queue`, or a select-driven loop.
  Either is fine; keep gammu calls off the UI thread.
- Test fixture contents beyond the two captured from the device.

## Review focus

- Vietnamese Unicode text and GSM-7 text both round-trip without mojibake.
- Incomplete multipart (some parts evicted from SIM) does not crash or silently
  drop the message; it is shown with the available parts.
- Alphanumeric senders (`VIETTEL`, bank shortcodes) are valid thread keys.
- `gammu` failure (busy port, no permission, SIM full) produces a banner message,
  not a traceback or a hung UI.
- Long compose text shows a correct segment count before sending.
