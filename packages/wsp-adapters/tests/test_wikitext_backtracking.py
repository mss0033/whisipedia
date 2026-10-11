import time

from wsp_adapters.wikitext import wikitext_to_text


def test_attribute_like_table_line_is_linear() -> None:
    page = "{|\n|" + "a=b " * 200 + "x\n|}\n"
    start = time.perf_counter()
    wikitext_to_text(page)
    assert time.perf_counter() - start < 0.5


def test_cell_attributes_still_stripped() -> None:
    text, _ = wikitext_to_text('{|\n| style="color:red" class=x | Cell text\n|}\n')
    assert "Cell text" in text
    assert "style" not in text
