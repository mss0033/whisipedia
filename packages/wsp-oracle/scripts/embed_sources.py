"""Embed refined Parquet documents of selected sources into the Oracle's vector store.

    uv sync --all-packages --extra gpu
    uv run python packages/wsp-oracle/scripts/embed_sources.py --sources wikipedia-en-zim

Resumable (re-run the same command after an interruption), incremental (a new Refinery generation
only re-embeds changed documents) and verbose (progress line every 10 s with throughput and ETA).
See ``wsp_oracle.embedbuild`` for the options; ``--help`` lists them.
"""

from wsp_oracle.embedbuild import main

if __name__ == "__main__":
    raise SystemExit(main())
