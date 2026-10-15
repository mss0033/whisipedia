"""Titles that are file names are shown readable; real titles are left alone (field exam 2026-10-14)."""

from __future__ import annotations

import pytest

from wsp_core.titles import shown_title


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("Instructor Guide.pdf", "Instructor Guide"),
        (
            "National_Incident_Management System_Third Edition_October_2017.pdf",
            "National Incident Management System Third Edition October 2017",
        ),
        ("ics_taining_reference_guide.pdf", "ics taining reference guide"),
        ("Field%20Manual%2021-76.pdf", "Field Manual 21-76"),
        ("Wi-Fi", "Wi-Fi"),
        ("COVID-19", "COVID-19"),
        ("pthread_create(3)", "pthread_create(3)"),
        ('What does if __name__ == "__main__": do?', 'What does if __name__ == "__main__": do?'),
        ("Bélády's anomaly", "Bélády's anomaly"),
        ("", ""),
    ],
)
def test_shown_title(raw: str, shown: str) -> None:
    assert shown_title(raw) == shown
