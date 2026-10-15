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


@pytest.mark.parametrize(
    ("native", "stub"),
    [
        ("youtube.fuzzy.replayweb.page/embed/w4mlQoAVurk", True),
        ("www.google.com/maps/embed?pb=!1m14!1m8", True),
        ("www.google.com/maps/d/embed?mid=1Gei5q", True),
        ("www.youtube-nocookie.com/embed/abc", True),
        ("player.vimeo.com/video/123", True),
        ("bookdash.org/why-we-do-what-we-do", False),
        ("A/Google_Maps", False),
    ],
)
def test_replay_stubs(native: str, stub: bool) -> None:
    from wsp_core.listing import is_replay_stub

    assert is_replay_stub(native) is stub
