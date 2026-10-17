"""nautilus archives (zimgit libraries, the USDA canning guide): a catalogue of documents."""

# ruff: noqa: E501 - the catalogue fixture is written as the scraper writes it, one entry a line

from wsp_core import nautilus

DATABASE = """var DATABASE = [
{'_id': '00000', 'ti': 'Introduction', 'dsc': '', 'aut': '', 'fp': ['INTRO.pdf']},
{'_id': '00004', 'ti': 'Part 4', 'dsc': 'Vegetables and Vegetable Products', 'aut': 'USDA', 'fp': ['GUIDE04.pdf']},
{'_id': '00009', 'ti': 'Handbook of Knots and Splices', 'dsc': 'Rope work', 'aut': '', 'fp': ['Knots (3).pdf', 'Knots (4).pdf']},
{'_id': '../x', 'ti': 'Bad id', 'dsc': '', 'aut': '', 'fp': ['x.pdf']},
{'_id': '00010', 'ti': 'No files', 'dsc': '', 'aut': '', 'fp': []},
];
"""


def test_the_catalogue_is_read_as_literals() -> None:
    items = nautilus.items(DATABASE.encode())
    assert [i.id for i in items] == ["00000", "00004", "00009"]  # no path in an id, no empty entry
    assert items[1].heading == "Part 4: Vegetables and Vegetable Products"  # a short title says little
    assert items[2].heading == "Handbook of Knots and Splices"
    assert items[2].files == ("Knots (3).pdf", "Knots (4).pdf")
    assert nautilus.items(b"var DATABASE = [__import__('os').system('x')];") == []  # never evaluated
    assert nautilus.items(b"<html>not a catalogue</html>") == []


def test_the_front_page_and_a_document_page() -> None:
    items = nautilus.items(DATABASE)
    home = nautilus.home_html("USDA Guide to Home Canning", "Safer canning", items)
    assert '<a href="nautilus/00004">Part 4: Vegetables and Vegetable Products</a> (USDA)' in home
    assert "<p>3 documents.</p>" in home
    page = nautilus.item_html(items[2], {"Knots (3).pdf": "The bowline.\n\nThe sheet bend.\fPage two"})
    assert '<a href="../files/Knots%20%283%29.pdf">Knots (3).pdf</a>' in page
    assert "<h2>Knots (3).pdf</h2><p>The bowline.</p><p>The sheet bend.</p><p>Page two</p>" in page
    assert "<p>Rope work</p>" in page
