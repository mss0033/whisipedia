"""``wsp``: the Whisipedia command line.

Read commands (``search``, ``doc``, ``sources``, ``changes``) go through the public SDK. With
``--url`` (or ``WSP_API_URL``) they talk to a running server; otherwise they run the API in-process on
the backends from :mod:`wsp_cli.bootstrap`, without opening a socket.

argparse rather than Typer/Click: the surface is small, it keeps the dependency tree (and the offline
audit) minimal, and it type-checks cleanly under pyright strict.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Protocol, TextIO

import httpx
from pydantic import BaseModel

from wsp_api import create_app
from wsp_api.inprocess import InProcessTransport
from wsp_api.services import DEFAULT_HOST, DEFAULT_PORT
from wsp_cli import bootstrap
from wsp_core.registry import RegistryError, load_registry
from wsp_sdk import WspApiError, WspClient

Handler = Callable[[argparse.Namespace, TextIO], int]


class _Subparsers(Protocol):
    def add_parser(self, name: str, *, help: str, description: str) -> argparse.ArgumentParser: ...


def _emit(out: TextIO, model: BaseModel) -> None:
    out.write(model.model_dump_json(indent=2) + "\n")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _client(args: argparse.Namespace) -> WspClient:
    url: str | None = args.url or os.environ.get("WSP_API_URL")
    if url:
        return WspClient(url)
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    print(f"note: {bootstrap.BACKEND_NOTE}", file=sys.stderr)
    app = create_app(bootstrap.build_services(cfg, lay), bootstrap.api_settings(cfg, lay))
    return WspClient(http=httpx.Client(transport=InProcessTransport(app), base_url=f"http://{DEFAULT_HOST}"))


# --- commands -----------------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace, out: TextIO) -> int:
    host: str = args.host
    if not _is_loopback(host) and not args.allow_remote:
        print(f"error: refusing to bind non-loopback {host!r} without --allow-remote", file=sys.stderr)
        return 2
    import uvicorn  # local import: only `serve` needs the ASGI server

    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    settings = bootstrap.api_settings(cfg, lay, static_dir=args.static_dir)
    if not _is_loopback(host):
        settings = replace(settings, allowed_hosts=(*settings.allowed_hosts, host))
    print(f"note: {bootstrap.BACKEND_NOTE}", file=sys.stderr)
    app = create_app(bootstrap.build_services(cfg, lay), settings)
    out.write(f"Whisipedia API on http://{host}:{args.port}/ (static: {settings.static_dir})\n")
    uvicorn.run(app, host=host, port=args.port, server_header=False, proxy_headers=False, log_level="info")
    return 0


def cmd_search(args: argparse.Namespace, out: TextIO) -> int:
    with _client(args) as wsp:
        res = wsp.search(" ".join(args.query), sources=args.source, limit=args.limit, offset=args.offset)
    if args.json:
        _emit(out, res)
        return 0
    if not res.hits:
        out.write(f"No results for {res.query!r}.\n")
    for i, h in enumerate(res.hits, start=res.offset + 1):
        out.write(f"{i:>3}. {h.title}\n     {h.address}  [{h.source_id}]\n")
        if h.snippet:
            out.write(f"     {h.snippet}\n")
    if res.next_offset is not None:
        out.write(f"\nMore: --offset {res.next_offset}\n")
    return 0


def cmd_doc(args: argparse.Namespace, out: TextIO) -> int:
    with _client(args) as wsp:
        doc = wsp.document(args.doc_id, at=args.at)
    if args.json:
        _emit(out, doc)
        return 0
    out.write(f"{doc.title}\n{'=' * len(doc.title)}\n")
    out.write(f"{doc.address}  license: {doc.license}  version: {doc.content_hash[:12]}\n\n{doc.text}\n")
    return 0


def cmd_sources(args: argparse.Namespace, out: TextIO) -> int:
    with _client(args) as wsp:
        items = list(wsp.iter_sources())
    if args.json:
        out.write(json.dumps([s.model_dump(mode="json") for s in items], indent=2) + "\n")
        return 0
    for s in items:
        out.write(f"{s.id:<34} {s.tier:<3} {s.status:<10} {s.current_generation or '-':<12} {s.name}\n")
    return 0


def cmd_changes(args: argparse.Namespace, out: TextIO) -> int:
    with _client(args) as wsp:
        if args.follow:
            if not (args.url or os.environ.get("WSP_API_URL")):
                print("error: --follow needs a running server (--url)", file=sys.stderr)
                return 2
            for item in wsp.stream_changes(since=args.since, max_seconds=args.max_seconds):
                c = item.change
                resume = f"  @{item.cursor}" if item.cursor else ""
                out.write(f"{c.generation_id}  {c.kind:<8} {c.doc_id}{resume}\n")
                out.flush()
            return 0
        page = wsp.changes(since=args.since, limit=args.limit)
    if args.json:
        _emit(out, page)
        return 0
    for c in page.items:
        out.write(f"{c.generation_id}  {c.kind:<8} {c.doc_id}\n")
    out.write(f"cursor: {page.cursor}\n")
    return 0


def cmd_registry_validate(args: argparse.Namespace, out: TextIO) -> int:
    lay = bootstrap.layout(args.config_dir)
    registry: Path = args.registry or lay.registry
    schema: Path = args.schema or lay.schema
    try:
        specs = load_registry(registry, schema)
    except (RegistryError, FileNotFoundError) as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        return 1
    by_status: dict[str, int] = {}
    for s in specs:
        by_status[s.status.value] = by_status.get(s.status.value, 0) + 1
    summary = ", ".join(f"{n} {k}" for k, n in sorted(by_status.items()))
    out.write(f"ok: {len(specs)} sources ({summary})\n")
    return 0


def cmd_config_show(args: argparse.Namespace, out: TextIO) -> int:
    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.redact(bootstrap.load(lay))
    out.write(json.dumps(cfg, indent=2, sort_keys=True, default=str) + "\n")
    return 0


# --- parser -------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wsp", description="Whisipedia: a private offline library.")
    p.add_argument("--config-dir", type=Path, default=None, help="default: $WSP_CONFIG_DIR or <repo>/config")
    p.add_argument("--url", default=None, help="API base URL (default: $WSP_API_URL, else run in-process)")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, handler: Handler, help_: str, parent: _Subparsers = sub) -> argparse.ArgumentParser:
        sp = parent.add_parser(name, help=help_, description=help_)
        sp.set_defaults(handler=handler)
        return sp

    s = add("serve", cmd_serve, "Serve the API and the Navigator on loopback.")
    s.add_argument("--host", default=DEFAULT_HOST)
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--static-dir", type=Path, default=None, help="Navigator bundle (default: navigator/dist)")
    s.add_argument("--allow-remote", action="store_true", help="permit binding a non-loopback address")

    s = add("search", cmd_search, "Search the library.")
    s.add_argument("query", nargs="+")
    s.add_argument("--source", action="append", default=None, help="restrict to a source id (repeatable)")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--json", action="store_true")

    s = add("doc", cmd_doc, "Show a document by id (wsp:<source>/<native-id>).")
    s.add_argument("doc_id")
    s.add_argument("--at", default=None, help="generation id (snapshot) to read")
    s.add_argument("--json", action="store_true")

    s = add("sources", cmd_sources, "List sources and their current generation.")
    s.add_argument("--json", action="store_true")

    s = add("changes", cmd_changes, "Read the change feed.")
    s.add_argument("--since", default=None, help="cursor from a previous run")
    s.add_argument("--limit", type=int, default=100)
    s.add_argument("--follow", action="store_true", help="stream new changes (needs --url)")
    s.add_argument("--max-seconds", type=float, default=None, help="with --follow: stop after this long")
    s.add_argument("--json", action="store_true")

    reg = add("registry", _usage, "Source registry tools.")
    reg_sub = reg.add_subparsers(dest="registry_command", required=True, metavar="COMMAND")
    s = add("validate", cmd_registry_validate, "Validate registry/sources.yaml against its schema.", reg_sub)
    s.add_argument("--registry", type=Path, default=None)
    s.add_argument("--schema", type=Path, default=None)

    cfg = add("config", _usage, "Configuration tools.")
    cfg_sub = cfg.add_subparsers(dest="config_command", required=True, metavar="COMMAND")
    add("show", cmd_config_show, "Print the merged configuration with secrets redacted.", cfg_sub)
    return p


def _usage(_: argparse.Namespace, __: TextIO) -> int:  # pragma: no cover - argparse requires a subcommand
    return 2


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: Handler = args.handler
    try:
        return handler(args, out or sys.stdout)
    except WspApiError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except httpx.TransportError as exc:
        print(f"error: cannot reach the API ({type(exc).__name__}); is `wsp serve` running?", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
