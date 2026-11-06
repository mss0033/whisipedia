"""Titles that are file names are shown readable; real titles are left alone (field exam 2026-10-14)."""

from __future__ import annotations

import pytest

from wsp_core.titles import repaired_title, shown_title


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


@pytest.mark.parametrize(
    ("stored", "page", "repaired"),
    [
        # warc2zim cut the title at its first character reference (2026-11-05 deep dive)
        (
            "Mohamed HANCHANE - Auteur Encyclopédie de l",
            "<head><title>Mohamed HANCHANE - Auteur Encyclopédie de l&#039;Environnement</title>",
            "Mohamed HANCHANE - Auteur Encyclopédie de l'Environnement",
        ),
        (
            "What is email spoofing? | How it works ",
            "<title>\n  What is email spoofing? | How it works &amp; prevention | Cloudflare</title>",
            "What is email spoofing? | How it works & prevention | Cloudflare",
        ),
        (
            "Blog | Smithsonian American Women",
            b"<TITLE lang='en'>Blog | Smithsonian American Women&#039;s History Museum</TITLE>",
            "Blog | Smithsonian American Women's History Museum",
        ),
        (
            "What is email spoofing? | How it works",
            "<title>What is email spoofing? | How it works &amp; prevention</title>",
            "What is email spoofing? | How it works & prevention",
        ),
        # whole titles stay the archive's: a curated title is never the page's "<title> - Wikipedia"
        ("Alan Turing", "<title>Alan Turing - Wikipedia</title>", "Alan Turing"),
        ("AT&T", "<title>AT&amp;T</title>", "AT&T"),
        ("Q&A", "<title>Q&amp;A - Site</title>", "Q&A"),
        ("Title", "<p>no title element</p>", "Title"),
        ("", "<title>A &amp; B</title>", ""),
        ("A", "<title>A&amp;</title>", "A&"),
    ],
)
def test_repaired_title(stored: str, page: str | bytes, repaired: str) -> None:
    assert repaired_title(stored, page) == repaired
