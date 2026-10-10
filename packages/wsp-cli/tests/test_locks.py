import subprocess
import sys
from pathlib import Path

import pytest

from wsp_cli.locks import LockHeldError, exclusive


def test_second_holder_is_refused_and_lock_frees_on_exit(tmp_path: Path) -> None:
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from wsp_cli.locks import exclusive\n"
        "with exclusive(Path(sys.argv[1]), 'ingest'):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(30)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "held"
        with pytest.raises(LockHeldError, match=f"pid {proc.pid}"), exclusive(tmp_path, "ingest"):
            pass
    finally:
        proc.kill()
        proc.wait()
    with exclusive(tmp_path, "ingest") as path:
        assert path.read_text() != ""


def test_reentry_after_release(tmp_path: Path) -> None:
    with exclusive(tmp_path, "index"):
        pass
    with exclusive(tmp_path, "index"):
        pass
