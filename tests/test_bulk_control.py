import time
from types import SimpleNamespace

from app.collect import _sleep, _transaction_delay


def test_transaction_delay_uses_bounded_low_skew():
    class RecordingRandom:
        def triangular(self, low, high, mode):
            assert (low, mode, high) == (180, 240, 480)
            return 239.6

    config = SimpleNamespace(
        delay_min_s=180, delay_mode_s=240, delay_max_s=480)
    assert _transaction_delay(config, RecordingRandom()) == 240


def test_stop_marker_interrupts_collector_wait(tmp_path):
    stop = tmp_path / "STOP_REQUESTED"
    stop.write_text("stop", encoding="utf-8")
    started = time.monotonic()
    _sleep(30, SimpleNamespace(stop_file=str(stop)))
    assert time.monotonic() - started < 1
