import os
from pathlib import Path


def load_env(path: str = ".env") -> None:
    """
    Minimal .env loader (KEY=VALUE per line, # comments allowed).
    Values are only set if the key is not already in the environment.
    """
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def require_env(name: str) -> str:
    """
    Fetch a required environment variable or raise a clear error.
    """
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable '{name}' is required but not set")
    return value
