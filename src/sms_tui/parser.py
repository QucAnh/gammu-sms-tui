"""Parse Gammu SMS backup INI files and reassemble multipart SMS."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import re

_SECTION_RE = re.compile(r"(?m)^(?=\[SMSBackup)")
_TEXT_KEY_RE = re.compile(r"^Text(\d+)$")
_COLLISION_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class SmsPart:
    """Single physical SMS part parsed from a Gammu backup section."""

    folder: int
    state: str
    number: str
    sent: datetime
    coding: str
    text: str
    udh: bytes | None
    pdu: str = "Deliver"
    concat_ref: int | None = None
    concat_total: int | None = None
    concat_seq: int | None = None


@dataclass(frozen=True)
class Sms:
    """Logical SMS message after UDH multipart reassembly."""

    folder: int
    state: str
    number: str
    sent: datetime
    coding: str
    text: str
    parts: int
    incomplete: bool
    pdu: str = "Deliver"


def _decode_utf16be_hex(hex_str: str) -> str:
    """Decode big-endian UTF-16 hex payload without stripping meaningful spaces."""
    cleaned = "".join(hex_str.split())
    if not cleaned:
        return ""
    raw = bytes.fromhex(cleaned)
    return raw.decode("utf-16-be", errors="replace").rstrip("\x00\r\n")


def decode_text00(values: dict[str, str]) -> str:
    """Join Text00..TextNN fields in numeric order and decode as UTF-16BE."""
    numbered: list[tuple[int, str]] = []
    for key, val in values.items():
        m = _TEXT_KEY_RE.match(key)
        if m:
            numbered.append((int(m.group(1)), val))
    if not numbered:
        return ""
    numbered.sort(key=lambda item: item[0])
    combined_hex = "".join(val for _, val in numbered)
    return _decode_utf16be_hex(combined_hex)


def _strip_quotes(val: str) -> str:
    val = val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        return val[1:-1]
    return val


def _parse_timestamp(values: dict[str, str]) -> datetime:
    for key in ("Sent", "DateTime"):
        raw = values.get(key, "").strip()
        if not raw:
            continue
        try:
            return datetime.strptime(raw[:15], "%Y%m%dT%H%M%S")
        except ValueError:
            continue
    return datetime.min


def _parse_udh(udh: bytes | None) -> tuple[int | None, int | None, int | None]:
    """Extract (concat_ref, total_parts, seq_num) from UDH 0x00 or 0x08 IE."""
    if not udh:
        return None, None, None
    udhl = udh[0]
    end = min(len(udh), 1 + udhl)
    idx = 1
    while idx + 2 <= end:
        iei = udh[idx]
        ie_len = udh[idx + 1]
        data_start = idx + 2
        data_end = data_start + ie_len
        if data_end > end:
            break
        if iei == 0x00 and ie_len == 3:
            ref = udh[data_start]
            total = udh[data_start + 1]
            seq = udh[data_start + 2]
            if total >= 1 and seq >= 1:
                return ref, total, seq
        elif iei == 0x08 and ie_len == 4:
            ref = int.from_bytes(udh[data_start : data_start + 2], "big")
            total = udh[data_start + 2]
            seq = udh[data_start + 3]
            if total >= 1 and seq >= 1:
                return ref, total, seq
        idx = data_end
    return None, None, None


def _parse_comment_text(comment_lines: list[str]) -> str:
    stripped: list[str] = []
    for line in comment_lines:
        body = line[1:]
        if body.startswith(" "):
            body = body[1:]
        stripped.append(body)
    while stripped and stripped[-1] == "":
        stripped.pop()
    return "\n".join(stripped)


def _parse_section(section: str) -> SmsPart | None:
    lines = section.splitlines()
    if not lines or not lines[0].startswith("[SMSBackup"):
        return None

    comment_lines: list[str] = []
    in_comments = True
    values: dict[str, str] = {}

    for line in lines[1:]:
        if in_comments and (line.startswith("#") or line.startswith(";")):
            comment_lines.append(line)
            continue
        in_comments = False
        if not line.strip() or line.lstrip().startswith(("#", ";")):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        values[key.strip()] = _strip_quotes(val)

    coding = values.get("Coding", "Default") or "Default"
    text = ""
    if coding.lower() != "8bit" and any(_TEXT_KEY_RE.match(k) for k in values):
        text = decode_text00(values)
    if not text and comment_lines:
        text = _parse_comment_text(comment_lines)

    number = values.get("Number", "")
    if (not number or any(ord(ch) < 32 for ch in number)) and values.get("NumberUnicode"):
        decoded_num = _decode_utf16be_hex(values["NumberUnicode"])
        if decoded_num:
            number = decoded_num
    number = "".join("_" if ch == "\x11" else ch for ch in number if ord(ch) >= 32 or ch == "\x11")

    try:
        folder = int(values.get("Folder", "1") or "1")
    except ValueError:
        folder = 1

    udh_hex = values.get("UDH", "").strip()
    udh = bytes.fromhex(udh_hex) if udh_hex else None
    concat_ref, concat_total, concat_seq = _parse_udh(udh)

    return SmsPart(
        folder=folder,
        state=values.get("State", "Read") or "Read",
        number=number,
        sent=_parse_timestamp(values),
        coding=coding,
        text=text,
        udh=udh,
        pdu=values.get("PDU", "Deliver") or "Deliver",
        concat_ref=concat_ref,
        concat_total=concat_total,
        concat_seq=concat_seq,
    )


def parse_backup(path: Path) -> list[SmsPart]:
    """Parse a Gammu SMS backup INI file into physical SmsPart objects."""
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    parts: list[SmsPart] = []
    for block in _SECTION_RE.split(raw):
        part = _parse_section(block)
        if part is not None:
            parts.append(part)
    return parts


def _merged_state(group: list[SmsPart]) -> str:
    states = [p.state for p in group]
    if all(s == "Read" for s in states):
        return "Read"
    if any(s == "UnRead" for s in states):
        return "UnRead"
    if all(s == "Sent" for s in states):
        return "Sent"
    if any(s == "UnSent" for s in states):
        return "UnSent"
    return states[0]


def _split_collisions(group: list[SmsPart]) -> list[list[SmsPart]]:
    """Split parts sharing an 8-bit concat_ref when seq repeats or timestamps diverge."""
    buckets: list[list[SmsPart]] = []
    for part in group:
        placed = False
        for bucket in buckets:
            seqs = {p.concat_seq for p in bucket}
            if part.concat_seq in seqs:
                continue
            ref_ts = bucket[0].sent
            if (
                part.sent != datetime.min
                and ref_ts != datetime.min
                and abs(part.sent - ref_ts) > _COLLISION_WINDOW
            ):
                continue
            bucket.append(part)
            placed = True
            break
        if not placed:
            buckets.append([part])
    return buckets


def merge_parts(parts: list[SmsPart]) -> list[Sms]:
    """Reassemble multipart SMS via UDH concatenation headers."""
    grouped: dict[tuple[str, int, int, int], list[SmsPart]] = {}
    items: list[tuple[str, SmsPart | tuple[str, int, int, int]]] = []

    for part in parts:
        if part.concat_ref is None or part.concat_total is None or part.concat_seq is None:
            items.append(("single", part))
            continue
        key = (part.number, part.folder, part.concat_ref, part.concat_total)
        if key not in grouped:
            grouped[key] = []
            items.append(("group", key))
        grouped[key].append(part)

    result: list[Sms] = []
    for kind, payload in items:
        if kind == "single":
            p = payload  # type: ignore[assignment]
            result.append(
                Sms(
                    folder=p.folder,
                    state=p.state,
                    number=p.number,
                    sent=p.sent,
                    coding=p.coding,
                    text=p.text,
                    parts=1,
                    incomplete=False,
                    pdu=p.pdu,
                )
            )
            continue

        key = payload  # type: ignore[assignment]
        for bucket in _split_collisions(grouped[key]):
            ordered = sorted(bucket, key=lambda p: p.concat_seq or 0)
            total = max((p.concat_total or len(ordered)) for p in ordered)
            seqs = {p.concat_seq for p in ordered}
            incomplete = seqs != set(range(1, total + 1))
            valid_dates = [p.sent for p in ordered if p.sent != datetime.min]
            sent = min(valid_dates) if valid_dates else ordered[0].sent
            result.append(
                Sms(
                    folder=ordered[0].folder,
                    state=_merged_state(ordered),
                    number=ordered[0].number,
                    sent=sent,
                    coding=ordered[0].coding,
                    text="".join(p.text for p in ordered),
                    parts=total,
                    incomplete=incomplete,
                    pdu=ordered[0].pdu,
                )
            )

    return result
