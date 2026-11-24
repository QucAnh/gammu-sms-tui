from datetime import datetime
from pathlib import Path

from sms_tui.parser import SmsPart, decode_text00, merge_parts, parse_backup

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


def test_decode_text00_strips_trailing_null_and_newline():
    assert decode_text00({"Text00": "00410042000A0000"}) == "AB"


def test_parse_udh_8bit():
    parts = parse_backup(FIXTURES / "backup_multipart.ini")
    p = parts[0]
    assert (p.concat_ref, p.concat_total, p.concat_seq) == (0xAB, 2, 1)


def test_merge_multipart_in_sequence_order():
    merged = merge_parts(parse_backup(FIXTURES / "backup_multipart.ini"))
    msg = next(m for m in merged if m.number == "+84901234567" and not m.incomplete)
    assert msg.text == "Xin chào Việt Nam"
    assert msg.parts == 2
    assert msg.incomplete is False


def test_alphanumeric_sender_kept():
    merged = merge_parts(parse_backup(FIXTURES / "backup_multipart.ini"))
    assert any(m.number == "VIETTEL" for m in merged)


def test_incomplete_multipart_flagged():
    merged = merge_parts(parse_backup(FIXTURES / "backup_multipart.ini"))
    orphan = next(m for m in merged if m.incomplete)
    assert orphan.text == "tail only"


def test_udh_16bit_and_ref_collision_split():
    parts = [
        SmsPart(
            folder=1,
            state="Read",
            number="VIETTEL",
            sent=datetime(2026, 11, 20, 10, 0, 0),
            coding="Default",
            text="Old1 ",
            udh=bytes.fromhex("06080412340201"),
            concat_ref=0x1234,
            concat_total=2,
            concat_seq=1,
        ),
        SmsPart(
            folder=1,
            state="Read",
            number="VIETTEL",
            sent=datetime(2026, 11, 20, 10, 0, 1),
            coding="Default",
            text="Old2",
            udh=bytes.fromhex("06080412340202"),
            concat_ref=0x1234,
            concat_total=2,
            concat_seq=2,
        ),
        SmsPart(
            folder=1,
            state="UnRead",
            number="VIETTEL",
            sent=datetime(2026, 11, 24, 10, 0, 0),
            coding="Default",
            text="New1",
            udh=bytes.fromhex("06080412340201"),
            concat_ref=0x1234,
            concat_total=2,
            concat_seq=1,
        ),
    ]
    merged = merge_parts(parts)
    assert len(merged) == 2
    assert merged[0].text == "Old1 Old2"
    assert merged[0].incomplete is False
    assert merged[1].text == "New1"
    assert merged[1].incomplete is True


def test_sent_state_preserved_on_merge():
    parts = [
        SmsPart(
            folder=2,
            state="Sent",
            number="+84901234567",
            sent=datetime(2026, 11, 24, 12, 0, 0),
            coding="Default",
            text="Part1 ",
            udh=None,
            pdu="Submit",
            concat_ref=1,
            concat_total=2,
            concat_seq=1,
        ),
        SmsPart(
            folder=2,
            state="Sent",
            number="+84901234567",
            sent=datetime(2026, 11, 24, 12, 0, 1),
            coding="Default",
            text="Part2",
            udh=None,
            pdu="Submit",
            concat_ref=1,
            concat_total=2,
            concat_seq=2,
        ),
    ]
    merged = merge_parts(parts)
    assert len(merged) == 1
    assert merged[0].state == "Sent"
    assert merged[0].pdu == "Submit"


def test_alphanumeric_sender_control_char_falls_back_to_number_unicode(tmp_path):
    ini = tmp_path / "sender.ini"
    ini.write_text(
        "[SMSBackup000]\n"
        "#Hello\n"
        'Number = "VIETTEL\x115G"\n'
        "NumberUnicode = 005600490045005400540045004C005F00350047\n"
        "Sent = 20261124T150926\n"
        "State = Read\n"
        "Folder = 1\n",
        encoding="utf-8",
    )
    parts = parse_backup(ini)
    assert parts[0].number == "VIETTEL_5G"
