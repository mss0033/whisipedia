#!/usr/bin/env python3
"""Permission requests for the library (owner request 2026-10-28): letters, drafts and their status.

Some sources may only be copied with the publisher's written permission (copyright), and some hosts
refuse automated clients in robots.txt, so the library may not fetch them (crawl). This script turns
``requests.json`` into everything the owner needs to ask, and keeps track of the answers:

* ``PERMISSIONS.html``: every request with its status, a button that opens the letter in the mail
  program (``mailto:``), the letter to copy, the contact page, and what to do next;
* ``<id>/letter.txt``: the letter; ``<id>/draft.eml``: the same as a draft e-mail (double-click: it
  opens unsent, ready to send, in Outlook, Thunderbird or Windows Mail);
* ``<id>/mark-sent.cmd``: double-click after sending (writes ``sent.txt`` with the date);
* ``status.json``: each request's state, read by the library's hourly sync (it raises an alert when a
  reply arrives, so the permission is recorded in the registry).

The owner sends every letter from their own mail program and signs it: the library never sends mail
and never writes the owner's name or address. The state of a request comes from files in its folder:

* ``sent*`` (``sent.txt`` from mark-sent.cmd, or the sent message saved there): sent, on that date;
* ``reply*`` (the answer saved as .eml, .msg, .pdf or pasted into reply.txt): answered;
* ``granted*`` / ``refused*``: decided (written when the answer is recorded).

A request sent ``FOLLOW_UP_DAYS`` ago without a reply offers a short follow-up letter.

Standard library only: it runs wherever Python 3.9+ does (the library runs it every hour from
``wsp collections manual --sync``; ``python permissions.py`` in this folder runs it by hand).
"""

# ruff: noqa: E501  (the letters are prose: their lines are as long as their sentences)
from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote

FOLLOW_UP_DAYS = 21
USER_AGENT = "Whisipedia/0.1 (personal offline archive)"
SIGNATURE = "[your name]"

ABOUT = (
    "Whisipedia is a personal offline reference library: one person keeps copies of reference works "
    "on their own computer and storage at home, to read without an Internet connection. It is not a "
    "website or a service: nothing in it is published, shared, lent or sold, and no one else can reach it."
)


def copyright_letter(r: dict[str, str]) -> tuple[str, str]:
    subject = f"Permission request: a private offline copy of {short(r['work'])}"
    body = f"""Hello,

I am writing to ask for permission to keep a private copy of {r["work"]}, {r["how"]}, in my personal offline library, Whisipedia.

{ABOUT}

The copy would be:
- for my own reading only, never published, shared or redistributed;
- kept unchanged, with your copyright and attribution notices;
- refreshed only from your own published editions;
- deleted at once if you ask.

If you grant this, a short reply saying so (and any conditions you set) is all I need. If you would rather I obtain the files another way, or if there is a fee, please let me know.

Thank you for your work and for considering this.

Kind regards,
{SIGNATURE}
"""
    return subject, body


def crawl_letter(r: dict[str, str]) -> tuple[str, str]:
    default = f"at present it does not fetch from {r['hosts']}; I download a few files a day by hand instead"
    situation = r.get("situation") or default
    subject = f"Request: allow a personal archive's downloader on {r['hosts'].split(' ')[0]}"
    alt = f"\n\n{r['alternative'][0].upper()}{r['alternative'][1:]}." if r.get("alternative") else ""
    body = f"""Hello,

I am writing about my personal offline library, Whisipedia. {ABOUT}

Its downloader identifies itself as "{USER_AGENT}" and follows robots.txt strictly, so {situation}. I would like to ask whether you would allow it to fetch {r["paths"]}.

It would fetch politely: {r["pace"]}, retrying slowly on errors, never in parallel, and only files you publish for download. If you agree, either of these would do:
- an Allow line in robots.txt for the user agent "Whisipedia", or
- a short reply giving permission (with any limits on rate or hours).{alt}

Thank you for publishing this material and for considering the request.

Kind regards,
{SIGNATURE}
"""
    return subject, body


def follow_up(r: dict[str, str], subject: str, sent: str) -> tuple[str, str]:
    body = f"""Hello,

I am following up on my message of {sent} ("{subject}"). I would be grateful for a short answer either way, or for the name of the right person to ask.

Kind regards,
{SIGNATURE}
"""
    return "Re: " + subject, body


