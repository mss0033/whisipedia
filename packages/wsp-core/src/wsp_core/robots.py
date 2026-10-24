"""robots.txt as RFC 9309 defines it, for every place the library decides whether it may fetch a URL.

``urllib.robotparser`` (used until 2026-10-24) reads ``*`` and ``$`` in a rule literally and takes the
first matching rule, not the longest: ``Disallow: */data.pdf`` (legislation.gov.uk), ``Disallow: /*_pdf``
(J-STAGE) and ``Disallow: /server/oai/*`` (WHO IRIS, DSpace) never matched, so paths their hosts deny
looked allowed. Here:

* the group is the one whose ``User-agent`` equals our product token (``Whisipedia``, case-insensitive),
  else the ``*`` group; several groups for the same agent are merged;
* a rule's path may hold ``*`` (any characters) and end in ``$`` (end of the path);
* the longest matching rule wins, and ``Allow`` wins a tie; no matching rule allows;
* ``/robots.txt`` itself is always allowed; ``Crawl-delay`` (not in the RFC, widely used) is read from
  the chosen group.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

_SAFE = "/:@!$&'()*+,;=-._~%"  # reserved and unreserved characters kept as they are when re-encoding


def _normalise(path: str) -> str:
    """Percent-encoding made uniform, so ``/a%20b`` and ``/a b`` compare equal (RFC 9309 §2.2.2)."""
    return quote(unquote(path), safe=_SAFE)


@dataclass(frozen=True, slots=True)
class _Rule:
    allow: bool
    path: str  # normalised, with any ``*`` and a trailing ``$``
    pattern: re.Pattern[str]

    @staticmethod
    def of(allow: bool, raw: str) -> _Rule:
        path = _normalise(raw)
        anchored = path.endswith("$")
        body = path[:-1] if anchored else path
        rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
        return _Rule(allow, path, re.compile(rx + ("$" if anchored else ""), re.DOTALL))

    @property
    def length(self) -> int:
        return len(self.path)


@dataclass(slots=True)
class _Group:
    agents: list[str] = field(default_factory=list[str])
    rules: list[_Rule] = field(default_factory=list[_Rule])
    delay: float | None = None


def product_token(user_agent: str) -> str:
    """``Whisipedia/0.1 (personal offline archive)`` -> ``whisipedia``."""
    return re.split(r"[/\s]", user_agent.strip(), maxsplit=1)[0].lower()


class Robots:
    """One host's parsed robots.txt."""

    def __init__(self, text: str) -> None:
        self._groups: list[_Group] = []
        current: _Group | None = None
        in_agents = False
        for raw in text.lstrip("﻿").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if current is None or not in_agents:
                    current = _Group()
                    self._groups.append(current)
                current.agents.append(value.lower())
                in_agents = True
                continue
            in_agents = False
            if current is None:
                continue  # a rule before any User-agent line belongs to no group
            if key in ("allow", "disallow"):
                if value:
                    current.rules.append(_Rule.of(key == "allow", value))
                # an empty Disallow allows everything: no rule to add
            elif key == "crawl-delay":
                with contextlib.suppress(ValueError):
                    current.delay = float(value)

    def _group_for(self, user_agent: str) -> _Group | None:
        token = product_token(user_agent)
        mine = [g for g in self._groups if token in g.agents]
        chosen = mine or [g for g in self._groups if "*" in g.agents]
        if not chosen:
            return None
        merged = _Group()
        for g in chosen:
            merged.rules.extend(g.rules)
            if g.delay is not None and merged.delay is None:
                merged.delay = g.delay
        return merged

    def can_fetch(self, user_agent: str, url: str) -> bool:
        parts = urlsplit(url)
        path = parts.path or "/"
        if path == "/robots.txt":
            return True
        target = _normalise(path + (f"?{parts.query}" if parts.query else ""))
        group = self._group_for(user_agent)
        if group is None:
            return True
        best: _Rule | None = None
        for rule in group.rules:
            if rule.pattern.match(target) and (
                best is None or rule.length > best.length or (rule.length == best.length and rule.allow)
            ):
                best = rule
        return best is None or best.allow

    def crawl_delay(self, user_agent: str) -> float | None:
        group = self._group_for(user_agent)
        return group.delay if group is not None else None


def parse(raw: bytes | str | None) -> Robots | None:
    """A robots.txt body, or None for none at all (a site that answers every path with an HTML page
    has no robots.txt)."""
    if raw is None:
        return None
    text = (raw[: 512 * 1024].decode("utf-8", "replace") if isinstance(raw, bytes) else raw).lstrip("﻿")
    if text.lstrip()[:20].lower().startswith(("<!doctype", "<html")):
        return None
    return Robots(text)
