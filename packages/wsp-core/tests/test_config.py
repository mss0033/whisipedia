from pathlib import Path

from wsp_core.config import load_config


def test_layers_merge_in_order(tmp_path: Path) -> None:
    (tmp_path / "default.toml").write_text('[vault]\nroot = "/a"\nscrub_cycle_days = 30\n')
    (tmp_path / "local.toml").write_text('[vault]\nroot = "/b"\n')
    cfg = load_config(tmp_path, environ={"WSP__VAULT__SCRUB_CYCLE_DAYS": "7", "OTHER": "x"})
    assert cfg["vault"] == {"root": "/b", "scrub_cycle_days": 7}


def test_env_strings_fall_back_to_raw(tmp_path: Path) -> None:
    cfg = load_config(tmp_path, environ={"WSP__VAULT__ROOT": "/mnt/z/whisipedia"})
    assert cfg["vault"]["root"] == "/mnt/z/whisipedia"


def test_no_local_env_skips_local_file(tmp_path: Path) -> None:
    (tmp_path / "default.toml").write_text('[vault]\nroot = "/a"\n')
    (tmp_path / "local.toml").write_text('[vault]\nroot = "/real"\n')
    cfg = load_config(tmp_path, environ={"WSP_NO_LOCAL_CONFIG": "1"})
    assert cfg["vault"]["root"] == "/a"