def short(text: str) -> str:
    return text.split(" (")[0]


def letter(r: dict[str, str]) -> tuple[str, str]:
    return copyright_letter(r) if r["kind"] == "copyright" else crawl_letter(r)


def mailto(to: str, subject: str, body: str) -> str:
    return f"mailto:{quote(to)}?subject={quote(subject)}&body={quote(body)}"


def draft_eml(to: str, subject: str, body: str) -> bytes:
    msg = EmailMessage()
    if to:
        msg["To"] = to
    msg["Subject"] = subject
    msg["X-Unsent"] = "1"  # Outlook and Windows Mail open it as an unsent draft
    msg.set_content(body)
    return bytes(msg)


def first(folder: Path, prefix: str) -> Path | None:
    found = sorted(p for p in folder.iterdir() if p.is_file() and p.name.lower().startswith(prefix))
    return found[0] if found else None


def day_of(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        text = ""
    for token in text.replace("/", "-").split():
        try:
            return date.fromisoformat(token[:10]).isoformat()
        except ValueError:
            continue
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)  # noqa: UP017 (UTC alias is 3.11+)
    return mtime.date().isoformat()


def state_of(folder: Path, today: date) -> dict[str, str | int]:
    out: dict[str, str | int] = {"state": "to send"}
    for prefix, state in (("granted", "granted"), ("refused", "refused")):
        if (p := first(folder, prefix)) is not None:
            return {"state": state, "on": day_of(p), "file": p.name}
    if (p := first(folder, "reply")) is not None:
        return {"state": "answered", "on": day_of(p), "file": p.name}
    if (p := first(folder, "sent")) is not None:
        sent = day_of(p)
        waited = (today - date.fromisoformat(sent)).days
        out = {"state": "sent", "on": sent, "waited": waited}
        if waited >= FOLLOW_UP_DAYS:
            out["state"] = "follow up"
    return out


MARK_SENT = '@echo off\r\necho %date% %time% > "%~dp0sent.txt"\r\necho Marked as sent. The library notices within the hour.\r\npause\r\n'

CSS = (
    "body{font:15px/1.45 system-ui,sans-serif;margin:24px;max-width:1100px;background:#fff;color:#111}"
    "h2{margin:28px 0 4px}code{background:#eef;padding:1px 4px}.n{color:#666}.s{font-weight:600}"
    "pre{white-space:pre-wrap;background:#f6f6f6;padding:10px;border-radius:6px;max-height:16em;overflow:auto}"
    "a.b,button{display:inline-block;margin:4px 8px 4px 0;padding:6px 12px;border:1px solid #888;"
    "border-radius:6px;background:#f2f2f2;color:inherit;text-decoration:none;font:inherit;cursor:pointer}"
    ".granted{color:#170}.refused{color:#a00}.answered,.follow{color:#a60}"
    "@media(prefers-color-scheme:dark){body{background:#111;color:#eee}code{background:#223}"
    "pre{background:#1c1c1c}a.b,button{background:#222}}"
)

NEXT = {
    "to send": "Press <b>Write the e-mail</b> (or open <code>draft.eml</code>), put your name where it "
    "says [your name], and send it. Then double-click <code>mark-sent.cmd</code> in this request's folder.",
    "sent": "Waiting for an answer. Save the reply into this request's folder as <code>reply.eml</code> "
    "(or paste it into <code>reply.txt</code>).",
    "follow up": "No answer yet: press <b>Send a follow-up</b>. If there is still no answer, the contact "
    "page may list another way to reach them.",
    "answered": "An answer is saved. The library has raised an alert; Claude reads it and records the "
    "permission (or the refusal) in the registry.",
    "granted": "Granted: the source is or will be taken in under that permission.",
    "refused": "Refused: the source stays out of the library.",
}


