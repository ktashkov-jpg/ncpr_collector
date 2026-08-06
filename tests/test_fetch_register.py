# -*- coding: utf-8 -*-
"""URL convention for the published register workbooks (offline)."""
import datetime as dt

from app.fetch_register import latest_publication, register_url, step_back


def test_url_matches_the_published_convention():
    """The known-good URL supplied 2026-08-06."""
    assert register_url(dt.date(2026, 8, 2), 4) == (
        "https://portal.ncpr.bg/download/08-2026/02-08-2026/"
        "Prilogenie-4-02-08-2026.xlsx")


def test_url_zero_pads_single_digit_months():
    assert register_url(dt.date(2026, 2, 2), 1) == (
        "https://portal.ncpr.bg/download/02-2026/02-02-2026/"
        "Prilogenie-1-02-02-2026.xlsx")


def test_latest_publication_on_or_after_the_second():
    assert latest_publication(dt.date(2026, 8, 6)) == dt.date(2026, 8, 2)
    assert latest_publication(dt.date(2026, 8, 2)) == dt.date(2026, 8, 2)


def test_latest_publication_before_the_second_falls_back_a_month():
    assert latest_publication(dt.date(2026, 8, 1)) == dt.date(2026, 7, 2)


def test_latest_publication_crosses_the_year_boundary():
    assert latest_publication(dt.date(2026, 1, 1)) == dt.date(2025, 12, 2)


def test_step_back_crosses_the_year_boundary():
    assert step_back(dt.date(2026, 1, 2)) == dt.date(2025, 12, 2)
