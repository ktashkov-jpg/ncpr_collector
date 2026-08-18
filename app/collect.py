# -*- coding: utf-8 -*-
"""The collector loop: one worker, long random delays, hard stop rules.

Design stance: this process talks to a monitored government service over an
IP allowlist that can be withdrawn. Every ambiguous situation therefore
resolves toward stopping, not toward continuing. A stalled collector costs
days; a withdrawn allowlist costs the entire access path.

Safety properties, each of which has a specific failure it prevents:

  * daily cap lives in SQLite, keyed by date   -> survives container restart
  * the cap is consumed BEFORE the request     -> a crash mid-request still counts
  * 403/429 write a HALTED file                -> refuses to run again without a human
  * single-instance lock                       -> two containers can't double the rate
  * raw XML archived before normalisation      -> interrupted runs stay auditable
  * WSDL fetched once and hashed               -> definitions aren't re-fetched per product
"""
from __future__ import annotations

import datetime as dt
import os
import random
import signal
import time
from pathlib import Path

from app import soap
from app.config import Config
from app.store import Store

RUNNING = True


def _stop(signum, frame):
    global RUNNING
    RUNNING = False
    log("signal received - finishing current wait, then exiting cleanly")


def log(message: str) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def today(config: Config) -> str:
    return dt.datetime.now().strftime("%Y-%m-%d")


def in_window(config: Config) -> bool:
    hour = dt.datetime.now().hour
    return config.window_start_hour <= hour < config.window_end_hour


