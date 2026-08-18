import sqlite3
from types import SimpleNamespace

import pytest

from app.store import Store
from app.web import create_app


@pytest.fixture()
def client(tmp_path):
    path = tmp_path / "test.sqlite3"
    store = Store(str(path))
    store.db.execute(
        """INSERT INTO local_catalogue(
          national_id, registration_number, trade_name, inn, atc_codes,
          authorization_holder, product_description, atc_source,
          annex_snapshot, pim_snapshot, imported_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        ("758", "II-1234", "Example 500 mg", "metformin", "A10BA02",
         "Example MAH", "tablets x 30", "test", "annex", "pim", "now"),
    )
    store.db.commit()
    app = create_app({"TESTING": True, "DB_PATH": str(path), "SECRET_KEY": "test"})
    return app.test_client(), path


def csrf(client):
    client.get("/")
    with client.session_transaction() as session:
        return session["csrf_token"]


def test_index_and_local_catalogue_search(client):
    web, _ = client
    page = web.get("/")
    assert page.status_code == 200
    assert b"Identify a medicinal product" in page.data
    assert web.get("/api/options/national_id?q=75").json == ["758"]
    result = web.get("/api/catalogue?national_id=758").json
    assert result[0]["trade_name"] == "Example 500 mg"
    assert result[0]["atc_codes"] == "A10BA02"
    assert web.get("/api/catalogue?national_id=75").json[0]["national_id"] == "758"
    assert web.get("/api/catalogue?registration_number=123").json[0]["national_id"] == "758"


def test_mutations_require_csrf_and_bulk_stays_locked(client):
    web, _ = client
    assert web.post("/api/export").status_code == 403
    response = web.post(
        "/api/collector/launch", headers={"X-CSRF-Token": csrf(web)}
    )
    assert response.status_code == 403
    assert "locked" in response.json["error"]


def test_existing_product_can_be_reviewed_without_soap(client):
    web, path = client
    db = sqlite3.connect(path)
    db.execute(
        """INSERT INTO product(
          task_id, medicinal_product_identifier, gtin_raw, gtin14,
          checksum_valid, name_bg, authorization_number, final_pack,
          reg_number, retrieved_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        ("probe", "758", "05001234567890", "05001234567890", 1,
         "Example", "AUTH-1", "tablets x 30", "II-1234", "now"),
    )
    db.commit()
    response = web.post(
        "/api/lookup", json={"national_id": "758"},
        headers={"X-CSRF-Token": csrf(web)},
    )
    assert response.status_code == 200
    assert response.json["reused"] is True
    assert response.json["reviews"][0]["gtin_raw"] == "05001234567890"
    audit = db.execute(
        "SELECT outcome, product_id, local_modified FROM audit_event ORDER BY event_id DESC LIMIT 1"
    ).fetchone()
    assert audit == ("reused", "758", 1)


def test_review_decision_and_export(client, monkeypatch, tmp_path):
    web, path = client
    db = sqlite3.connect(path)
    db.execute(
        """INSERT INTO product(task_id, medicinal_product_identifier, gtin_raw,
        gtin14, checksum_valid, retrieved_at) VALUES(?,?,?,?,?,?)""",
        ("probe", "758", "05001234567890", "05001234567890", 1, "now"),
    )
    db.commit()
    token = csrf(web)
    web.post("/api/lookup", json={"national_id": "758"}, headers={"X-CSRF-Token": token})
    review_id = web.get("/api/reviews").json[0]["review_id"]
    accepted = web.post(
        f"/api/reviews/{review_id}", json={"status": "accepted"},
        headers={"X-CSRF-Token": token},
    )
    assert accepted.status_code == 200


def test_download_all_collected_as_csv(client):
    web, path = client
    db = sqlite3.connect(path)
    db.execute(
        """INSERT INTO product(task_id, medicinal_product_identifier, gtin_raw,
        gtin14, checksum_valid, retrieved_at) VALUES(?,?,?,?,?,?)""",
        ("fwd:758", "758", "05001234567890", "05001234567890", 1, "now"),
    )
    db.commit()
    response = web.post(
        "/api/export/all", headers={"X-CSRF-Token": csrf(web)})
    assert response.status_code == 200
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert b"05001234567890" in response.data
    assert b"II-1234" in response.data


def test_bulk_start_and_stop_preserve_sqlite_queue(client, tmp_path):
    web, path = client
    db = sqlite3.connect(path)
    db.execute(
        "INSERT INTO queue(task_id,kind,key,priority,reason) VALUES(?,?,?,?,?)",
        ("fwd:758", "forward", "758", 10, "test"),
    )
    db.commit()
    runtime = SimpleNamespace(
        daily_cap=80,
        halt_file=str(tmp_path / "HALTED"),
        lock_file=str(tmp_path / "collector.lock"),
        stop_file=str(tmp_path / "STOP_REQUESTED"),
    )
    web.application.config.update(
        BULK_APPROVED=True,
        NCPR_CONFIG=runtime,
        BULK_LAUNCHER=lambda: SimpleNamespace(pid=4321),
    )
    token = csrf(web)
    started = web.post(
        "/api/collector/launch", json={"confirmed": True, "pending": 1},
        headers={"X-CSRF-Token": token},
    )
    assert started.status_code == 202
    stopped = web.post(
        "/api/collector/stop", headers={"X-CSRF-Token": token})
    assert stopped.status_code == 202
    assert (tmp_path / "STOP_REQUESTED").exists()
    assert db.execute("SELECT status FROM queue WHERE task_id='fwd:758'").fetchone()[0] == "pending"
    assert db.execute("SELECT state FROM bulk_run WHERE singleton=1").fetchone()[0] == "stopping"
