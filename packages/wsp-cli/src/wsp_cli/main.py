"""``wsp``: the Whisipedia command line.

Read commands (``search``, ``doc``, ``sources``, ``changes``) go through the public SDK. With
``--url`` (or ``WSP_API_URL``) they talk to a running server; otherwise they run the API in-process on
the backends from :mod:`wsp_cli.bootstrap`, without opening a socket.

Operator commands (``ingest``, ``index build``, ``scrub``, ``drill``, ``approve``/``reject``/``resume``,
``status``) act on the real library configured in ``config/`` (see :mod:`wsp_cli.ops`).

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
from wsp_cli import bootstrap, doccollections, multilingual, ops
from wsp_core.registry import RegistryError, load_registry
from wsp_sdk import WspApiError, WspClient
from wsp_vault.drill import DrillKind

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
    _note_backend(cfg)
    app = create_app(bootstrap.build_services(cfg, lay), bootstrap.api_settings(cfg, lay))
    return WspClient(http=httpx.Client(transport=InProcessTransport(app), base_url=f"http://{DEFAULT_HOST}"))


def _note_backend(cfg: dict[str, object]) -> None:
    note = bootstrap.backend_note(cfg)
    if note:
        print(f"note: {note}", file=sys.stderr)


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
    _note_backend(cfg)
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

    s = add(
        "ingest",
        ops.cmd_ingest,
        "Adopt completed bulk downloads from <vault>/_incoming into the vault "
        "(exit 0 done, 1 a version failed, 3 another ingest runs, 4 files still missing or downloading).",
    )
    s.add_argument("--source", default=None, help="only this source id")
    s.add_argument("--dry-run", action="store_true", help="report what would happen; change nothing")
    s.add_argument("--offline", action="store_true", help="do not fetch Kiwix .sha256 files")
    s.add_argument(
        "--allow-mirror", action="store_true", help="with --source gutenberg: ingest the rsync tree"
    )
    s.add_argument("--settle-seconds", type=float, default=120.0, help="ignore files modified more recently")
    s.add_argument("--json", action="store_true")

    ix = add("index", _usage, "Search index tools.")
    ix_sub = ix.add_subparsers(dest="index_command", required=True, metavar="COMMAND")
    s = add(
        "build",
        ops.cmd_index_build,
        "Refine sealed generations to Parquet and index them (local disk).",
        ix_sub,
    )
    s.add_argument("--source", default=None, help="only this source id")
    s.add_argument("--no-rank", action="store_true", help="skip the static rank (PageRank) step")
    s = add(
        "rank",
        ops.cmd_index_rank,
        "Recompute the link-analysis static rank (PageRank), suggest priors and featured articles.",
        ix_sub,
    )
    s.add_argument("--damping", type=float, default=0.85, help="PageRank damping factor")
    s.add_argument("--mix", type=float, default=0.5, help="weight of the cross-source rank (0-1)")
    s = add(
        "spell",
        ops.cmd_index_spell,
        'Rebuild the "did you mean" vocabulary from the indexed text (local disk).',
        ix_sub,
    )
    s = add(
        "catalog",
        ops.cmd_index_catalog,
        "Rebuild the portal/directory/random catalog from the Refinery (local disk).",
        ix_sub,
    )
    s.add_argument("--source", default=None, help="only this source id")

    s = add(
        "scrub", ops.cmd_scrub, "Re-hash sealed files that are due and run the repair ladder on findings."
    )
    s.add_argument("--budget-gb", type=float, default=None, help="stop after hashing this many GB")

    s = add("drill", ops.cmd_drill, "Break a sandboxed copy of a sealed generation; expect detect + repair.")
    s.add_argument("--kind", choices=[k.value for k in DrillKind], default=None)
    s.add_argument("--source", default=None, help="drill this source (default: smallest generation)")

    for name, help_ in (
        ("approve", "Continue a release held by the integrity or sanity checks."),
        ("reject", "Reject a held release (it is quarantined)."),
        ("resume", "Resume an auto-paused source."),
    ):
        s = add(name, ops.cmd_owner, help_)
        s.set_defaults(owner_action=name)
        s.add_argument("source")
        if name != "resume":
            s.add_argument("version", nargs="?", default=None, help="default: the release held for review")
            s.add_argument("--note", default=None, help="reason recorded in the ledger")

    add("status", ops.cmd_status, "Sources, generations, degraded data and the orchestrator queue.")

    pr = add("proposals", _usage, "Curator proposals: list, show, accept or reject.")
    pr_sub = pr.add_subparsers(dest="proposals_command", required=True, metavar="COMMAND")
    s = add("list", ops.cmd_proposals_list, "List proposals, newest first.", pr_sub)
    s.add_argument("--status", choices=["open", "applied", "accepted", "rejected"], default=None)
    s.add_argument("--json", action="store_true")
    s = add("show", ops.cmd_proposals_show, "Print one proposal file.", pr_sub)
    s.add_argument("id", type=int)
    for name, help_ in (
        ("accept", "Write the drafted entry into registry/sources.yaml (working tree) for you to commit."),
        ("reject", "Reject a proposal; the candidate is never proposed again."),
    ):
        s = add(name, ops.cmd_proposals_decide, help_, pr_sub)
        s.set_defaults(decision=name)
        s.add_argument("id", type=int)
        s.add_argument("--note", default=None)

    doccollections.register(sub, add)
    multilingual.register(sub, add)

    pa = add("parity", _usage, "par2 parity (Warden).")
    pa_sub = pa.add_subparsers(dest="parity_command", required=True, metavar="COMMAND")
    add("status", ops.cmd_parity_status, "Parity coverage of sealed generations.", pa_sub)

    sv = add("service", _usage, "systemd user units: install, status, uninstall.")
    sv_sub = sv.add_subparsers(dest="service_command", required=True, metavar="COMMAND")
    for name, help_ in (
        ("install", "Render deploy/systemd units into ~/.config/systemd/user and enable them."),
        ("status", "Show whether the units are installed, enabled and active."),
        ("uninstall", "Disable and remove the units this command installed."),
    ):
        s = add(name, ops.cmd_service, help_, sv_sub)
        s.add_argument("--unit-dir", default=None, help="default: $XDG_CONFIG_HOME/systemd/user")
        if name != "status":
            s.add_argument("--dry-run", action="store_true")
        if name == "install":
            s.add_argument("--now", action="store_true", help="also start the services")
            s.add_argument("--no-enable", action="store_true", help="only write the unit files")

    s = add("doctor", ops.cmd_doctor, "Check config, vault, index, disk, tools, services, key and ledger.")
    s.add_argument("--quiet", action="store_true", help="print only problems (to stderr)")
    s.add_argument("--require", action="append", default=None, help="only these checks decide the exit code")

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
    except bootstrap.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
