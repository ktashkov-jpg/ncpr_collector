# -*- coding: utf-8 -*-
"""Collection policy, all overridable by environment variable.

Defaults are the runbook's conservative starting values (§9). They are
deliberately slow: NCPR publishes no request quota, reportedly monitors
automated activity, and access depends on a source-IP allowlist that can be
withdrawn. The cost of being too slow is time; the cost of being too fast is
losing the only access path that exists.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    # --- service (runbook §2) ---
    endpoint: str = _str(
        "NCPR_ENDPOINT",
        "https://portal.ncpr.bg:443/registers/MedicinalProductsRegistersService")
    namespace: str = _str("NCPR_NAMESPACE",
                          "http://webservice.portal.ncprmp.sirma.com")

    # --- rate policy (runbook §9) ---
    delay_min_s: int = _int("NCPR_DELAY_MIN_S", 300)
    delay_max_s: int = _int("NCPR_DELAY_MAX_S", 600)
    daily_cap: int = _int("NCPR_DAILY_CAP", 80)
    window_start_hour: int = _int("NCPR_WINDOW_START_HOUR", 8)   # local time
    window_end_hour: int = _int("NCPR_WINDOW_END_HOUR", 18)

    # --- transport ---
    timeout_s: int = _int("NCPR_TIMEOUT_S", 60)
    # The runbook's successful test used curl -k because the host could not
    # build the Let's Encrypt chain. That is a host CA problem, not a server
    # problem, and the fix belongs in the image (see Dockerfile), not here.
    # Disabling verification is opt-in and loudly logged.
    insecure_tls: bool = _str("NCPR_INSECURE_TLS", "0") == "1"

    # --- paths (container volumes) ---
    #
    # Two roots, because the two kinds of file want different storage:
    #
    #   db_dir       SQLite, lock file, HALTED marker. Small, write-heavy,
    #                and it needs working POSIX locks -- this is the file
    #                that holds the daily counter enforcing the rate cap,
    #                so its integrity is a safety property, not bookkeeping.
    #                Keep it on a local filesystem. Never on CIFS/SMB.
    #
    #   archive_dir  raw XML, exports, cached WSDL. Append-only, no locking,
    #                and the part worth putting on a shared folder so it can
    #                be read and copied without touching the collector.
    #
    # Both fall back to data_dir, so a single-path deployment is unchanged.
    data_dir: str = _str("NCPR_DATA_DIR", "/data")
    db_dir_override: str = _str("NCPR_DB_DIR", "")
    archive_dir_override: str = _str("NCPR_ARCHIVE_DIR", "")

    @property
    def db_dir(self) -> str:
        return self.db_dir_override or self.data_dir

    @property
    def archive_dir(self) -> str:
        return self.archive_dir_override or self.data_dir

    @property
    def db_path(self) -> str:
        return os.path.join(self.db_dir, "ncpr.sqlite3")

    @property
    def raw_dir(self) -> str:
        return os.path.join(self.archive_dir, "raw")

    @property
    def input_dir(self) -> str:
        """Reference data supplied by the operator (Annex 4, drug_ref,
        salvia). Kept apart from raw/ and the exports so inputs are never
        confused with harvest — same layout as bda-smpc-corpus."""
        return os.path.join(self.archive_dir, "input")

    @property
    def wsdl_path(self) -> str:
        return os.path.join(self.archive_dir, "service.wsdl")

    @property
    def export_path(self) -> str:
        return os.path.join(self.archive_dir, "ncpr_gtin_crosswalk.csv")

    @property
    def halt_file(self) -> str:
        """Written on a hard stop (403/429). Requires deliberate human
        removal before the collector will run again — see collect.py.
        Lives with the database: it is operational state, not output."""
        return os.path.join(self.db_dir, "HALTED")

    @property
    def lock_file(self) -> str:
        return os.path.join(self.db_dir, "collector.lock")

    def ensure_dirs(self) -> None:
        for path in (self.db_dir, self.archive_dir, self.raw_dir, self.input_dir):
            os.makedirs(path, exist_ok=True)

    def validate(self) -> None:
        if self.delay_min_s > self.delay_max_s:
            raise ValueError("NCPR_DELAY_MIN_S exceeds NCPR_DELAY_MAX_S")
        if self.delay_min_s < 60:
            raise ValueError(
                "Refusing a sub-60s delay against a monitored allowlisted "
                "service. Raise NCPR_DELAY_MIN_S or edit this check knowingly.")
        if self.daily_cap > 500:
            raise ValueError(
                "NCPR_DAILY_CAP above 500 is not a conservative policy; "
                "get written rate guidance from NCPR first.")
