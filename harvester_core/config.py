"""Central configuration. Paths are anchored to the application, never cwd."""
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping

APP_DIR = Path(__file__).resolve().parent.parent
SECRET_NAMES = ("TMDB_API_KEY", "TMDB_BEARER_TOKEN", "TVDB_API_KEY", "TVDB_PIN")


def parse_key_file(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE credentials; malformed/comment lines are ignored."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    values = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key in SECRET_NAMES:
            values[key] = value
    return values


@dataclass(frozen=True)
class Config:
    app_dir: Path
    state_dir: Path
    movie_root: Path
    tv_root: Path
    tmdb_api_key: str | None = None
    tmdb_bearer_token: str | None = None
    tvdb_api_key: str | None = None
    tvdb_pin: str | None = None

    def state_path(self, name: str) -> Path:
        return self.state_dir / name


def load_config(overrides: Mapping[str, object] | None = None,
                environ: Mapping[str, str] | None = None,
                app_dir: Path | None = None) -> Config:
    """Load explicit overrides > environment > adjacent credential file > defaults."""
    app = Path(app_dir or APP_DIR).resolve()
    env = os.environ if environ is None else environ
    file_values = parse_key_file(app / "keys_and_tokens.txt")
    supplied = dict(overrides or {})

    def choose(field, env_name, default=None):
        value = supplied.get(field)
        if value is None:
            value = env.get(env_name, file_values.get(env_name, default))
        return value

    return Config(
        app_dir=app,
        state_dir=Path(choose("state_dir", "HARVESTER_STATE_DIR", app / "state")).expanduser().resolve(),
        movie_root=Path(choose("movie_root", "HARVESTER_MOVIE_ROOT", "/mnt/2tb/Movie")).expanduser().resolve(),
        tv_root=Path(choose("tv_root", "HARVESTER_TV_ROOT", "/mnt/2tb/TV")).expanduser().resolve(),
        tmdb_api_key=choose("tmdb_api_key", "TMDB_API_KEY"),
        tmdb_bearer_token=choose("tmdb_bearer_token", "TMDB_BEARER_TOKEN"),
        tvdb_api_key=choose("tvdb_api_key", "TVDB_API_KEY"),
        tvdb_pin=choose("tvdb_pin", "TVDB_PIN"),
    )
