from wsp_index.text import drop_stopwords


def test_drop_stopwords_keeps_content_and_phrases() -> None:
    assert drop_stopwords("how do vaccines work") == "vaccines work"
    assert drop_stopwords("The Who") == "The Who"  # nothing would remain: unchanged
    assert drop_stopwords('"to be or not to be"') == '"to be or not to be"'
