import sqlite3
from types import SimpleNamespace

import pytest

from app.collect import acquire_lock, release_lock
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
    assert b"Start bulk scrape" in page.data
    assert b"Stop bulk scrape" in page.data
    assert web.get("/api/options/national_id?q=75").json == ["758"]
    result = web.get("/api/catalogue?national_id=758").json
    assert result[0]["trade_name"] == "Example 500 mg"
    assert result[0]["atc_codes"] == "A10BA02"
    assert web.get("/api/catalogue?national_id=75").json[0]["national_id"] == "758"
    assert web.get("/api/catalogue?registration_number=123").json[0]["national_id"] == "758"


def test_api_errors_are_json(client):
    web, _ = client
    response = web.get("/api/not-a-route")
    assert response.status_code == 404
    assert response.is_json
    assert response.json["error"]


def test_csrf_token_can_be_supplied_in_json_body(client):
    web, _ = client
    response = web.post("/api/export", json={"csrf_token": csrf(web)})
    assert response.status_code == 200
    assert response.is_json


def test_unexpected_api_errors_do_not_return_html(client):
    web, _ = client

    def fail():
        raise RuntimeError("database detail must stay in the server log")

    web.application.view_functions["api_status"] = fail
    response = web.get("/api/status")
    assert response.status_code == 500
    assert response.is_json
    assert "could not complete" in response.json["error"]
    assert "database detail" not in response.get_data(as_text=True)


def test_review_api_is_bounded_and_prioritizes_pending(client):
    web, path = client
    db = sqlite3.connect(path)
    for index in range(25):
        product_rowid = db.execute(
            """INSERT INTO product(task_id, medicinal_product_identifier,
               gtin_raw, gtin14, checksum_valid, retrieved_at)
               VALUES(?,?,?,?,?,?)""",
            (f"probe:{index}", "758", f"{index:014d}", f"{index:014d}", 1, "now"),
        ).lastrowid
        db.execute(
            """INSERT INTO review_item(product_rowid, national_id, status,
               created_at) VALUES(?,?,?,?)""",
            (product_rowid, "758", "accepted", "now"),
        )
    pending_product = db.execute(
        """INSERT INTO product(task_id, medicinal_product_identifier,
           gtin_raw, gtin14, checksum_valid, retrieved_at)
           VALUES(?,?,?,?,?,?)""",
        ("pending", "758", "99999999999999", "99999999999999", 0, "now"),
    ).lastrowid
    pending_review = db.execute(
        """INSERT INTO review_item(product_rowid, national_id, status,
           created_at) VALUES(?,?,?,?)""",
        (pending_product, "758", "pending", "now"),
    ).lastrowid
    db.commit()

    rows = web.get("/api/reviews").json
    assert len(rows) == 20
    assert rows[0]["review_id"] == pending_review
    assert rows[0]["status"] == "pending"


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
    lock_handles = []

    def launch():
        lock_handles.append(acquire_lock(runtime))
        return SimpleNamespace(pid=4321)

    web.application.config.update(
        BULK_APPROVED=True,
        NCPR_CONFIG=runtime,
        BULK_LAUNCHER=launch,
    )
    try:
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
        status = db.execute(
            "SELECT status FROM queue WHERE task_id='fwd:758'"
        ).fetchone()[0]
        assert status == "pending"
        state = db.execute(
            "SELECT state FROM bulk_run WHERE singleton=1"
        ).fetchone()[0]
        assert state == "stopping"
    finally:
        if lock_handles:
            release_lock(lock_handles.pop())


def test_status_reconciles_stale_bulk_state(client, tmp_path):
    web, path = client
    runtime = SimpleNamespace(
        daily_cap=80,
        halt_file=str(tmp_path / "HALTED"),
        lock_file=str(tmp_path / "collector.lock"),
        stop_file=str(tmp_path / "STOP_REQUESTED"),
    )
    web.application.config["NCPR_CONFIG"] = runtime
    db = sqlite3.connect(path)
    db.execute(
        "UPDATE bulk_run SET state='running', pid=20, "
        "requested_at='2026-08-21T12:58:35+00:00', "
        "started_at='2026-08-21T12:58:35+00:00' WHERE singleton=1"
    )
    db.commit()
    (tmp_path / "collector.lock").write_text("20", encoding="utf-8")
    (tmp_path / "STOP_REQUESTED").write_text("stop", encoding="utf-8")

    status = web.get("/api/status").json

    assert status["collector_running"] is False
    assert status["bulk"]["state"] == "failed"
    assert status["bulk"]["pid"] is None
    assert not (tmp_path / "STOP_REQUESTED").exists()
