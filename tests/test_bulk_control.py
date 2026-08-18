import time
from types import SimpleNamespace

from app.collect import _sleep


def test_stop_marker_interrupts_collector_wait(tmp_path):
    stop = tmp_path / "STOP_REQUESTED"
    stop.write_text("stop", encoding="utf-8")
    started = time.monotonic()
    _sleep(30, SimpleNamespace(stop_file=str(stop)))
    assert time.monotonic() - started < 1
