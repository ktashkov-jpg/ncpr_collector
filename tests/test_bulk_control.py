import multiprocessing
import time
from types import SimpleNamespace

import pytest

from app.collect import (
    CollectorLockHeld, _sleep, _transaction_delay, acquire_lock,
    lock_is_held, release_lock,
)


def _hold_lock(path: str, ready) -> None:
    handle = acquire_lock(SimpleNamespace(lock_file=path))
    ready.set()
    time.sleep(30)
    release_lock(handle)


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


def test_stale_pid_record_does_not_claim_runtime_lock(tmp_path):
    path = tmp_path / "collector.lock"
    path.write_text("20", encoding="utf-8")
    config = SimpleNamespace(lock_file=str(path))

    assert lock_is_held(config) is False
    handle = acquire_lock(config)
    try:
        assert lock_is_held(config) is True
        with pytest.raises(CollectorLockHeld):
            acquire_lock(config)
    finally:
        release_lock(handle)

    assert path.exists()
    assert lock_is_held(config) is False


def test_runtime_lock_is_released_when_process_is_terminated(tmp_path):
    path = str(tmp_path / "collector.lock")
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(target=_hold_lock, args=(path, ready))
    process.start()
    try:
        assert ready.wait(10)
        assert lock_is_held(SimpleNamespace(lock_file=path)) is True
        process.terminate()
        process.join(10)
        assert not process.is_alive()
        assert lock_is_held(SimpleNamespace(lock_file=path)) is False
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
