import pytest

from wsp_core.ids import InvalidDocIdError, content_hash, make_doc_id, parse_doc_id


def test_round_trip_keeps_slashes_in_native_id() -> None:
    doc_id = make_doc_id("wikipedia-en", "A/Alan_Turing")
    assert doc_id == "wsp:wikipedia-en/A/Alan_Turing"
    assert parse_doc_id(doc_id) == ("wikipedia-en", "A/Alan_Turing")


@pytest.mark.parametrize("bad", ["wikipedia-en/x", "wsp:wikipedia-en", "wsp:Bad_Source/x", "wsp:ok/ x"])
def test_rejects_malformed_ids(bad: str) -> None:
    with pytest.raises(InvalidDocIdError):
        parse_doc_id(bad)


def test_content_hash_ignores_newline_style_and_trailing_space() -> None:
    assert content_hash("a  \r\nb\n") == content_hash("a\nb")
    assert content_hash("a") != content_hash("b")
