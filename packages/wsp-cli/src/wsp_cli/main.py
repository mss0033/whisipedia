"""``wsp``: the Whisipedia command line.

Read commands (``search``, ``doc``, ``sources``, ``changes``) go through the public SDK. With
``--url`` (or ``WSP_API_URL``) they talk to a running server; otherwise they run the API in-process on
the backends from :mod:`wsp_cli.bootstrap`, without opening a socket.

Operator commands (``ingest``, ``index build``, ``scrub``, ``drill``, ``approve``/``reject``/``resume``,
``status``) act on the real library configured in ``config/`` (see :mod:`wsp_cli.ops`).

argparse rather than Typer/Click: the surface is small, it keeps the dependency tree (and the offline
audit) minimal, and it type-checks cleanly under pyright strict.

Imports are lazy (audit 2026-10-11 #11): ``wsp search --url ...`` must not load the API app, the
index or the operator commands. Operator commands import :mod:`wsp_cli.ops` when they run, and the
``collections`` and ``plan`` command groups are registered only when asked for (or for ``--help``).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from typing import Protocol, TextIO, cast

import httpx
from pydantic import BaseModel

from wsp_cli import bootstrap
from wsp_sdk import WspApiError, WspClient

DEFAULT_HOST = "127.0.0.1"  # = wsp_api.services.DEFAULT_HOST
DEFAULT_PORT = 8737  # = wsp_api.services.DEFAULT_PORT
LAZY_GROUPS = {"collections": "wsp_cli.doccollections", "plan": "wsp_cli.multilingual"}
_TAKES_VALUE = frozenset({"--config-dir", "--url"})

Handler = Callable[[argparse.Namespace, TextIO], int]


def _ops(name: str) -> Handler:
    """A handler from :mod:`wsp_cli.ops`, imported when the command runs (it loads the index stack)."""

    def run(args: argparse.Namespace, out: TextIO) -> int:
        handler = cast(Handler, getattr(import_module("wsp_cli.ops"), name))
        return handler(args, out)

    run.__name__ = name
    return run


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
    from wsp_api import create_app
    from wsp_api.inprocess import InProcessTransport

    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    _note_backend(cfg)
    # In-process: no background threads, no worker processes, no warm-up (only `serve` has those).
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
    import faulthandler
    import signal

    import uvicorn  # local import: only `serve` needs the ASGI server

    # `systemctl --user kill --kill-whom=main -s USR1 whisipedia-api` prints every thread's stack to the
    # journal: the only way to see a stalled server's Python stacks (ptrace is not allowed for the
    # user's processes). `--kill-whom=main` matters: the default signals every process of the unit, and
    # a ZIM worker (no SIGUSR1 handler) would die of it, failing its in-flight requests (audit
    # 2026-10-12, L2).
    faulthandler.register(signal.SIGUSR1, all_threads=True)

    from wsp_api import create_app

    lay = bootstrap.layout(args.config_dir)
    cfg = bootstrap.load(lay)
    settings = bootstrap.serve_settings(cfg, lay, static_dir=args.static_dir)
    if not _is_loopback(host):
        settings = replace(settings, allowed_hosts=(*settings.allowed_hosts, host))
    _note_backend(cfg)
    app = create_app(bootstrap.build_services(cfg, lay), settings)
    out.write(f"Whisipedia API on http://{host}:{args.port}/ (static: {settings.static_dir})\n")
    # No access log: a page view with images is hundreds of lines (audit #21). Failed and slow requests
    # are logged by wsp_api.serving.SlowRequestLog.
    uvicorn.run(
        app,
        host=host,
        port=args.port,
        server_header=False,
        proxy_headers=False,
        log_level="info",
        access_log=False,
    )
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
        doc = wsp.document(args.doc_id, at=args.at, fields=["text"])
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
    from wsp_core.registry import RegistryError, load_registry

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


def command_of(argv: Sequence[str]) -> str | None:
    """The command word of ``argv`` (after global options), or None."""
    skip = False
    for token in argv:
        if skip:
            skip = False
            continue
        if token in _TAKES_VALUE:
            skip = True
            continue
        if token.startswith("-"):
            continue
        return token
    return None


def build_parser(argv: Sequence[str] | None = None) -> argparse.ArgumentParser:
    """The ``wsp`` parser. With ``argv``, modules that only some commands need are imported only when
    ``argv`` names one of those commands (or asks for help, or names no known command)."""
    command = None if argv is None else command_of(argv)
    wants_help = argv is None or any(t in ("-h", "--help") for t in argv[:1]) or command is None
    p = argparse.ArgumentParser(prog="wsp", description="Whisipedia: a private offline library.")
    p.add_argument("--config-dir", type=Path, default=None, help="default: $WSP_CONFIG_DIR or <repo>/config")
    p.add_argument("--url", default=None, help="API base URL (default: $WSP_API_URL, else run in-process)")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    builtin: set[str] = set()

    def add(name: str, handler: Handler, help_: str, parent: _Subparsers = sub) -> argparse.ArgumentParser:
        sp = parent.add_parser(name, help=help_, description=help_)
        sp.set_defaults(handler=handler)
        if parent is sub:
            builtin.add(name)
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
        _ops("cmd_ingest"),
        "Adopt completed bulk downloads from <vault>/_incoming into the vault. Exit codes, for every "
        "planned version (with --version: for that version only): 0 ingested or already sealed, "
        "1 a version failed, 2 usage, 3 another ingest runs, 4 files still missing or downloading, "
        "8 (--version only) no download plan names that version.",
    )
    s.add_argument("--source", default=None, help="only this source id")
    s.add_argument(
        "--version",
        default=None,
        help="with --source: only this incoming version (the cycle label given to `wsp collections "
        "fetch --version`); the exit code then reports on it alone",
    )
    s.add_argument(
        "--partition",
        default=None,
        help="with --version, for a partitioned source: the version <cycle>.<partition> (as fetched)",
    )
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
        _ops("cmd_index_build"),
        "Refine sealed generations to Parquet and index them (local disk).",
        ix_sub,
    )
    s.add_argument("--source", default=None, help="only this source id")
    s.add_argument("--no-rank", action="store_true", help="skip the static rank (PageRank) step")
    s.add_argument(
        "--no-spell",
        action="store_true",
        help="skip the spelling vocabulary (run `wsp index spell` after a batch)",
    )
    s = add(
        "rank",
        _ops("cmd_index_rank"),
        "Recompute the link-analysis static rank (PageRank), suggest priors and featured articles.",
        ix_sub,
    )
    s.add_argument("--damping", type=float, default=0.85, help="PageRank damping factor")
    s.add_argument("--mix", type=float, default=0.5, help="weight of the cross-source rank (0-1)")
    s.add_argument(
        "--reuse",
        action="store_true",
        help="keep the existing static rank file; only redo suggest priors and featured articles",
    )
    s = add(
        "spell",
        _ops("cmd_index_spell"),
        'Rebuild the "did you mean" vocabulary from the indexed text (local disk).',
        ix_sub,
    )
    s = add(
        "archive-changes",
        _ops("cmd_index_archive_changes"),
        "Move old change-feed ranges to the sealed archive ([index] changes_archive; may be the NAS).",
        ix_sub,
    )
    s.add_argument("--compact", action="store_true", help="then VACUUM the database to give the space back")
    s = add(
        "catalog",
        _ops("cmd_index_catalog"),
        "Rebuild the portal/directory/random catalog from the Refinery (local disk).",
        ix_sub,
    )
    s.add_argument("--source", default=None, help="only this source id")

    s = add(
        "scrub", _ops("cmd_scrub"), "Re-hash sealed files that are due and run the repair ladder on findings."
    )
    s.add_argument("--budget-gb", type=float, default=None, help="stop after hashing this many GB")

    s = add(
        "undegrade",
        _ops("cmd_undegrade"),
        "Fully re-verify a DEGRADED generation and clear the flag only if every file is intact.",
    )
    s.add_argument("source")
    s.add_argument("generation")

    led = add("ledger", _usage, "Ledger maintenance.")
    led_sub = led.add_subparsers(dest="ledger_command", required=True, metavar="COMMAND")
    s = add(
        "recover",
        _ops("cmd_ledger_recover"),
        "Cut a torn ledger day back to its last valid line (the cut bytes are kept as .torn).",
        led_sub,
    )
    s.add_argument("--note", default=None, help="reason recorded in the ledger")

    s = add(
        "drill", _ops("cmd_drill"), "Break a sandboxed copy of a sealed generation; expect detect + repair."
    )
    s.add_argument(
        "--kind", choices=_drill_kinds() if wants_help or command == "drill" else None, default=None
    )
    s.add_argument("--source", default=None, help="drill this source (default: smallest generation)")

    for name, help_ in (
        ("approve", "Continue a release held by the integrity or sanity checks."),
        ("reject", "Reject a held release (it is quarantined)."),
        ("resume", "Resume an auto-paused source."),
        ("retry", "Give a quarantined or backing-off release a fresh start (no attempts counted)."),
    ):
        s = add(name, _ops("cmd_owner"), help_)
        s.set_defaults(owner_action=name)
        s.add_argument("source")
        if name != "resume":
            s.add_argument("version", nargs="?", default=None, help="default: the release held for review")
            s.add_argument("--note", default=None, help="reason recorded in the ledger")
        if name == "approve":
            s.add_argument(
                "--force",
                action="store_true",
                help="promote even though a newer generation is current (moves CURRENT backwards)",
            )

    add("status", _ops("cmd_status"), "Sources, generations, degraded data and the orchestrator queue.")

    pr = add("proposals", _usage, "Curator proposals: list, show, accept or reject.")
    pr_sub = pr.add_subparsers(dest="proposals_command", required=True, metavar="COMMAND")
    s = add("list", _ops("cmd_proposals_list"), "List proposals, newest first.", pr_sub)
    s.add_argument("--status", choices=["open", "applied", "accepted", "rejected"], default=None)
    s.add_argument("--json", action="store_true")
    s = add("show", _ops("cmd_proposals_show"), "Print one proposal file.", pr_sub)
    s.add_argument("id", type=int)
    for name, help_ in (
        ("accept", "Write the drafted entry into registry/sources.yaml (working tree) for you to commit."),
        ("reject", "Reject a proposal; the candidate is never proposed again."),
    ):
        s = add(name, _ops("cmd_proposals_decide"), help_, pr_sub)
        s.set_defaults(decision=name)
        s.add_argument("id", type=int)
        s.add_argument("--note", default=None)
        if name == "accept":
            s.add_argument(
                "--only",
                action="append",
                default=None,
                metavar="KEY",
                help="document review: allow only these rows (document keys; repeatable); default every row",
            )
        else:
            s.add_argument(
                "--deny",
                action="append",
                default=None,
                metavar="KEY",
                help="document review: deny these rows (document keys; repeatable). Without it a "
                "rejection only closes the proposal and denies nothing",
            )

    for group, module in LAZY_GROUPS.items():
        if wants_help or command == group or command not in builtin:
            register = cast(Callable[[object, object], None], import_module(module).register)
            register(sub, add)

    pa = add("parity", _usage, "par2 parity (Warden).")
    pa_sub = pa.add_subparsers(dest="parity_command", required=True, metavar="COMMAND")
    add("status", _ops("cmd_parity_status"), "Parity coverage of sealed generations.", pa_sub)

    sv = add("service", _usage, "systemd user units: install, status, uninstall.")
    sv_sub = sv.add_subparsers(dest="service_command", required=True, metavar="COMMAND")
    for name, help_ in (
        ("install", "Render deploy/systemd units into ~/.config/systemd/user and enable them."),
        ("status", "Show whether the units are installed, enabled and active."),
        ("uninstall", "Disable and remove the units this command installed."),
    ):
        s = add(name, _ops("cmd_service"), help_, sv_sub)
        s.add_argument("--unit-dir", default=None, help="default: $XDG_CONFIG_HOME/systemd/user")
        if name != "status":
            s.add_argument("--dry-run", action="store_true")
        if name == "install":
            s.add_argument("--now", action="store_true", help="also start the services")
            s.add_argument("--no-enable", action="store_true", help="only write the unit files")

    s = add(
        "doctor", _ops("cmd_doctor"), "Check config, vault, index, disk, tools, services, key and ledger."
    )
    s.add_argument("--quiet", action="store_true", help="print only problems (to stderr)")
    s.add_argument("--require", action="append", default=None, help="only these checks decide the exit code")

    cfg = add("config", _usage, "Configuration tools.")
    cfg_sub = cfg.add_subparsers(dest="config_command", required=True, metavar="COMMAND")
    add("show", cmd_config_show, "Print the merged configuration with secrets redacted.", cfg_sub)
    return p


def _drill_kinds() -> list[str]:
    from wsp_vault.drill import DrillKind

    return [k.value for k in DrillKind]


def _usage(_: argparse.Namespace, __: TextIO) -> int:  # pragma: no cover - argparse requires a subcommand
    return 2


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser(raw).parse_args(raw)
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