def render(requests: list[dict[str, str]], status: dict[str, dict[str, str | int]], folder_win: str) -> str:
    today = date.today().isoformat()
    parts = [
        f"<!doctype html><meta charset=utf-8><title>Permission requests</title><style>{CSS}</style>",
        "<h1>Permission requests</h1>",
        f"<p class=n>Updated {today}. Each request has a folder under <code>{html.escape(folder_win)}</code>. "
        "You send every letter from your own e-mail and sign it; the library never sends mail. "
        "When an answer comes, save it in the request's folder and the library will pick it up.</p>",
        "<p><b>Copyright</b> requests ask the publisher to allow a private copy. <b>Crawl</b> requests ask a "
        "website to let the library's downloader in (its robots.txt keeps it out today), which also ends "
        "the manual downloads for that source.</p>",
    ]
    for r in requests:
        st = status[r["id"]]
        state = str(st["state"])
        subject, body = letter(r)
        if state == "follow up":
            subject, body = follow_up(r, subject, str(st["on"]))
        when = f" since {st['on']}" if "on" in st else ""
        waited = f", {st['waited']} days" if "waited" in st else ""
        parts.append(f"<h2>{html.escape(r['publisher'])}</h2>")
        parts.append(
            f"<p><span class='s {state.split()[0]}'>{html.escape(state)}{when}{waited}</span> &middot; "
            f"{html.escape(r['kind'])} &middot; for <code>{html.escape(', '.join(r['sources']))}</code></p>"
        )
        parts.append(f"<p>{NEXT[state]}</p>")
        to = r.get("to", "")
        if state in ("to send", "follow up", "sent"):
            label = "Send a follow-up" if state == "follow up" else "Write the e-mail"
            if to:
                parts.append(
                    f'<a class=b href="{html.escape(mailto(to, subject, body), quote=True)}">{label}</a>'
                )
                parts.append(f'<a class=b href="{html.escape(r["id"])}/draft.eml">Open draft.eml</a>')
            parts.append(
                f'<a class=b href="{html.escape(r["contact_url"], quote=True)}" target=_blank>Contact page</a>'
            )
            parts.append(f"<button onclick=\"copy('{html.escape(r['id'])}')\">Copy the letter</button>")
            to_line = (
                f"To: <code>{html.escape(to)}</code>"
                if to
                else "No e-mail address: use the contact page (paste the letter into its form)."
            )
            parts.append(f"<p class=n>{to_line} Found: {html.escape(r['verified'])}.</p>")
            parts.append(
                f"<pre id='{html.escape(r['id'])}'>Subject: {html.escape(subject)}\n\n{html.escape(body)}</pre>"
            )
    parts.append(
        "<script>function copy(id){const t=document.getElementById(id).innerText;"
        "navigator.clipboard.writeText(t).then(()=>alert('Copied'),()=>alert('Select the text and copy it'));}</script>"
    )
    return "\n".join(parts) + "\n"


def run(here: Path, folder_win: str = "") -> dict[str, dict[str, str | int]]:
    requests = json.loads((here / "requests.json").read_text(encoding="utf-8"))["requests"]
    today = date.today()
    status: dict[str, dict[str, str | int]] = {}
    for r in requests:
        folder = here / r["id"]
        folder.mkdir(exist_ok=True)
        subject, body = letter(r)
        (folder / "letter.txt").write_text(
            f"To: {r.get('to') or r['contact_url']}\nSubject: {subject}\n\n{body}", encoding="utf-8"
        )
        (folder / "draft.eml").write_bytes(draft_eml(r.get("to", ""), subject, body))
        mark = folder / "mark-sent.cmd"
        if not mark.exists():
            mark.write_bytes(MARK_SENT.encode("ascii"))
        status[r["id"]] = {**state_of(folder, today), "sources": ", ".join(r["sources"]), "kind": r["kind"]}
    (here / "PERMISSIONS.html").write_text(
        render(requests, status, folder_win or str(here)), encoding="utf-8"
    )
    tmp = here / "status.json.partial"
    tmp.write_text(json.dumps(status, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(here / "status.json")
    return status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    ap.add_argument(
        "--dir", type=Path, default=Path(__file__).resolve().parent, help="the _permissions folder"
    )
    ap.add_argument("--windows-path", default="", help="how Windows names the folder (shown on the page)")
    args = ap.parse_args(argv)
    for rid, st in run(args.dir, args.windows_path).items():
        print(f"{rid:34} {st['state']}" + (f" ({st['on']})" if "on" in st else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
