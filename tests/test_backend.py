import json
import os
from pathlib import Path
import pty
import stat
import threading

from sms_tui.backend import GammuBackend, GammuError, parse_at_signal, parse_signal_output


def fake_gammu(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "gammu"
    p.write_text("#!/usr/bin/env python3\nimport sys, json\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def test_sync_parses_backup_and_defaults_to_yes(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "backup_single.ini"
    log = tmp_path / "args.json"
    p = fake_gammu(
        tmp_path,
        f"open({str(log)!r}, 'w').write(json.dumps(sys.argv)); "
        f"open(sys.argv[2], 'w').write(open({str(fixture)!r}).read())",
    )
    be = GammuBackend(gammu_bin=str(p))
    msgs = be.sync()
    assert len(msgs) == 1
    assert msgs[0].number == "+84901234567"
    assert json.loads(log.read_text())[3] == "-yes"


def test_sync_can_preserve_sim_with_auto_clear_false(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "backup_single.ini"
    log = tmp_path / "args.json"
    p = fake_gammu(
        tmp_path,
        f"open({str(log)!r}, 'w').write(json.dumps(sys.argv)); "
        f"open(sys.argv[2], 'w').write(open({str(fixture)!r}).read())",
    )
    be = GammuBackend(gammu_bin=str(p), auto_clear=False)
    be.sync()
    assert json.loads(log.read_text())[3] == "-all"


def test_send_ascii_omits_unicode_flag(tmp_path):
    log = tmp_path / "args.json"
    p = fake_gammu(tmp_path, f"open({str(log)!r}, 'w').write(json.dumps(sys.argv))")
    GammuBackend(gammu_bin=str(p)).send("+84901234567", "Hello")
    args = json.loads(log.read_text())
    assert args[1:4] == ["sendsms", "TEXT", "+84901234567"]
    assert "-textutf8" in args and "Hello" in args
    assert "-unicode" not in args


def test_send_vietnamese_adds_unicode_and_textutf8(tmp_path):
    log = tmp_path / "args.json"
    p = fake_gammu(tmp_path, f"open({str(log)!r}, 'w').write(json.dumps(sys.argv))")
    GammuBackend(gammu_bin=str(p)).send("+84901234567", "Xin chào Việt Nam")
    args = json.loads(log.read_text())
    assert args[1:4] == ["sendsms", "TEXT", "+84901234567"]
    assert "-unicode" in args
    assert "-textutf8" in args and "Xin chào Việt Nam" in args


def test_failure_raises_gammu_error(tmp_path):
    p = fake_gammu(tmp_path, "sys.stdout.write('Sending failed\\n'); sys.stderr.write('Error: busy\\n'); sys.exit(1)")
    try:
        GammuBackend(gammu_bin=str(p)).send("+8490", "x")
        assert False, "expected GammuError"
    except GammuError as e:
        assert "busy" in str(e)
        assert "Sending failed" in str(e)


def test_clear_inbox_uses_deleteallsms(tmp_path):
    log = tmp_path / "args.json"
    p = fake_gammu(tmp_path, f"open({str(log)!r}, 'w').write(json.dumps(sys.argv))")
    GammuBackend(gammu_bin=str(p)).clear_inbox()
    assert json.loads(log.read_text())[1:] == ["deleteallsms", "1"]


def test_parse_signal_output_and_fallback(tmp_path):
    sample = (
        "Network state             : home network\n"
        'Network name for Gammu    : "Viettel"\n'
        "Signal strength           : -69 dBm\n"
        "Network level             : 80 %\n"
    )
    sig = parse_signal_output(sample)
    assert sig.network == "Viettel"
    assert sig.state == "home network"
    assert sig.dbm == -69
    assert sig.level == 80

    p = fake_gammu(
        tmp_path,
        "print('Network state             : home network')\n"
        "print('Network name for Gammu    : \"Viettel\"')\n",
    )
    fb = GammuBackend(gammu_bin=str(p), config=tmp_path / "missing.gammurc").signal_status()
    assert fb.network == "Viettel"
    assert fb.state == "home network"


def test_parse_at_signal_and_pty_serial_probe(tmp_path):
    parsed = parse_at_signal(
        "+CSQ: 22,99\r\n"
        '+COPS: 0,0,"VIETTEL",7\r\n'
        "+CEREG: 0,1\r\n"
        "+CREG: 0,0\r\n"
        "OK\r\n"
    )
    assert parsed.network == "VIETTEL LTE"
    assert parsed.state == "home"
    assert parsed.dbm == -69
    assert parsed.level == 71

    cesq = parse_at_signal(
        "+CSQ: 99,99\r\n"
        "+CESQ: 99,99,255,255,20,45\r\n"
        '+COPS: 0,0,"Viettel",7\r\n'
        "+CEREG: 0,1\r\n"
        "OK\r\n"
    )
    assert cesq.dbm == -95
    assert cesq.level == 56
    assert cesq.network == "Viettel LTE"
    assert cesq.state == "home"

    xcesq = parse_at_signal(
        "+CSQ: 99,99\r\n"
        "+CESQ: 99,99,255,255,255,255\r\n"
        "+XCESQ: 0,99,99,255,255,22,50,10\r\n"
        '+COPS: 0,0,"Viettel",7\r\n'
        "+CEREG: 0,1\r\n"
    )
    assert xcesq.dbm == -90
    assert xcesq.level == 67

    master, slave = pty.openpty()
    slave_name = os.ttyname(slave)
    cfg = tmp_path / "gammurc"
    cfg.write_text(f"[gammu]\ndevice = {slave_name}\nconnection = at115200\n", encoding="utf-8")

    def responder():
        try:
            os.read(master, 256)
            os.write(
                master,
                b'+CSQ: 20,99\r\n+COPS: 0,0,"VIETTEL",7\r\n+CEREG: 0,1\r\nOK\r\n',
            )
        except OSError:
            pass

    t = threading.Thread(target=responder, daemon=True)
    t.start()
    try:
        be = GammuBackend(gammu_bin="/bin/false", config=cfg)
        sig = be.signal_status()
        assert sig.network == "VIETTEL LTE"
        assert sig.state == "home"
        assert sig.dbm == -73
        assert sig.level == 65
    finally:
        os.close(master)
        os.close(slave)
        t.join(timeout=1)
