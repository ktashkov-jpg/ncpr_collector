# -*- coding: utf-8 -*-
"""Page through listMedicinalProducts to build the catalogue.

Why this exists, given the collector already works from the Annex 4
workbook:

  * It is live, not a snapshot dated 02-07-2026.
  * It covers ALL six RegisterCode values. The workbook is PDL_APPENDIX_4
    alone, while the GTIN scope is the whole Positive Drug List -- so the
    workbook may simply not contain some products that have GTINs.
  * It returns inn, atcCodes, medicamentForm, quantity, medicamentUnit and
    finalPack for every product. That is the authoritative pack-selection
    metadata the local matchers have been deriving heuristically, and it
    arrives in a few paged calls rather than one per product.

It does NOT return GTINs -- medicinalProductListItem has no gtins field, and
the spec says to call getMedicinalProductData for full data. So this does not
replace the per-product collection; it makes it correctly scoped and
correctly ordered.

Counts against the same daily cap as everything else: these are real
requests to the same service.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

from app import soap
from app.config import Config
from app.store import Store

FIELD_MAP = {
    "medicinalProductIdentifier": "medicinal_product_identifier",
    "registerMedicamentId": "register_medicament_id",
    "registerCode": "register_code",
    "registerName": "register_name",
    "nameBG": "name_bg",
    "nameEN": "name_en",
    "inn": "inn",
    "atcCodes": "atc_codes",
    "authorizationHolder": "authorization_holder",
    "producer": "producer",
    "medicamentForm": "medicament_form",
    "quantity": "quantity",
    "medicamentUnit": "medicament_unit",
    "finalPack": "final_pack",
    "publishedAt": "published_at",
}


def to_row(item: dict, register_code: str, now: str) -> dict:
    row = {value: "" for value in FIELD_MAP.values()}
    for key, value in item.items():
        if key in FIELD_MAP:
            row[FIELD_MAP[key]] = value
    if not row["register_code"]:
        row["register_code"] = register_code or "ALL"
    row["retrieved_at"] = now
    return row


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.enumerate_catalogue")
    parser.add_argument("--register", action="append",
                        help="RegisterCode to enumerate; repeatable. "
                             "Default: all six.")
    parser.add_argument("--page-size", type=int, default=200)
    parser.add_argument("--delay", type=float, default=None,
                        help="seconds between pages; defaults to the "
                             "configured collection delay")
    parser.add_argument("--count-only", action="store_true",
                        help="one call per register: report allResultsCount "
                             "and stop. Six requests total.")
    args = parser.parse_args()

    config = Config()
    config.validate()
    if Path(config.halt_file).exists():
        raise SystemExit(f"{config.halt_file} present - resolve before running.")

    config.ensure_dirs()
    store = Store(config.db_path)
    opener = soap.make_opener(config.insecure_tls)
    registers = args.register or list(soap.REGISTER_CODES)
    delay = args.delay if args.delay is not None else config.delay_min_s
    day = dt.datetime.now().strftime("%Y-%m-%d")
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    print(f"registers: {', '.join(registers)}")
    print(f"page size: {args.page_size} | delay {delay}s | "
          f"cap {store.used_today(day)}/{config.daily_cap} used today\n")

    totals: dict[str, int] = {}
    for register in registers:
        from_row, saved, expected = 0, 0, None
        while True:
            if store.used_today(day) >= config.daily_cap:
                print("  daily cap reached - stopping. Re-run tomorrow; "
                      "already-saved pages are kept.")
                return 0
            store.consume(day)
            try:
                payload = soap.build_list_envelope(
                    config.namespace, register, from_row, args.page_size)
                import urllib.request
                request = urllib.request.Request(
                    config.endpoint, data=payload, method="POST")
                request.add_header("Content-Type", "text/xml; charset=UTF-8")
                request.add_header("SOAPAction", '""')
                started = time.time()
                with opener.open(request, timeout=config.timeout_s) as response:
                    body = response.read()
                elapsed = int((time.time() - started) * 1000)
            except Exception as exc:                # noqa: BLE001
                print(f"  {register}: request failed: {type(exc).__name__}: {exc}")
                break

            raw = Path(config.raw_dir) / (
                f"list_{register}_{from_row}_"
                f"{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}.xml")
            raw.write_bytes(body)                   # archive before parsing

            parsed = soap.parse_list(body)
            store.log(task_id=f"list:{register}:{from_row}", http_status=200,
                      soap_fault=parsed.get("fault"), elapsed_ms=elapsed,
                      bytes=len(body))
            if parsed.get("fault"):
                print(f"  {register}: SOAP fault: {parsed['fault'][:70]}")
                break

            if expected is None:
                expected = parsed.get("all_results_count")
                totals[register] = expected or 0
                print(f"  {register:18} allResultsCount = {expected}")
                if args.count_only:
                    break

            items = parsed["items"]
            if not items:
                break
            saved += store.save_catalogue(
                [to_row(i, register, now) for i in items])
            from_row += len(items)
            print(f"     rows {from_row}/{expected}  saved={saved}", flush=True)
            if expected is not None and from_row >= expected:
                break
            time.sleep(delay)

    print("\ncatalogue by register:")
    for register_code, rows, ids in store.catalogue_stats():
        print(f"  {register_code:18} rows={rows:6} distinct ids={ids}")
    if args.count_only:
        total = sum(totals.values())
        print(f"\nallResultsCount total across registers: {total}")
        print("Note: a product in several registers is counted once per "
              "register, so this is an upper bound on distinct products.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
