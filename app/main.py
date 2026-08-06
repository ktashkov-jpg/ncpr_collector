# -*- coding: utf-8 -*-
"""CLI entry point.

    python -m app.main doctor       check config, TLS, egress IP, WSDL
    python -m app.main build-queue  populate the work queue from Annex 4
    python -m app.main collect      run the collector loop
    python -m app.main status       queue / counter / results summary
    python -m app.main export       write the GTIN <-> pfid crosswalk CSV

`doctor` exists because the single most likely failure on this service is
running from the wrong egress IP -- an SSH session alone does not make
requests originate from the whitelisted host (runbook §3).
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from app.config import Config
from app.store import Store


def cmd_doctor(args: argparse.Namespace) -> int:
    from app import soap
    import urllib.request

    config = Config()
    config.validate()
    print(f"endpoint     : {config.endpoint}")
    print(f"data dir     : {config.data_dir}")
    print(f"delay        : {config.delay_min_s}-{config.delay_max_s}s")
    print(f"daily cap    : {config.daily_cap}")
    print(f"window       : {config.window_start_hour:02d}:00-"
          f"{config.window_end_hour:02d}:00 local")
    print(f"insecure TLS : {config.insecure_tls}")

    opener = soap.make_opener(config.insecure_tls)
    print("\negress IP (must match the address registered with NCPR):")
    try:
        with opener.open("https://api.ipify.org", timeout=20) as response:
            print(f"  {response.read().decode().strip()}")
    except Exception as exc:                       # noqa: BLE001 - diagnostic
        print(f"  could not determine: {type(exc).__name__}: {exc}")

    print("\nWSDL reachability:")
    try:
        request = urllib.request.Request(config.endpoint + "?wsdl")
        with opener.open(request, timeout=config.timeout_s) as response:
            body = response.read()
        print(f"  HTTP {response.status}, {len(body)} bytes, "
              f"sha256={soap.sha256(body)[:16]}...")
        if b"soap:address" in body:
            for line in body.decode("utf-8", "replace").splitlines():
                if "soap:address" in line:
                    print(f"  {line.strip()[:110]}")
    except Exception as exc:                       # noqa: BLE001 - diagnostic
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = Config()
    store = Store(config.db_path)
    day = dt.datetime.now().strftime("%Y-%m-%d")
    stats = store.queue_stats()
    pending = stats.get("pending", 0)
    print(f"queue         : {stats}")
    print(f"used today    : {store.used_today(day)}/{config.daily_cap}")
    rows = store.db.execute("SELECT COUNT(*) n FROM product").fetchone()["n"]
    gtins = store.db.execute(
        "SELECT COUNT(DISTINCT gtin14) n FROM product").fetchone()["n"]
    bad = store.db.execute(
        "SELECT COUNT(*) n FROM product WHERE checksum_valid=0").fetchone()["n"]
    print(f"product rows  : {rows} ({gtins} distinct GTIN-14, {bad} bad checksum)")
    if pending:
        print(f"remaining     : ~{-(-pending // max(config.daily_cap, 1))} days "
              f"at {config.daily_cap}/day")
    import os
    if os.path.exists(config.halt_file):
        print(f"\n*** HALTED *** {config.halt_file}")
        with open(config.halt_file, encoding="utf-8") as handle:
            print(handle.read())
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Emit the crosswalk. GTINs are quoted as text so a spreadsheet cannot
    turn 05712249101367 into 5.71225E+12 (HANDOVER §11)."""
    import csv
    config = Config()
    store = Store(config.db_path)
    out = args.out or f"{config.data_dir}/ncpr_gtin_crosswalk.csv"
    rows = store.db.execute(
        "SELECT medicinal_product_identifier, gtin_raw, gtin14, ean13_derived, "
        "checksum_valid, name_bg, name_en, authorization_number, final_pack, "
        "retrieved_at, raw_sha256 FROM product ORDER BY gtin14").fetchall()
    with open(out, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, quoting=csv.QUOTE_ALL)
        writer.writerow([k for k in rows[0].keys()] if rows else
                        ["medicinal_product_identifier", "gtin_raw", "gtin14"])
        for row in rows:
            writer.writerow([row[k] for k in row.keys()])
    print(f"wrote {out} ({len(rows)} rows, all fields quoted as text)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.main")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check config, TLS, egress IP, WSDL")
    sub.add_parser("status", help="queue / counter / results summary")
    export = sub.add_parser("export", help="write the GTIN crosswalk CSV")
    export.add_argument("--out")
    sub.add_parser("collect", help="run the collector loop")
    sub.add_parser("build-queue", help="see: python -m app.queue_build --help")

    args = parser.parse_args()
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "export":
        return cmd_export(args)
    if args.command == "collect":
        from app.collect import main as collect_main
        collect_main()
        return 0
    if args.command == "build-queue":
        print("Run: python -m app.queue_build --annex /data/Prilogenie-4.xlsx "
              "[--include-reverse] [--contested contested.csv]")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
