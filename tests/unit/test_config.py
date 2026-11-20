"""Config file and session token (NFR-S1, NFR-S2)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from pydantic import SecretStr

from fvs.config import Config, config_path, ensure_config, ensure_token, load_config, save_config


def test_first_start_writes_defaults(home: Path) -> None:
    cfg = ensure_config(home)
    assert config_path(home).exists()
    assert cfg.port == 8787
    assert cfg.elevenlabs_api_key is None
    assert cfg.folders.input.name == "input"


def test_token_is_random_and_stable(home: Path) -> None:
    t1 = ensure_token(home)
    assert len(t1) >= 40
    assert ensure_token(home) == t1


def test_api_key_round_trip_and_masking(home: Path) -> None:
    key = "sk_test_0123456789abcd"
    save_config(Config(elevenlabs_api_key=SecretStr(key)), home)
    cfg = load_config(home)
    assert cfg.elevenlabs_api_key is not None
    assert cfg.elevenlabs_api_key.get_secret_value() == key
    assert cfg.api_key_last4 == "abcd"
    assert key not in repr(cfg) and key not in str(cfg)


def test_secret_files_are_user_only(home: Path) -> None:
    ensure_token(home)
    save_config(Config(), home)
    for path in (config_path(home), home / "token"):
        if sys.platform == "win32":
            acl = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
            assert "Everyone" not in acl
            assert "BUILTIN\\Users" not in acl
            assert "Authenticated Users" not in acl
        else:
            assert path.stat().st_mode & 0o077 == 0
