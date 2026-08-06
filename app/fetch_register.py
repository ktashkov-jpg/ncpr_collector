# -*- coding: utf-8 -*-
"""Download a published NCPR register workbook by convention, not by path.

The registers are republished on the 2nd of each month at a URL built from
the publication date:

    /download/<MM-YYYY>/<DD-MM-YYYY>/Prilogenie-<n>-<DD-MM-YYYY>.xlsx

e.g. https://portal.ncpr.bg/download/08-2026/02-08-2026/Prilogenie-4-02-08-2026.xlsx

So no workbook path needs hardcoding: the latest edition is derivable from
today's date. If the computed month is not yet published the fetch steps
back a month rather than failing, since publication can lag the 2nd.

These files are public downloads, not SOAP product calls, so they do not
consume the daily request cap — but they are still requests to the same
host and are logged.

⚠ None of the published workbooks contain GTIN. They supply the catalogue
(national ids, INN, ATC, form, pack, prices); the barcode association exists
only behind the SOAP service.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import urllib.error
import urllib.request
from pathlib import Path

from app import soap
from app.config import Config
from app.store import Store

BASE = "https://portal.ncpr.bg"
PUBLICATION_DAY = 2


def register_url(date: dt.date, appendix: int = 4, stem: str = "Prilogenie") -> str:
    day = f"{date.day:02d}-{date.month:02d}-{date.year}"
    month = f"{date.month:02d}-{date.year}"
    return f"{BASE}/download/{month}/{day}/{stem}-{appendix}-{day}.xlsx"


def latest_publication(today: dt.date | None = None) -> dt.date:
    """The most recent 2nd-of-the-month on or before today."""
    today = today or dt.date.today()
    if today.day >= PUBLICATION_DAY:
        return dt.date(today.year, today.month, PUBLICATION_DAY)
    previous = dt.date(today.year, today.month, 1) - dt.timedelta(days=1)
    return dt.date(previous.year, previous.month, PUBLICATION_DAY)


def step_back(date: dt.date) -> dt.date:
    previous = dt.date(date.year, date.month, 1) - dt.timedelta(days=1)
    return dt.date(previous.year, previous.month, PUBLICATION_DAY)


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.fetch_register")
    parser.add_argument("--appendix", type=int, default=4,
                        help="1-4; 4 is the union of 1+2+3 and the collection "
                             "target (default: 4)")
    parser.add_argument("--date", help="DD-MM-YYYY; default: latest 2nd-of-month")
    parser.add_argument("--stem", default="Prilogenie",
                        help="filename stem, in case the site spelling changes")
    parser.add_argument("--attempts", type=int, default=3,
                        help="months to step back if not yet published")
    parser.add_argument("--out", help="destination file (default: input dir)")
    args = parser.parse_args()

    config = Config()
    config.ensure_dirs()
    store = Store(config.db_path)

    if args.date:
        day, month, year = (int(p) for p in args.date.split("-"))
        date = dt.date(year, month, day)
    else:
        date = latest_publication()

    opener = soap.make_opener(config.insecure_tls)
    for attempt in range(args.attempts):
        url = register_url(date, args.appendix, args.stem)
        print(f"trying {url}")
        try:
            with opener.open(urllib.request.Request(url),
                             timeout=config.timeout_s) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            print(f"  HTTP {exc.code}")
            if exc.code == 404 and attempt < args.attempts - 1:
                date = step_back(date)
                continue
            store.log(task_id=f"fetch:appendix{args.appendix}",
                      http_status=exc.code, note=url)
            return 1
        except Exception as exc:                    # noqa: BLE001
            print(f"  {type(exc).__name__}: {exc}")
            return 1

        destination = Path(args.out) if args.out else Path(
            config.input_dir) / url.rsplit("/", 1)[-1]
        destination.write_bytes(body)
        digest = soap.sha256(body)
        store.log(task_id=f"fetch:appendix{args.appendix}", http_status=200,
                  bytes=len(body), note=f"{url} sha256={digest[:16]}")
        print(f"\nsaved {destination}")
        print(f"  {len(body)} bytes  sha256={digest}")
        print(f"  published {date:%d-%m-%Y}")
        print("\nNote: this workbook contains no GTIN. It supplies the "
              "catalogue only;\nthe barcode association comes from the SOAP "
              "service.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
