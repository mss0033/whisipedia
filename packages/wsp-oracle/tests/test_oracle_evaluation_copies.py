from wsp_oracle.evaluation import graded, ndcg_at


def test_copies_of_a_judged_article_count_once_and_lesser_ones_count_less() -> None:
    judged = {"wsp:wikipedia-en-zim/Vaccine": 2}
    ranked = [
        "wsp:kiwix-wikipedia-en-simple-all/Vaccine",  # lesser copy: also relevant
        "wsp:wikipedia-top10/en/Vaccine",  # the same article in the other full copy: the answer
        "wsp:wikipedia-en-zim/Vaccine",  # a second copy of it: counted once
        "wsp:mdwiki/Other",
    ]
    keys, ideal, scoring = graded(ranked, judged)
    assert keys == ["wsp:kiwix-wikipedia-en-simple-all/Vaccine", "wikipedia:Vaccine", "wsp:mdwiki/Other"]
    assert scoring["wikipedia:Vaccine"] == 2
    assert scoring["wsp:kiwix-wikipedia-en-simple-all/Vaccine"] == 1
    assert ideal == {"wikipedia:Vaccine": 2}
    score = ndcg_at(keys, scoring, ideal_from=ideal)
    assert 0.6 < score < 1.0  # the answer second, behind a lesser copy


def test_a_judged_lesser_copy_keeps_its_own_grade() -> None:
    keys, ideal, scoring = graded(
        ["wsp:mdwiki/Asthma"], {"wsp:mdwiki/Asthma": 2, "wsp:wikipedia-en-zim/Asthma": 1}
    )
    assert scoring["wsp:mdwiki/Asthma"] == 2
    assert ndcg_at(keys, scoring, ideal_from=ideal) > 0.7
