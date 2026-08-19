import sqlite3

from app.store import Store


def test_old_product_table_gets_gtin_evidence_columns(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute("""
        CREATE TABLE product (
            task_id TEXT NOT NULL,
            medicinal_product_identifier TEXT,
            gtin_raw TEXT,
            gtin14 TEXT,
            ean13_derived TEXT,
            checksum_valid INTEGER,
            name_bg TEXT,
            name_en TEXT,
            authorization_number TEXT,
            final_pack TEXT,
            reg_number TEXT,
            retrieved_at TEXT NOT NULL,
            raw_path TEXT,
            raw_sha256 TEXT
        )
    """)
    db.commit()
    db.close()

    migrated = Store(str(path))
    columns = {
        row["name"] for row in migrated.db.execute("PRAGMA table_info(product)")
    }

    assert "expected_check_digit" in columns
    assert "indicator_digit" in columns
