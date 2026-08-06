# -*- coding: utf-8 -*-
"""Single ad-hoc lookup, through the audited path.

Same archiving, request logging, cap accounting and stop rules as the
collector — the point is that a one-off diagnostic leaves the same evidence
trail as a scheduled call, rather than being a curl invocation nobody can
reconstruct later.

Designed for the reverse-lookup experiment on the invalid value NCPR
returned for national id 758:

    python -m app.probe --gtin 50085412959961    # the value as supplied
    python -m app.probe --gtin 50085412959967    # the checksum-correct value

| …961 resolves | …967 resolves | conclusion                                  |
|---------------|---------------|---------------------------------------------|
| yes           | no            | СЕСПА stores the invalid value; the error   |
|               |               | is in the stored data                       |
| no            | yes           | СЕСПА stores the valid value; the error is  |
|               |               | introduced on the way out                   |
| yes           | yes           | both indexed                                |
| no            | no            | reverse index is keyed on something else —  |
|               |               | inconclusive, not evidence of absence       |

Two requests total.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from app import soap
from app.config import Config
from app.store import Store


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.probe")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--gtin", help="reverse lookup (getMedicinalProductDataByGTIN)")
    group.add_argument("--natid", help="forward lookup (getMedicinalProductDataWithGTIN)")
    args = parser.parse_args()

    config = Config()
    config.validate()
    if Path(config.halt_file).exists():
        raise SystemExit(f"{config.halt_file} present — resolve before probing.")
    config.ensure_dirs()

    store = Store(config.db_path)
    day = dt.datetime.now().strftime("%Y-%m-%d")
    used = store.used_today(day)
    if used >= config.daily_cap:
        raise SystemExit(f"daily cap reached ({used}/{config.daily_cap}).")

    operation = soap.REVERSE if args.gtin else soap.FORWARD
    key = args.gtin or args.natid
    opener = soap.make_opener(config.insecure_tls)

    if args.gtin:
        print(f"value      {args.gtin}")
        print(f"checksum   {'valid' if soap.valid_gtin(args.gtin) else 'INVALID'}")
        expected = soap.expected_check_digit(args.gtin)
        if expected:
            print(f"expected   last digit {expected} "
                  f"(-> {args.gtin[:-1]}{expected})")
        indicator = soap.indicator_digit(soap.gtin14(args.gtin))
        if indicator:
            note = ("consumer unit" if indicator == "0"
                    else "variable measure" if indicator == "9"
                    else "HIGHER PACKAGING LEVEL — not a dispensed-pack barcode")
            print(f"indicator  {indicator} ({note})")

    store.consume(day)
    print(f"\n[{used + 1}/{config.daily_cap}] {operation}({key})")
    started = time.time()
    try:
        status, body, elapsed = soap.call(
            opener, config.endpoint, config.namespace, operation,
            key, config.timeout_s)
    except soap.HardStop as exc:
        print(f"HARD STOP: {exc}")
        store.log(task_id=f"probe:{key}", note=f"HARD STOP: {exc}")
        return 1
    except soap.Transient as exc:
        print(f"transient failure: {exc}")
        store.log(task_id=f"probe:{key}", note=f"transient: {exc}")
        return 1

    stamp = dt.datetime.now(dt.timezone.utc)
    raw = Path(config.raw_dir) / f"probe_{key}_{stamp:%Y%m%dT%H%M%SZ}.xml"
    raw.write_bytes(body)

    parsed = soap.parse(body)
    store.log(task_id=f"probe:{key}", http_status=status,
              soap_fault=parsed.get("fault"), elapsed_ms=elapsed,
              bytes=len(body), note="probe")

    print(f"HTTP {status}  {elapsed} ms  {len(body)} bytes")
    print(f"archived {raw}")
    if parsed.get("fault"):
        print(f"\nSOAP fault: {parsed['fault']}")
        print("-> this value does NOT resolve")
        return 0
    if not parsed.get("medicinal_product_identifier"):
        print("\nno product returned -> this value does NOT resolve")
        return 0

    print("\nRESOLVES:")
    print(json.dumps({k: v for k, v in parsed.items()},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
