"""Local configuration: ``~/.fvs/config.toml`` and ``~/.fvs/token`` (DS 6.2, 10.1, 10.4).

The config file holds the folders, the port and the ElevenLabs API key, and is readable
only by the current user (NFR-S2). Everything else lives in the ``settings`` table.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import tomllib
from pathlib import Path

import tomli_w
from pydantic import BaseModel, Field, SecretStr

from fvs.atomic import write_bytes_atomic, write_text_atomic

DEFAULT_PORT = 8787
HOST = "127.0.0.1"  # never anything else (NFR-S1)


def fvs_home() -> Path:
    """``~/.fvs``, or ``$FVS_HOME`` when set (used by tests)."""
    env = os.environ.get("FVS_HOME")
    return Path(env).expanduser() if env else Path.home() / ".fvs"


def default_root() -> Path:
    """``~/FVS``, or ``$FVS_ROOT`` when set."""
    env = os.environ.get("FVS_ROOT")
    return Path(env).expanduser() if env else Path.home() / "FVS"


class Folders(BaseModel):
    input: Path = Field(default_factory=lambda: default_root() / "input")
    workspace: Path = Field(default_factory=lambda: default_root() / "workspace")
    output: Path = Field(default_factory=lambda: default_root() / "output")
    # Example videos the creator likes (INS-1): read only, never footage.
    inspiration: Path = Field(default_factory=lambda: default_root() / "inspiration")

    @property
    def assets(self) -> Path:
        return self.workspace / "assets"

    def expanded(self) -> Folders:
        return Folders(
            input=self.input.expanduser(),
            workspace=self.workspace.expanduser(),
            output=self.output.expanduser(),
            inspiration=self.inspiration.expanduser(),
        )


class Config(BaseModel):
    port: int = DEFAULT_PORT
    folders: Folders = Field(default_factory=Folders)
    elevenlabs_api_key: SecretStr | None = None

    @property
    def api_key_last4(self) -> str | None:
        if self.elevenlabs_api_key is None:
            return None
        return self.elevenlabs_api_key.get_secret_value()[-4:]


def config_path(home: Path | None = None) -> Path:
    return (home or fvs_home()) / "config.toml"


def token_path(home: Path | None = None) -> Path:
    return (home or fvs_home()) / "token"


def restrict_to_user(path: Path) -> bool:
    """Make ``path`` readable and writable by the current user only. Returns success."""
    if sys.platform == "win32":
        user = os.environ.get("USERNAME")
        if not user:
            return False
        domain = os.environ.get("USERDOMAIN")
        principal = f"{domain}\\{user}" if domain else user
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:(F)"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0
    os.chmod(path, 0o600)
    return True


ENV_KEY = "ELEVENLABS_API_KEY"


def key_from_env(env_file: Path) -> str | None:
    """``ELEVENLABS_API_KEY`` from the environment, else from a ``.env`` file (where
    ``npx @elevenlabs/cli onboard`` saves it). For ``fvs key --from-env`` only: the core
    itself reads the key from config.toml, wherever it is started."""
    value = os.environ.get(ENV_KEY, "").strip()
    if not value and env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            name, sep, rest = line.strip().removeprefix("export ").partition("=")
            if sep and name.strip() == ENV_KEY:
                value = rest.split(" #")[0].strip().strip("'\"")
    return value or None


def load_config(home: Path | None = None) -> Config:
    path = config_path(home)
    if not path.exists():
        return Config()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    key = data.get("elevenlabs", {}).get("api_key")
    return Config(
        port=data.get("port", DEFAULT_PORT),
        folders=Folders(**data.get("folders", {})),
        elevenlabs_api_key=SecretStr(key) if key else None,
    )


def save_config(config: Config, home: Path | None = None) -> Path:
    path = config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {
        "port": config.port,
        "folders": {
            "input": str(config.folders.input),
            "workspace": str(config.folders.workspace),
            "output": str(config.folders.output),
            "inspiration": str(config.folders.inspiration),
        },
    }
    if config.elevenlabs_api_key is not None:
        data["elevenlabs"] = {"api_key": config.elevenlabs_api_key.get_secret_value()}
    write_bytes_atomic(path, tomli_w.dumps(data).encode("utf-8"))
    restrict_to_user(path)
    return path


def ensure_config(home: Path | None = None) -> Config:
    """Load the config, writing the defaults on first start."""
    if not config_path(home).exists():
        save_config(Config(), home)
    return load_config(home)


def ensure_token(home: Path | None = None) -> str:
    """Return the local session token, creating a random one on first start."""
    path = token_path(home)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    write_text_atomic(path, token)
    restrict_to_user(path)
    return token
