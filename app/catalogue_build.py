# -*- coding: utf-8 -*-
"""Build the GUI's local pharmacist catalogue without calling SESPA.

Annex 4 is authoritative for active PLS package rows and supplies national ID,
registration number, INN and the complete product/package description. A local
PimChecker PostgreSQL text dump enriches ATC codes and authorization holders.

The default is strict: any missing required GUI field prevents the database
replacement. ``--check-only`` performs the same reconciliation without writing.
``--allow-incomplete`` is diagnostic and keeps issues in
``local_catalogue_issue``; it must not be used to declare the GUI ready.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from app.config import Config
from app.queue_build import ACTIVE, norm_id, read_annex
from app.store import SCHEMA


REQUIRED_FIELDS = (
    "national_id",
    "registration_number",
    "trade_name",
    "inn",
    "atc_codes",
)
COPY_RE = re.compile(r"^COPY public\.medical_products \((.+)\) FROM stdin;$")


def clean(value: str | None) -> str:
    if value is None or value == r"\N":
        return ""
    return value.strip()


def copy_unescape(value: str) -> str:
    """Decode PostgreSQL COPY text escapes, leaving unknown escapes intact."""
    if value == r"\N":
        return ""
    out: list[str] = []
    i = 0
    escapes = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
    while i < len(value):
        if value[i] != "\\" or i + 1 >= len(value):
            out.append(value[i])
            i += 1
            continue
        nxt = value[i + 1]
        if nxt in escapes:
            out.append(escapes[nxt])
        elif nxt == "\\":
            out.append("\\")
        else:
            out.extend(("\\", nxt))
        i += 2
    return "".join(out).strip()


def norm_text(value: str) -> str:
    return " ".join(value.casefold().replace("/", " ").split())


def split_atc(value: str) -> set[str]:
    return {
        code.upper()
        for code in re.findall(r"\b[A-Z]\d{2}[A-Z]{1,2}\d{0,2}\b", value.upper())
    }


@dataclass(frozen=True)
class PimProduct:
    national_id: str
    atc_codes: frozenset[str]
    registration_number: str
    name: str
    authorization_holder: str
    inn: str


def read_pim_products(path: Path) -> list[PimProduct]:
    """Read only the medical_products COPY block from a PostgreSQL dump."""
    products: list[PimProduct] = []
    columns: list[str] | None = None
    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\r\n")
            if columns is None:
                match = COPY_RE.match(line)
                if match:
                    columns = [part.strip() for part in match.group(1).split(",")]
                continue
            if line == r"\.":
                break
            values = line.split("\t")
            if len(values) != len(columns):
                continue
            row = {key: copy_unescape(value) for key, value in zip(columns, values)}
            national_id = norm_id(row.get("national_number"))
            if not national_id.isdigit() or row.get("active") != "t" or row.get("deleted_at"):
                continue
            products.append(PimProduct(
                national_id=national_id,
                atc_codes=frozenset(split_atc(row.get("atc_code", ""))),
                registration_number=clean(row.get("registration_number")),
                name=clean(row.get("name")),
                authorization_holder=clean(row.get("authorization_holder")),
                inn=clean(row.get("inn")),
            ))
    if columns is None:
        raise ValueError(f"medical_products COPY block not found in {path}")
    return products


def read_atc_references(paths: list[Path]) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Read official pre-extracted appendix CSVs keyed by national_id."""
    atc_by_national: dict[str, set[str]] = defaultdict(set)
    holder_by_national: dict[str, str] = {}
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                national_id = norm_id(row.get("national_id"))
                if not national_id.isdigit():
                    continue
                atc_by_national[national_id].update(split_atc(row.get("atc", "")))
                holder = clean(row.get("mah"))
                if holder:
                    holder_by_national.setdefault(national_id, holder)
    return atc_by_national, holder_by_national


