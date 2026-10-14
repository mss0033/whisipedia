from wsp_api.addresses import address_of, encode_native


def test_native_ids_that_break_parsing_are_escaped() -> None:
    assert encode_native("xkcd.com/936/") == "xkcd.com/936%2F"
    assert encode_native("printer.gotquestions.net/GeneratePF?articleId=3547") == (
        "printer.gotquestions.net/GeneratePF%3FarticleId=3547"
    )
    assert encode_native("a b#c%d//") == "a%20b%23c%25d%2F%2F"
    assert encode_native("en/Alan_Turing") == "en/Alan_Turing"  # readable when nothing breaks
    assert address_of("s", "x/") == "wsp://s/x%2F"
    assert address_of("s") == "wsp://s"
