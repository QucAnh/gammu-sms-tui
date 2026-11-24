from sms_tui.render import format_signal_bar, sms_segments, wrap_text


def test_gsm7_limits():
    assert sms_segments("") == (1, "gsm7")
    assert sms_segments("a" * 160) == (1, "gsm7")
    assert sms_segments("a" * 161) == (2, "gsm7")
    assert sms_segments("a" * 306) == (2, "gsm7")


def test_unicode_limits():
    assert sms_segments("ế" * 70) == (1, "ucs2")
    assert sms_segments("ế" * 71) == (2, "ucs2")


def test_wrap_preserves_words_and_breaks_long_tokens():
    lines = wrap_text("Xin chào Việt Nam hôm nay", 10)
    assert all(len(line) <= 10 for line in lines)
    assert "".join(lines).replace(" ", "") == "XinchàoViệtNamhômnay"

    broken = wrap_text("abcdefghijkl", 5)
    assert broken == ["abcde", "fghij", "kl"]


def test_format_signal_bar():
    line = format_signal_bar("Viettel", "home network", -69, 80)
    assert "[####-] 80% (-69 dBm)" in line
    assert "Viettel (home network)" in line

    dbm_only = format_signal_bar("", "", -82, None)
    assert "dBm" in dbm_only
    assert "50%" in dbm_only

    empty = format_signal_bar("Viettel", "home network", None, None)
    assert "Signal: [ONLINE] | Viettel (home network)" == empty

    roaming = format_signal_bar("Viettel", "roaming", None, None)
    assert "Signal: [ROAMING] | Viettel (roaming)" == roaming

    no_service = format_signal_bar("", "no service", None, None)
    assert "[-----] -- | (no service)" in no_service
