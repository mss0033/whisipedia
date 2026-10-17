"""freeCodeCamp-scraper archives (Rosetta Code): a challenge is Markdown behind an app stub."""

# ruff: noqa: E501 - expected HTML is clearer on one line

import json

from wsp_core import fcc

STUB = (
    '<html><head><title>Soundex</title><meta http-equiv="refresh" '
    "content=\"0;URL='../../../index.html#rosetta-code/challenges/5a23'\" /></head><body></body></html>"
)
MD = """---
id: 5a23
title: Soundex
dashedName: soundex
---

# --description--

**Soundex** codes *names*. The name `Quincy` is `Q520`.

<ul>
  <li>A rule.</li>
</ul>

# --instructions--

Write a function.

# --hints--

`soundex` should be a function.

```js
assert(typeof soundex == 'function');
```

# --seed--

## --seed-contents--

```js
function soundex(s) {}
```

# --solutions--

```js
function soundex(s) { return s < 2; }
```
"""


def test_a_stub_names_its_challenge() -> None:
    path = "index/rosetta-code/challenges/5a23.html"
    assert fcc.content_entry(path, STUB) == "content/curriculum/rosetta-code/challenges/5a23.md"
    assert fcc.content_entry("index/other.html", STUB) is None  # the refresh names another challenge
    assert fcc.content_entry(path, "<html><body>A page</body></html>") is None


def test_a_challenge_reads_as_description_instructions_tests_and_a_solution() -> None:
    page = fcc.page_html(MD.encode())
    assert page is not None
    assert page.startswith("<html><head><title>Soundex</title></head><body><h1>Soundex</h1>")
    assert (
        "<p><strong>Soundex</strong> codes <em>names</em>. The name <code>Quincy</code> is <code>Q520</code>.</p>"
        in page
    )
    assert "<ul> <li>A rule.</li> </ul>" in page
    assert "<h2>Instructions</h2><p>Write a function.</p>" in page
    assert "<h2>Tests</h2>" in page
    assert "<pre><code>assert(typeof soundex == &#x27;function&#x27;);</code></pre>" in page
    assert "<h2>A solution</h2><pre><code>function soundex(s) { return s &lt; 2; }</code></pre>" in page
    assert "seed" not in page.lower()  # the empty starting code is not shown
    assert fcc.page_html(b"no sections") is None


def test_the_front_page_lists_every_challenge() -> None:
    index = {
        "rosetta-code": {
            "rosetta-code-challenges": [
                {"title": "100 doors", "slug": "5948"},
                {"title": "x", "slug": "../y"},
            ]
        }
    }
    home = fcc.home_html("Rosetta Code", json.dumps(index).encode())
    assert home is not None
    assert '<a href="index/rosetta-code/rosetta-code-challenges/5948.html">100 doors</a>' in home
    assert "<p>1 challenge.</p>" in home
    assert fcc.home_html("x", b"{}") is None