def seconds_until_window(config: Config) -> int:
    now = dt.datetime.now()
    target = now.replace(hour=config.window_start_hour, minute=0, second=0,
                         microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return int((target - now).total_seconds())


def halt(config: Config, store: Store, reason: str) -> None:
    Path(config.halt_file).write_text(
        f"{dt.datetime.now(dt.timezone.utc).isoformat()}\n{reason}\n",
        encoding="utf-8")
    store.log(note=f"HALT: {reason}")
    log("=" * 68)
    log(f"HARD STOP: {reason}")
    log(f"Wrote {config.halt_file}. The collector will refuse to start until")
    log("a human removes that file. Confirm allowlist status with NCPR or the")
    log("university network administrator before resuming.")
    log("=" * 68)


def acquire_lock(config: Config) -> None:
    """Refuse to start a second collector against the same data volume."""
    path = Path(config.lock_file)
    if path.exists():
        pid = path.read_text(encoding="utf-8").strip()
        raise SystemExit(
            f"Lock file {path} exists (pid {pid}). Another collector may be "
            f"running. If you are certain it is not, remove the file.")
    path.write_text(str(os.getpid()), encoding="utf-8")


def release_lock(config: Config) -> None:
    Path(config.lock_file).unlink(missing_ok=True)


def cache_wsdl(config: Config, store: Store, opener) -> str:
    """Fetch the WSDL once and record its hash; a change means the contract
    may have moved and the run should be reviewed (runbook §9, §11)."""
    known = store.get_meta("wsdl_sha256")
    if known:
        return known
    import urllib.request
    request = urllib.request.Request(config.endpoint + "?wsdl")
    with opener.open(request, timeout=config.timeout_s) as response:
        body = response.read()
    digest = soap.sha256(body)
    Path(config.wsdl_path).write_bytes(body)
    store.set_meta("wsdl_sha256", digest)
    log(f"cached WSDL ({len(body)} bytes) sha256={digest[:16]}...")
    return digest


def handle(store: Store, config: Config, task, body: bytes,
           http_status: int, elapsed: int, wsdl_hash: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc)
    raw_name = f"{task['task_id'].replace(':', '_')}_{stamp:%Y%m%dT%H%M%SZ}.xml"
    raw_path = Path(config.raw_dir) / raw_name
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(body)                      # archive BEFORE parsing

    parsed = soap.parse(body)
    store.log(task_id=task["task_id"], http_status=http_status,
              soap_fault=parsed.get("fault"), elapsed_ms=elapsed,
              bytes=len(body), wsdl_sha256=wsdl_hash)

    if parsed.get("fault"):
        # Runbook §10: record and continue on the normal schedule.
        store.finish(task["task_id"], "fault", parsed["fault"])
        store.audit("bulk_collect", "soap_fault", product_id=task["key"],
                    soap_action=task["kind"], http_status=http_status,
                    detail=parsed["fault"][:300])
        log(f"  SOAP fault: {parsed['fault'][:80]}")
        return

    gtins = parsed.get("gtins", [])
    if not gtins:
        # Normal for non-PLS packages -- SESPA holds GTINs only for the
        # Positive Drug List. Not an error, and not worth a retry.
        store.finish(task["task_id"], "no_gtin")
        store.audit("bulk_collect", "no_gtin", product_id=task["key"],
                    soap_action=task["kind"], http_status=http_status)
        log("  no GTIN (expected for non-PLS packages)")
        return

    for gtin in gtins:
        store.save_product(
            task_id=task["task_id"],
            medicinal_product_identifier=parsed.get("medicinal_product_identifier"),
            gtin_raw=gtin,
            gtin14=soap.gtin14(gtin),
            ean13_derived=soap.ean13_from(gtin),
            checksum_valid=1 if soap.valid_gtin(gtin) else 0,
            expected_check_digit=soap.expected_check_digit(gtin),
            indicator_digit=soap.indicator_digit(soap.gtin14(gtin)),
            name_bg=parsed.get("name_bg"),
            name_en=parsed.get("name_en"),
            authorization_number=parsed.get("authorization_number"),
            final_pack=parsed.get("final_pack"),
            retrieved_at=stamp.isoformat(),
            raw_path=str(raw_path),
            raw_sha256=soap.sha256(body),
        )
    store.finish(task["task_id"], "done")
    store.audit("bulk_collect", "success", product_id=task["key"],
                soap_action=task["kind"], http_status=http_status,
                local_modified=True)
    log(f"  {len(gtins)} GTIN(s): {', '.join(gtins)}  "
        f"{(parsed.get('name_en') or parsed.get('name_bg') or '')[:36]}")


def main() -> None:
    global RUNNING
    RUNNING = True
    config = Config()
    config.validate()
    if Path(config.halt_file).exists():
        raise SystemExit(
            f"{config.halt_file} present - a previous run hard-stopped.\n"
            f"{Path(config.halt_file).read_text(encoding='utf-8')}\n"
            "Resolve with NCPR, then delete the file to resume.")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    config.ensure_dirs()
    store = Store(config.db_path)
    acquire_lock(config)
    final_state = "stopped"
    try:
        store.set_bulk_state("running", pid=os.getpid())
        log(f"db      : {config.db_dir}")
        log(f"archive : {config.archive_dir}")
        if config.insecure_tls:
            log("WARNING: TLS verification disabled (NCPR_INSECURE_TLS=1). "
                "Fix the host CA store and turn this off.")
        opener = soap.make_opener(config.insecure_tls)
        wsdl_hash = cache_wsdl(config, store, opener)

        consecutive_5xx = 0
        log(f"policy: 1 worker | delay {config.delay_min_s}-{config.delay_max_s}s "
            f"| cap {config.daily_cap}/day | window "
            f"{config.window_start_hour:02d}:00-{config.window_end_hour:02d}:00")
        log(f"queue: {store.queue_stats()}")

        while RUNNING:
            if Path(config.stop_file).exists():
                log("operator stop requested - preserving queue and exiting")
                break
            day = today(config)
            used = store.used_today(day)
            if used >= config.daily_cap:
                wait = seconds_until_window(config)
                log(f"daily cap reached ({used}/{config.daily_cap}); "
                    f"sleeping {wait // 3600}h until the next window")
                _sleep(wait, config)
                continue
            if not in_window(config):
                wait = seconds_until_window(config)
                log(f"outside operating window; sleeping {wait // 3600}h {wait % 3600 // 60}m")
                _sleep(wait, config)
                continue

            task = store.next_task()
            if task is None:
                log("queue empty - nothing pending. Exiting.")
                final_state = "completed"
                break

            operation = soap.FORWARD if task["kind"] == "forward" else soap.REVERSE
            used = store.consume(day)           # count before sending
            log(f"[{used}/{config.daily_cap}] {task['task_id']} "
                f"(p{task['priority']}) {task['reason'][:46]}")

            try:
                status, body, elapsed = soap.call(
                    opener, config.endpoint, config.namespace, operation,
                    task["key"], config.timeout_s)
                consecutive_5xx = 0
                handle(store, config, task, body, status, elapsed, wsdl_hash)
            except soap.HardStop as exc:
                store.defer(task["task_id"], str(exc))
                store.audit("bulk_collect", "hard_stop", product_id=task["key"],
                            soap_action=task["kind"], detail=str(exc))
                halt(config, store, str(exc))
                final_state = "halted"
                break
            except soap.Transient as exc:
                consecutive_5xx += 1
                store.defer(task["task_id"], str(exc))
                store.log(task_id=task["task_id"], note=f"transient: {exc}")
                store.audit("bulk_collect", "transient_failure", product_id=task["key"],
                            soap_action=task["kind"], detail=type(exc).__name__)
                # Runbook §10: 30 min, then 2 h, then stop for the day.
                if consecutive_5xx == 1:
                    log(f"  transient ({exc}); waiting 30 min")
                    _sleep(30 * 60, config)
                elif consecutive_5xx == 2:
                    log(f"  transient ({exc}); waiting 2 h")
                    _sleep(2 * 3600, config)
                else:
                    log(f"  third consecutive failure ({exc}); stopping for today")
                    _sleep(seconds_until_window(config), config)
                    consecutive_5xx = 0
                continue

            delay = random.randint(config.delay_min_s, config.delay_max_s)
            log(f"  sleeping {delay}s")
            _sleep(delay, config)

        log(f"final queue state: {store.queue_stats()}")
    finally:
        release_lock(config)
        Path(config.stop_file).unlink(missing_ok=True)
        store.set_bulk_state(final_state, note="queue state preserved")


def _sleep(seconds: int, config: Config | None = None) -> None:
    """Interruptible sleep so SIGTERM does not wait out a 10-minute delay."""
    end = time.time() + seconds
    while (RUNNING and time.time() < end and
           not (config and Path(config.stop_file).exists())):
        time.sleep(min(5, max(0, end - time.time())))


if __name__ == "__main__":
    main()
