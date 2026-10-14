from pathlib import Path

from wsp_core.memory import cgroup_memory, drop_file_cache, memory_line, own_cgroup


def _fake_cgroup(tmp_path: Path) -> Path:
    here = tmp_path / "sys" / "user.slice" / "job.service"
    here.mkdir(parents=True)
    (here / "memory.stat").write_text("anon 1048576\nfile 3145728\nfile_dirty 0\nslab 99\nbogus x\n")
    (here / "memory.current").write_text("4194304\n")
    (here / "memory.swap.current").write_text("2097152\n")
    return here


def test_own_cgroup_resolves_the_proc_entry(tmp_path: Path) -> None:
    proc = tmp_path / "cgroup"
    proc.write_text("0::/user.slice/job.service\n")
    assert own_cgroup(tmp_path / "sys", proc) == tmp_path / "sys" / "user.slice" / "job.service"
    assert own_cgroup(tmp_path / "sys", tmp_path / "missing") is None


def test_cgroup_memory_reads_heap_cache_and_swap(tmp_path: Path) -> None:
    here = _fake_cgroup(tmp_path)
    assert cgroup_memory(here) == {
        "anon": 1048576,
        "file": 3145728,
        "file_dirty": 0,
        "current": 4194304,
        "swap": 2097152,
    }
    assert cgroup_memory(tmp_path / "nowhere") == {}


def test_memory_line_shows_cache_apart_from_heap(tmp_path: Path) -> None:
    line = memory_line(_fake_cgroup(tmp_path))
    assert line.startswith("mem: peak rss ")
    assert "cgroup anon 1 file 3 file_dirty 0 current 4 swap 2 MiB" in line
    assert "cgroup" not in memory_line(tmp_path / "nowhere")


def test_drop_file_cache_is_best_effort(tmp_path: Path) -> None:
    path = tmp_path / "part.parquet"
    path.write_bytes(b"x" * 8192)
    drop_file_cache(path)  # nothing to assert about the cache; it must not raise
    drop_file_cache(tmp_path / "missing")
    assert path.read_bytes() == b"x" * 8192
