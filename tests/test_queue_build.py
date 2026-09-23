import os
import subprocess
import sys
from pathlib import Path

from app.store import Store


def test_full_catalogue_queue_appends_without_replacing_existing_state(tmp_path):
    """The full sweep must extend the live SQLite manifest, never rebuild it."""
    source = tmp_path / "ncpr_scrape_canonical.csv"
    source.write_text(
        "national_num,in_active,source_datasets\n"
        "100,True,active_all_status\n"
        "200,False,otc_all_status\n",
        encoding="utf-8-sig",
    )
    db_dir = tmp_path / "state"
    store = Store(str(db_dir / "ncpr.sqlite3"))
    store.add_task("fwd:100", "forward", "100", 20, "existing Appendix task")
    store.finish("fwd:100", "no_gtin")

    env = os.environ | {"NCPR_DATA_DIR": str(db_dir)}
    result = subprocess.run(
        [sys.executable, "-m", "app.queue_build", "--catalogue-csv", str(source)],
        cwd=Path(__file__).resolve().parents[1], env=env,
        text=True, capture_output=True, check=True,
    )

    # The process keeps the original row and only adds the unseen national ID.
    rows = list(store.db.execute(
        "SELECT task_id, priority, reason, status FROM queue ORDER BY task_id"))
    assert [tuple(row) for row in rows] == [
        ("fwd:100", 20, "existing Appendix task", "no_gtin"),
        ("fwd:200", 60, "full NCPR catalogue (other status)", "pending"),
    ]
    assert store.get_meta("full_catalogue_source") == source.name
    assert "newly queued (existing tasks were retained):" in result.stdout
