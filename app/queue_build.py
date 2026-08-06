# -*- coding: utf-8 -*-
"""Build the work queue from Annex 4, ordered by decision value.

At ~80 calls/day a full sweep of the active PLS takes roughly a month, so
the ORDER of the queue decides when useful answers arrive, not whether they
do. Sequential-by-id would deliver the most decisive rows last.

Priority bands (lower runs first):

  10  reverse lookups for salvia's RECONSTRUCTED GTINs
      These are derived, not published: salvia stored `0` + the first 12
      digits of the real GTIN and the check digit was rebuilt. The method
      has aggregate support (137/279 corroborated, 0/279 under a deliberately
      wrong check digit) but no per-row proof. An authoritative reverse
      lookup settles each one, and it is the only thing that can.

  20  forward lookups whose reg_number maps to exactly ONE drug_ref row
      No pack ambiguity by construction, so each answer is an immediately
      usable GTIN -> pfid link with no selection logic.

  30  forward lookups for reg_numbers already contested by our own matchers
      (supplied via --contested); an authoritative answer resolves a review
      row rather than adding a new one.

  40  everything else, active PLS rows.

Rows whose status is not 'Активен' are not queued at all: the runbook (§8)
notes the workbook carries historical rows, and only active ones should
drive the initial enrichment.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

import openpyxl

from app.config import Config
from app.store import Store

COL_INN, COL_REG, COL_DESC, COL_STATUS, COL_NATID = 0, 1, 2, 24, 25
ACTIVE = "Активен"


def norm_id(value) -> str:
    """15955.0 -> '15955'. openpyxl hands back floats for numeric cells and
    the service expects the text form (runbook §8)."""
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def read_annex(path: Path) -> list[dict]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    # The workbook declares its used range as A1 even though data runs to
    # AA32457; without these two calls openpyxl yields almost nothing.
    sheet.reset_dimensions()
    sheet.calculate_dimension(force=True)

    rows = []
    for row in sheet.iter_rows(values_only=True):
        if len(row) <= COL_NATID:
            continue
        national_id = norm_id(row[COL_NATID])
        reg_number = norm_id(row[COL_REG])
        if not national_id.isdigit():
            continue
        status = str(row[COL_STATUS]).strip() if row[COL_STATUS] else ""
        rows.append({
            "national_id": national_id,
            "reg_number": reg_number,
            "inn": str(row[COL_INN] or "").strip(),
            "description": str(row[COL_DESC] or "").strip(),
            "status": status,
        })
    return rows


def drug_ref_group_sizes(path: Path) -> dict[str, int]:
    sizes: dict[str, int] = defaultdict(int)
    if not path.exists():
        return sizes
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for record in csv.DictReader(handle):
            reg = (record.get("reg_number") or "").strip()
            if reg:
                sizes[reg] += 1
    return sizes


def load_reconstructed(path: Path) -> list[tuple[str, str]]:
    """(gtin, name) for salvia rows whose barcode was reconstructed."""
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for record in csv.DictReader(handle):
            if record.get("gtin_origin") == "reconstructed" and record.get("gtin_final"):
                out.append((record["gtin_final"], record.get("name_bg", "")[:60]))
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--annex", required=True, type=Path)
    # Defaults point at the archive volume, not at the authoring machine.
    # These were hardcoded Windows paths, which on the Linux host resolved to
    # nothing and silently produced a queue with every task in the lowest
    # band -- a plausible-looking 3,135 rows that would have run in exactly
    # the wrong order for 40 days. Missing reference data now warns loudly.
    parser.add_argument("--drug-ref", type=Path,
                        default=Path(os.environ.get(
                            "NCPR_DRUG_REF", "/archive/input/drug_ref_min.csv")))
    parser.add_argument("--salvia", type=Path,
                        default=Path(os.environ.get(
                            "NCPR_SALVIA", "/archive/input/salvia_products.csv")))
    parser.add_argument("--contested", type=Path,
                        help="optional CSV with a reg_number column")
    parser.add_argument("--include-reverse", action="store_true",
                        help="queue reverse lookups for reconstructed GTINs")
    parser.add_argument("--allow-missing-references", action="store_true",
                        help="build an unordered queue even without drug_ref "
                             "and salvia (not recommended)")
    args = parser.parse_args()

    config = Config()
    config.ensure_dirs()
    store = Store(config.db_path)

    # Priority ordering is the whole design of this queue: without the
    # reference files every task collapses into the lowest band and the
    # decisive answers arrive last. Refuse to build a silently-degraded
    # queue -- 40 days is too long to spend running in the wrong order.
    problems = []
    if not args.drug_ref.exists():
        problems.append(
            f"drug_ref not found at {args.drug_ref}\n"
            f"    Without it no reg_number group sizes are known, so band 20\n"
            f"    (reg_number -> exactly one pfid) cannot be identified and\n"
            f"    every task falls to band 40.")
    if args.include_reverse and not args.salvia.exists():
        problems.append(
            f"--include-reverse given but salvia products not found at {args.salvia}\n"
            f"    Band 10 validates reconstructed GTINs; it would be empty.")
    if problems:
        print("REFUSING TO BUILD A MIS-ORDERED QUEUE\n")
        for problem in problems:
            print(f"  * {problem}\n")
        print("  Copy the reference files onto the archive volume, or pass\n"
              "  --drug-ref / --salvia explicitly. To build an unordered queue\n"
              "  deliberately, pass --allow-missing-references.")
        if not args.allow_missing_references:
            raise SystemExit(2)
        print("  --allow-missing-references given; continuing unordered.\n")

    annex = read_annex(args.annex)
    active = [r for r in annex if r["status"] == ACTIVE]
    sizes = drug_ref_group_sizes(args.drug_ref)

    contested: set[str] = set()
    if args.contested and args.contested.exists():
        with args.contested.open(encoding="utf-8-sig", newline="") as handle:
            for record in csv.DictReader(handle):
                reg = (record.get("reg_number") or "").strip()
                if reg:
                    contested.add(reg)

    added = defaultdict(int)

    if args.include_reverse:
        for gtin, name in load_reconstructed(args.salvia):
            if store.add_task(f"rev:{gtin}", "reverse", gtin, 10,
                              f"validate reconstructed GTIN ({name})"):
                added["10 reverse/reconstructed"] += 1

    for row in active:
        reg = row["reg_number"]
        size = sizes.get(reg, 0)
        if size == 1:
            priority, why = 20, "reg_number maps to exactly one drug_ref row"
        elif reg in contested:
            priority, why = 30, "reg_number contested by local matchers"
        else:
            priority, why = 40, f"active PLS row (reg group size {size})"
        if store.add_task(f"fwd:{row['national_id']}", "forward",
                          row["national_id"], priority, why):
            added[f"{priority} forward"] += 1

    print(f"Annex 4 rows read       : {len(annex)}")
    print(f"  active                : {len(active)}")
    print(f"  other statuses        : {len(annex) - len(active)}")
    print(f"drug_ref reg groups     : {len(sizes)}")
    print("\nqueued:")
    for band, count in sorted(added.items()):
        print(f"  {band:34} {count}")
    print(f"\nqueue now: {store.queue_stats()}")
    if not any(band.startswith("20") for band in added):
        print("\n  WARNING: band 20 is empty. Every task will run in the "
              "lowest priority\n  band, so the most decisive answers arrive "
              "last. Check --drug-ref.")

    per_day = config.daily_cap
    pending = store.queue_stats().get("pending", 0)
    print(f"\nAt {per_day}/day that is ~{-(-pending // max(per_day, 1))} days "
          f"of collection.")


if __name__ == "__main__":
    main()
