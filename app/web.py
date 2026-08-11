# -*- coding: utf-8 -*-
"""Private pharmacist UI: local discovery, explicit SOAP lookup, review.

The web process never starts the collector implicitly. Every network lookup is
an explicit POST for one exact catalogue identifier and shares the collector's
counter, lock, raw archive, request log and hard-stop marker.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import secrets
import sqlite3
import tempfile
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, session

from app import soap
from app.collect import acquire_lock, cache_wsdl, halt, in_window, release_lock, today
from app.config import Config
from app.store import Store

OPTION_FIELDS = {
    "national_id": "national_id",
    "registration_number": "registration_number",
    "trade_name": "trade_name",
    "atc": "atc_codes",
    "inn": "inn",
}


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("NCPR_WEB_SECRET") or secrets.token_hex(32),
        DB_PATH=Config().db_path,
        TESTING=False,
    )
    if test_config:
        app.config.update(test_config)
    # Apply additive SQLite migrations before the first request. This matters
    # for catalogues built before the review workflow existed.
    Store(app.config["DB_PATH"]).db.close()

    @app.get("/")
    def index():
        token = session.setdefault("csrf_token", secrets.token_urlsafe(24))
        return render_template("index.html", csrf_token=token, status=_status(app))

    @app.get("/api/status")
    def api_status():
        return jsonify(_status(app))

    @app.get("/api/options/<field>")
    def options(field: str):
        column = OPTION_FIELDS.get(field)
        if not column:
            abort(404)
        query = request.args.get("q", "").strip()
        limit = min(max(request.args.get("limit", 12, type=int), 1), 50)
        db = _db(app)
        sql = f"SELECT DISTINCT {column} value FROM local_catalogue WHERE {column} <> ''"
        args: list[object] = []
        if query:
            sql += f" AND {column} LIKE ? ESCAPE '\\'"
            args.append(f"%{_like(query)}%")
        if field == "national_id":
            sql += f" ORDER BY CAST({column} AS INTEGER), {column} LIMIT ?"
        else:
            sql += f" ORDER BY {column} COLLATE NOCASE LIMIT ?"
        args.append(limit)
        return jsonify([row["value"] for row in db.execute(sql, args)])

    @app.get("/api/catalogue")
    def catalogue():
        clauses, args = [], []
        for key, column in OPTION_FIELDS.items():
            value = request.args.get(key, "").strip()
            if value:
                clauses.append(f"{column} = ?")
                args.append(value)
        sql = "SELECT * FROM local_catalogue"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY CAST(national_id AS INTEGER), national_id LIMIT 50"
        rows = _db(app).execute(sql, args).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.get("/api/catalogue/<national_id>")
    def catalogue_item(national_id: str):
        row = _db(app).execute(
            "SELECT * FROM local_catalogue WHERE national_id=?", (national_id,)
        ).fetchone()
        if not row:
            abort(404)
        return jsonify(dict(row))

    @app.get("/api/reviews")
    def reviews():
        return jsonify(_reviews(_db(app)))

    @app.post("/api/lookup")
    def lookup():
        _require_csrf()
        payload = request.get_json(silent=True) or {}
        national_id = str(payload.get("national_id", "")).strip()
        if not national_id.isdigit():
            return jsonify(error="Choose a valid National ID from the catalogue."), 400
        try:
            result, reused = _lookup_one(app, national_id)
        except LookupError as exc:
            return jsonify(error=str(exc)), 409
        return jsonify(result=result, reused=reused, reviews=_reviews(_db(app)))

    @app.post("/api/reviews/<int:review_id>")
    def decide(review_id: int):
        _require_csrf()
        status = (request.get_json(silent=True) or {}).get("status")
        if status not in ("accepted", "rejected"):
            return jsonify(error="Decision must be accepted or rejected."), 400
        db = _db(app)
        cur = db.execute(
            "UPDATE review_item SET status=?, decided_at=? WHERE review_id=?",
            (status, _now(), review_id),
        )
        if not cur.rowcount:
            abort(404)
        db.commit()
        exported = False
        if status == "accepted" and _accepted_unexported(db) >= 10:
            exported = bool(_export_confirmed(app, db))
        return jsonify(reviews=_reviews(db), exported=exported)

    @app.post("/api/export")
    def export_now():
        _require_csrf()
        count = _export_confirmed(app, _db(app))
        return jsonify(exported=count, status=_status(app))

    @app.post("/api/collector/launch")
    def launch_collector():
        _require_csrf()
        # The control is intentionally present before approval, but this
        # process does not gain launch authority merely because it has a UI.
        if not Config().bulk_approved:
            return jsonify(error="Bulk launch is locked until written approval is recorded."), 403
        return jsonify(error="Bulk launch requires an operator-run deployment command."), 501

    return app


def _db(app: Flask) -> sqlite3.Connection:
    db = sqlite3.connect(app.config["DB_PATH"], timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _status(app: Flask) -> dict:
    config = Config()
    db = _db(app)
    day = dt.datetime.now().strftime("%Y-%m-%d")
    scalar = lambda sql, args=(): db.execute(sql, args).fetchone()[0]
    return {
        "catalogue_rows": scalar("SELECT COUNT(*) FROM local_catalogue"),
        "requests_used": scalar("SELECT COALESCE((SELECT used FROM daily_counter WHERE day=?),0)", (day,)),
        "daily_cap": config.daily_cap,
        "pending_reviews": scalar("SELECT COUNT(*) FROM review_item WHERE status='pending'"),
        "staged": _accepted_unexported(db),
        "halted": Path(config.halt_file).exists(),
        "collector_running": Path(config.lock_file).exists(),
        "bulk_approved": config.bulk_approved,
    }


def _lookup_one(app: Flask, national_id: str) -> tuple[dict, bool]:
    config = Config()
    config.ensure_dirs()
    store = Store(app.config["DB_PATH"])
    catalogue = store.db.execute(
        "SELECT * FROM local_catalogue WHERE national_id=?", (national_id,)
    ).fetchone()
    if not catalogue:
        raise LookupError("That National ID is not present in the local catalogue.")
    existing = store.db.execute(
        "SELECT rowid, * FROM product WHERE medicinal_product_identifier=? ORDER BY rowid DESC LIMIT 1",
        (national_id,),
    ).fetchone()
    if existing:
        _stage(store.db, existing["rowid"], national_id)
        return dict(existing), True
    if Path(config.halt_file).exists():
        raise LookupError("Requests are halted. Resolve the recorded hard stop before retrying.")
    if store.used_today(today(config)) >= config.daily_cap:
        raise LookupError("Today’s request cap has been reached.")
    if not in_window(config):
        raise LookupError(
            f"SOAP requests are allowed from {config.window_start_hour:02d}:00 to "
            f"{config.window_end_hour:02d}:00 local time."
        )
    if Path(config.lock_file).exists():
        raise LookupError("The collector is already using the SOAP connection.")

    acquire_lock(config)
    try:
        opener = soap.make_opener(config.insecure_tls)
        wsdl_hash = cache_wsdl(config, store, opener)
        used = store.consume(today(config))
        task_id = f"ui:{national_id}:{used}"
        try:
            status, body, elapsed = soap.call(
                opener, config.endpoint, config.namespace, soap.FORWARD,
                national_id, config.timeout_s,
            )
        except soap.HardStop as exc:
            halt(config, store, str(exc))
            raise LookupError(str(exc)) from exc
        except soap.Transient as exc:
            store.log(task_id=task_id, note=f"transient: {exc}")
            raise LookupError(f"Temporary service failure: {exc}") from exc

        stamp = dt.datetime.now(dt.timezone.utc)
        raw_path = Path(config.raw_dir) / f"{task_id.replace(':', '_')}_{stamp:%Y%m%dT%H%M%SZ}.xml"
        raw_path.write_bytes(body)
        parsed = soap.parse(body)
        store.log(task_id=task_id, http_status=status, soap_fault=parsed.get("fault"),
                  elapsed_ms=elapsed, bytes=len(body), wsdl_sha256=wsdl_hash)
        if parsed.get("fault"):
            raise LookupError(f"SESPA returned a SOAP fault: {parsed['fault']}")
        if not parsed.get("gtins"):
            raise LookupError("SESPA returned this product without a GTIN.")

        first_rowid = None
        for gtin in parsed["gtins"]:
            store.save_product(
                task_id=task_id,
                medicinal_product_identifier=parsed.get("medicinal_product_identifier") or national_id,
                gtin_raw=gtin, gtin14=soap.gtin14(gtin),
                ean13_derived=soap.ean13_from(gtin),
                checksum_valid=1 if soap.valid_gtin(gtin) else 0,
                expected_check_digit=soap.expected_check_digit(gtin),
                indicator_digit=soap.indicator_digit(soap.gtin14(gtin)),
                name_bg=parsed.get("name_bg") or catalogue["trade_name"],
                name_en=parsed.get("name_en"),
                authorization_number=parsed.get("authorization_number"),
                final_pack=parsed.get("final_pack") or catalogue["product_description"],
                reg_number=catalogue["registration_number"],
                retrieved_at=stamp.isoformat(), raw_path=str(raw_path),
                raw_sha256=soap.sha256(body),
            )
            rowid = store.db.execute("SELECT last_insert_rowid()").fetchone()[0]
            first_rowid = first_rowid or rowid
            _stage(store.db, rowid, national_id)
        row = store.db.execute("SELECT rowid, * FROM product WHERE rowid=?", (first_rowid,)).fetchone()
        return dict(row), False
    finally:
        release_lock(config)


def _stage(db: sqlite3.Connection, product_rowid: int, national_id: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO review_item(product_rowid,national_id,created_at) VALUES(?,?,?)",
        (product_rowid, national_id, _now()),
    )
    db.commit()


def _reviews(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("""
        SELECT r.review_id, r.status, r.exported_at, r.national_id,
               c.registration_number, c.trade_name, c.inn, c.atc_codes,
               p.authorization_number, p.final_pack, p.gtin_raw, p.gtin14,
               p.checksum_valid, p.retrieved_at
        FROM review_item r
        JOIN product p ON p.rowid=r.product_rowid
        LEFT JOIN local_catalogue c ON c.national_id=r.national_id
        ORDER BY r.review_id DESC LIMIT 100
    """).fetchall()
    return [dict(row) for row in rows]


def _accepted_unexported(db: sqlite3.Connection) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM review_item WHERE status='accepted' AND exported_at IS NULL"
    ).fetchone()[0]


def _export_confirmed(app: Flask, db: sqlite3.Connection) -> int:
    config = Config()
    config.ensure_dirs()
    rows = db.execute("""
        SELECT r.review_id, r.national_id, c.registration_number, c.trade_name,
               c.inn, c.atc_codes, p.authorization_number, p.final_pack,
               p.gtin_raw, p.gtin14, p.checksum_valid, p.retrieved_at
        FROM review_item r JOIN product p ON p.rowid=r.product_rowid
        LEFT JOIN local_catalogue c ON c.national_id=r.national_id
        WHERE r.status='accepted' ORDER BY r.review_id
    """).fetchall()
    pending_ids = [row["review_id"] for row in rows if not db.execute(
        "SELECT exported_at FROM review_item WHERE review_id=?", (row["review_id"],)
    ).fetchone()[0]]
    if not pending_ids:
        return 0
    target = Path(config.confirmed_export_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, quoting=csv.QUOTE_ALL)
            writer.writerow(rows[0].keys())
            writer.writerows(tuple(row) for row in rows)
        os.replace(temp_name, target)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise
    now = _now()
    db.executemany("UPDATE review_item SET exported_at=? WHERE review_id=?", [(now, i) for i in pending_ids])
    db.commit()
    return len(pending_ids)


def _require_csrf() -> None:
    supplied = request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf_token", "")
    if not supplied or not expected or not secrets.compare_digest(supplied, expected):
        abort(403)


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=6001)