def reconcile(annex_path: Path, pim_path: Path,
              atc_reference_paths: list[Path] | None = None
              ) -> tuple[list[dict], list[dict], dict]:
    annex_rows = [row for row in read_annex(annex_path) if row["status"] == ACTIVE]
    pim_products = read_pim_products(pim_path)
    official_atc, official_holders = read_atc_references(atc_reference_paths or [])

    by_national: dict[str, list[PimProduct]] = defaultdict(list)
    atc_by_inn: dict[str, set[str]] = defaultdict(set)
    for product in pim_products:
        by_national[product.national_id].append(product)
        if product.inn:
            atc_by_inn[norm_text(product.inn)].update(product.atc_codes)

    records: list[dict] = []
    issues: list[dict] = []
    source_counts: dict[str, int] = defaultdict(int)
    imported_at = dt.datetime.now(dt.timezone.utc).isoformat()

    for annex in annex_rows:
        national_id = annex["national_id"]
        matches = by_national.get(national_id, [])
        exact_codes = set().union(*(p.atc_codes for p in matches)) if matches else set()
        if official_atc.get(national_id):
            atc_codes = official_atc[national_id]
            atc_source = "official_appendix:national_id"
        elif exact_codes:
            atc_codes = exact_codes
            atc_source = "pim:national_id"
        else:
            inn_codes = atc_by_inn.get(norm_text(annex["inn"]), set())
            if len(inn_codes) == 1:
                atc_codes = set(inn_codes)
                atc_source = "pim:unique_inn"
            else:
                atc_codes = set()
                atc_source = "unresolved"
                reason = "no ATC candidate" if not inn_codes else "ambiguous INN-to-ATC mapping"
                issues.append({
                    "national_id": national_id,
                    "field": "atc_codes",
                    "reason": reason,
                    "candidates": "|".join(sorted(inn_codes)),
                })

        holders = sorted({p.authorization_holder for p in matches if p.authorization_holder})
        if not holders and official_holders.get(national_id):
            holders = [official_holders[national_id]]
        record = {
            "national_id": national_id,
            "registration_number": clean(annex["reg_number"]),
            # Annex description contains the trade name plus strength/form/pack.
            "trade_name": clean(annex["description"]),
            "inn": clean(annex["inn"]),
            "atc_codes": "|".join(sorted(atc_codes)),
            "authorization_holder": " | ".join(holders),
            "product_description": clean(annex["description"]),
            "atc_source": atc_source,
            "annex_snapshot": annex_path.name,
            "pim_snapshot": pim_path.name,
            "imported_at": imported_at,
        }
        source_counts[atc_source] += 1
        for field in REQUIRED_FIELDS:
            if not record[field] and not any(
                issue["national_id"] == national_id and issue["field"] == field
                for issue in issues
            ):
                issues.append({
                    "national_id": national_id,
                    "field": field,
                    "reason": "required source value missing",
                    "candidates": "",
                })
        records.append(record)

    stats = {
        "active_annex_rows": len(annex_rows),
        "catalogue_records": len(records),
        "pim_products": len(pim_products),
        "atc_reference_files": len(atc_reference_paths or []),
        "issues": len(issues),
        "atc_sources": dict(sorted(source_counts.items())),
    }
    return records, issues, stats


def replace_catalogue(db_path: Path, records: list[dict], issues: list[dict]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    try:
        db.executescript(SCHEMA)
        with db:
            db.execute("DELETE FROM local_catalogue")
            db.execute("DELETE FROM local_catalogue_issue")
            if records:
                columns = list(records[0])
                db.executemany(
                    f"INSERT INTO local_catalogue({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})",
                    ([record[column] for column in columns] for record in records),
                )
            if issues:
                db.executemany(
                    "INSERT INTO local_catalogue_issue"
                    "(national_id,field,reason,candidates) VALUES (?,?,?,?)",
                    ((i["national_id"], i["field"], i["reason"], i["candidates"])
                     for i in issues),
                )
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.catalogue_build")
    parser.add_argument("--annex", type=Path, required=True)
    parser.add_argument("--pim-sql", type=Path, required=True)
    parser.add_argument("--atc-csv", type=Path, action="append", default=[],
                        help="official appendix extract with national_id and atc; repeatable")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--issues-out", type=Path,
                        help="optional JSON report; no report file is written by default")
    args = parser.parse_args()

    records, issues, stats = reconcile(args.annex, args.pim_sql, args.atc_csv)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if issues:
        print("\nfirst unresolved fields:")
        for issue in issues[:20]:
            suffix = f" ({issue['candidates']})" if issue["candidates"] else ""
            print(f"  {issue['national_id']} {issue['field']}: "
                  f"{issue['reason']}{suffix}")
        if len(issues) > 20:
            print(f"  ... and {len(issues) - 20} more")
    if args.issues_out:
        args.issues_out.parent.mkdir(parents=True, exist_ok=True)
        args.issues_out.write_text(
            json.dumps({"stats": stats, "issues": issues}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\nwrote {args.issues_out}")

    if issues and not args.allow_incomplete:
        print("\nREFUSING TO REPLACE local_catalogue: required fields are incomplete.")
        return 2
    if args.check_only:
        print("\ncheck only: database unchanged")
        return 0 if not issues else 2

    db_path = args.db or Path(Config().db_path)
    replace_catalogue(db_path, records, issues)
    print(f"\nwrote {len(records)} rows to {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
