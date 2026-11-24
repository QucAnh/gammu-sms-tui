"""Pure text formatting and SMS segment calculation helpers."""

from __future__ import annotations

import math

# GSM 03.38 basic alphabet + newline/CR
_GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# GSM 03.38 extension table characters (consume 2 septets each)
_GSM7_EXT = frozenset("^{}\\[~]|€")


def gsm7_septet_len(text: str) -> int | None:
    """Return septet count if text fits GSM-7, else None."""
    total = 0
    for ch in text:
        if ch in _GSM7_BASIC:
            total += 1
        elif ch in _GSM7_EXT:
            total += 2
        else:
            return None
    return total


def sms_segments(text: str) -> tuple[int, str]:
    """Return (segment_count, 'gsm7'|'ucs2') for an outgoing SMS."""
    septets = gsm7_septet_len(text)
    if septets is not None:
        if septets <= 160:
            return 1, "gsm7"
        return math.ceil(septets / 153), "gsm7"

    units = len(text.encode("utf-16-be")) // 2
    if units <= 70:
        return 1, "ucs2"
    return math.ceil(units / 67), "ucs2"


def wrap_text(text: str, width: int) -> list[str]:
    """Wrap text onto lines of at most `width` characters, breaking long words."""
    if width <= 0:
        return [text]
    if not text:
        return [""]

    out: list[str] = []
    for paragraph in text.splitlines() or [""]:
        if not paragraph:
            out.append("")
            continue
        current = ""
        for word in paragraph.split(" "):
            if not word:
                if len(current) + 1 <= width:
                    current += " "
                continue
            while len(word) > width:
                if current:
                    out.append(current)
                    current = ""
                out.append(word[:width])
                word = word[width:]
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= width:
                current = f"{current} {word}"
            else:
                out.append(current)
                current = word
        if current:
            out.append(current)
    return out or [""]


def format_signal_bar(
    network: str,
    state: str,
    dbm: int | None,
    level: int | None,
) -> str:
    """Format a compact 5-bar signal + carrier status string."""
    pct = level
    if pct is None and dbm is not None:
        # Map typical GSM RSSI (-113..-51 dBm) to 0..100%
        pct = max(0, min(100, round((dbm + 113) * 100 / 62)))

    if pct is not None:
        pct = max(0, min(100, pct))
        filled = min(5, max(0, (pct + 10) // 20))
        bars = "#" * filled + "-" * (5 - filled)
        parts = [f"[{bars}] {pct}%"]
        if dbm is not None:
            parts.append(f"({dbm} dBm)")
        signal_txt = " ".join(parts)
    elif dbm is not None:
        signal_txt = f"{dbm} dBm"
    elif state in ("home", "home network"):
        signal_txt = "[ONLINE]"
    elif state in ("roaming", "roaming network"):
        signal_txt = "[ROAMING]"
    else:
        signal_txt = "[-----] --"

    net_parts = [p for p in (network, f"({state})" if state else "") if p]
    if net_parts:
        return f"Signal: {signal_txt} | {' '.join(net_parts)}"
    return f"Signal: {signal_txt}"
