# -*- coding: utf-8 -*-
from pathlib import Path

from openpyxl import Workbook

from app.catalogue_build import read_pim_products, reconcile
from app.queue_build import ACTIVE


def write_annex(path: Path, rows: list[tuple]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.append(["title"])
    ws.append(["title"])
    for row in rows:
        values = [None] * 27
        values[0], values[1], values[2], values[24], values[25] = row
        ws.append(values)
    wb.save(path)


def write_pim_dump(path: Path, rows: list[list[str]]) -> None:
    columns = (
        "id, parent_id, atc_code, national_number, registration_number, "
        "name, quantity, authorization_holder, inn, dispensing_regime, "
        "active, created_at, updated_at, last_updated_at, deleted_at"
    )
    text = [f"COPY public.medical_products ({columns}) FROM stdin;"]
    text.extend("\t".join(row) for row in rows)
    text.append(r"\.")
    path.write_text("\n".join(text) + "\n", encoding="utf-8")


def pim_row(natid: str, atc: str, inn: str, active: str = "t") -> list[str]:
    return [
        "1", r"\N", atc or r"\N", natid, "REG", "Trade", "10 mg",
        "Holder", inn, r"\N", active, "2026-01-01", "2026-01-01", r"\N", r"\N",
    ]


def test_reads_medical_products_copy_block(tmp_path):
    dump = tmp_path / "pim.sql"
    write_pim_dump(dump, [pim_row("15955", "A10AE56", "Insulin degludec")])
    products = read_pim_products(dump)
    assert len(products) == 1
    assert products[0].national_id == "15955"
    assert products[0].atc_codes == frozenset({"A10AE56"})


def test_reconcile_prefers_exact_national_id(tmp_path):
    annex = tmp_path / "annex.xlsx"
    dump = tmp_path / "pim.sql"
    write_annex(annex, [("INN", "20001234", "Trade, 10 mg, Pack: 3", ACTIVE, 15955)])
    write_pim_dump(dump, [pim_row("15955", "A10AE56", "INN")])
    records, issues, stats = reconcile(annex, dump)
    assert not issues
    assert records[0]["national_id"] == "15955"
    assert records[0]["atc_codes"] == "A10AE56"
    assert records[0]["atc_source"] == "pim:national_id"
    assert stats["catalogue_records"] == 1


def test_unique_inn_can_fill_missing_exact_atc(tmp_path):
    annex = tmp_path / "annex.xlsx"
    dump = tmp_path / "pim.sql"
    write_annex(annex, [("Aciclovir", "20001234", "Trade, 10 mg", ACTIVE, 100)])
    write_pim_dump(dump, [
        pim_row("100", "", "Aciclovir"),
        pim_row("200", "J05AB01", "Aciclovir"),
    ])
    records, issues, _ = reconcile(annex, dump)
    assert not issues
    assert records[0]["atc_codes"] == "J05AB01"
    assert records[0]["atc_source"] == "pim:unique_inn"


def test_ambiguous_inn_is_reported_not_guessed(tmp_path):
    annex = tmp_path / "annex.xlsx"
    dump = tmp_path / "pim.sql"
    write_annex(annex, [("Combination", "20001234", "Trade", ACTIVE, 100)])
    write_pim_dump(dump, [
        pim_row("200", "A01AA01", "Combination"),
        pim_row("201", "B02BB02", "Combination"),
    ])
    records, issues, _ = reconcile(annex, dump)
    assert records[0]["atc_codes"] == ""
    assert issues == [{
        "national_id": "100",
        "field": "atc_codes",
        "reason": "ambiguous INN-to-ATC mapping",
        "candidates": "A01AA01|B02BB02",
    }]


def test_official_atc_csv_has_priority(tmp_path):
    annex = tmp_path / "annex.xlsx"
    dump = tmp_path / "pim.sql"
    official = tmp_path / "appendix1_active.csv"
    write_annex(annex, [("Pantoprazole", "20001234", "Trade", ACTIVE, 100)])
    write_pim_dump(dump, [pim_row("100", "A02BC", "Pantoprazole")])
    official.write_text(
        "national_id,atc,inn,trade_name,mah\n"
        "100,A02BC02,Pantoprazole,Trade,Holder\n",
        encoding="utf-8-sig",
    )
    records, issues, _ = reconcile(annex, dump, [official])
    assert not issues
    assert records[0]["atc_codes"] == "A02BC02"
    assert records[0]["atc_source"] == "official_appendix:national_id"
