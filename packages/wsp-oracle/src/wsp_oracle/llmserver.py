"""Start the local LLM server for summaries: ``llama-server`` bound to loopback, model from local disk.

    uv run wsp-oracle-llm                 # uses [oracle.generator] from the layered config
    uv run wsp-oracle-llm --print         # show the command instead of running it

The prebuilt CUDA release of llama.cpp is installed user-local (see docs/components/oracle.md); its
shared libraries sit next to the binary, so ``LD_LIBRARY_PATH`` is set to that directory. The
process gets ``HF_HUB_OFFLINE=1`` and no model download flags: the GGUF must already be on disk.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from wsp_oracle.config import OracleConfig
from wsp_oracle.generator import require_loopback


def server_command(cfg: OracleConfig, *, parallel: int = 2) -> tuple[list[str], dict[str, str]]:
    """``(argv, env)`` for ``llama-server`` per ``[oracle.generator]``."""
    gen = cfg.generator
    url = urlparse(require_loopback(gen.url))
    binary = Path(gen.server_binary).expanduser()
    model = cfg.model_path(gen.model)
    argv = [
        str(binary),
        "--model",
        str(model),
        "--host",
        url.hostname or "127.0.0.1",
        "--port",
        str(url.port or 8738),
        "--ctx-size",
        str(gen.context * parallel),
        "--parallel",
        str(parallel),
        "--n-gpu-layers",
        str(gen.gpu_layers),
        "--flash-attn",
        "on",
        "--jinja",
        "--reasoning",
        "off",
        "--no-mmproj",
        "--no-webui",
        "--offline",
    ]
    env = dict(os.environ)
    lib = str(binary.parent)
    env["LD_LIBRARY_PATH"] = f"{lib}:{env['LD_LIBRARY_PATH']}" if env.get("LD_LIBRARY_PATH") else lib
    env["HF_HUB_OFFLINE"] = "1"
    return argv, env


def main(argv: Sequence[str] | None = None) -> int:
    from wsp_core.config import load_config

    ap = argparse.ArgumentParser(description="Run llama-server for Whisipedia summaries (loopback only).")
    ap.add_argument("--config-dir", type=Path, default=None, help="directory with default.toml/local.toml")
    ap.add_argument("--parallel", type=int, default=2, help="concurrent summaries (slots)")
    ap.add_argument("--print", action="store_true", help="print the command and exit")
    args = ap.parse_args(argv)
    raw: Mapping[str, Any] = load_config(args.config_dir or _config_dir())
    section = raw.get("oracle", {})
    cfg = OracleConfig.from_mapping(cast(Mapping[str, Any], section) if isinstance(section, Mapping) else {})
    cmd, env = server_command(cfg, parallel=args.parallel)
    if args.print:
        print(" ".join(cmd))
        return 0
    for path in (Path(cmd[0]), Path(cmd[2])):
        if not path.exists():
            print(f"missing: {path} (see docs/components/oracle.md, 'Generator')", file=sys.stderr)
            return 2
    os.execve(cmd[0], cmd, env)  # noqa: S606 - replaces this process with the server  # pragma: no cover


def _config_dir() -> Path:
    here = Path.cwd().resolve()
    for parent in (here, *here.parents):
        if (parent / "config" / "default.toml").is_file():
            return parent / "config"
    return here / "config"


if __name__ == "__main__":
    raise SystemExit(main())
