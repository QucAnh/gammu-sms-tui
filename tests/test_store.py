from datetime import datetime

from sms_tui.parser import Sms
from sms_tui.store import Store, normalize_peer


def sms(
    number="VIETTEL",
    text="OTP 123",
    ts="2026-11-24T15:09:26",
    state="UnRead",
    pdu="Deliver",
):
    return Sms(
        folder=1,
        state=state,
        number=number,
        sent=datetime.fromisoformat(ts),
        coding="Default",
        text=text,
        parts=1,
        incomplete=False,
        pdu=pdu,
    )


def test_normalize_peer_merges_local_and_e164():
    assert normalize_peer("0901234567") == "+84901234567"
    assert normalize_peer("84901234567") == "+84901234567"
    assert normalize_peer("+84 901 234 567") == "+84901234567"
    assert normalize_peer("VIETTEL") == "VIETTEL"
    assert normalize_peer("191") == "191"


def test_add_and_dedup_does_not_inflate_unread(tmp_path):
    s = Store(tmp_path / "t.db")
    assert s.add_messages([sms()]) == 1
    assert s.add_messages([sms()]) == 0
    threads = s.list_threads()
    assert len(threads) == 1
    assert threads[0].unread == 1
    s.close()


def test_threads_sorted_and_unread_and_mark_read(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add_messages([sms(ts="2026-11-24T10:00:00"), sms(text="newer", ts="2026-11-24T11:00:00")])
    s.add_messages([sms(number="+84901234567", ts="2026-11-24T12:00:00")])
    threads = s.list_threads()
    assert [t.peer for t in threads] == ["+84901234567", "VIETTEL"]
    assert threads[1].unread == 2

    s.mark_read("VIETTEL")
    threads = s.list_threads()
    assert threads[1].unread == 0
    s.close()


def test_same_number_formats_share_thread(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add_messages([sms(number="+84901234567", text="from intl", ts="2026-11-24T10:00:00")])
    s.add_outgoing("0901234567", "reply local")
    threads = s.list_threads()
    assert len(threads) == 1
    assert threads[0].peer == "+84901234567"
    assert len(s.list_messages("0901234567")) == 2
    s.close()


def test_outgoing_roundtrip_vietnamese(tmp_path):
    s = Store(tmp_path / "t.db")
    mid = s.add_outgoing("+84901234567", "Xin chào Việt Nam")
    s.set_status(mid, "sent")
    msgs = s.list_messages("+84901234567")
    assert msgs[0].direction == "out"
    assert msgs[0].status == "sent"
    assert msgs[0].text == "Xin chào Việt Nam"
    s.close()


def test_sent_pdu_and_status_report_from_backup(tmp_path):
    s = Store(tmp_path / "t.db")
    inserted = s.add_messages(
        [
            sms(number="0901234567", text="saved out", state="Sent", pdu="Submit"),
            sms(number="0901234567", text="delivered", state="Read", pdu="Status_Report"),
        ]
    )
    assert inserted == 1
    msgs = s.list_messages("+84901234567")
    assert len(msgs) == 1
    assert msgs[0].direction == "out"
    assert s.list_threads()[0].unread == 0
    s.close()


def test_search_matches_text_and_number(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add_messages([sms(text="your OTP is 123456")])
    assert s.list_threads(query="123456")
    assert s.list_threads(query="viet")
    assert not s.list_threads(query="nomatch")
    s.close()
