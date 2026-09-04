"""Declarative provider discovery without importing or configuring clients."""
from dataclasses import asdict, dataclass
from typing import Callable


@dataclass(frozen=True)
class ProviderProfile:
    key: str
    display_name: str
    capabilities: tuple[str, ...]
    credential_requirements: tuple[str, ...]
    media_kinds: tuple[str, ...]
    artifact_kinds: tuple[str, ...]
    user_agent: str
    configured: Callable[[object], bool]

    def describe(self, config):
        value = asdict(self)
        value.pop("configured")
        value["configured"] = bool(self.configured(config))
        return value


_REGISTRY = {}


def register(profile):
    """Register a Python adapter profile; implementation details stay elsewhere."""
    _REGISTRY[profile.key] = profile


def profiles(config):
    return [_REGISTRY[key].describe(config) for key in sorted(_REGISTRY)]


register(ProviderProfile(
    "tmdb", "The Movie Database",
    ("movie.identity", "movie.metadata", "movie.poster", "movie.credits", "person.image"),
    ("TMDB_API_KEY or TMDB_BEARER_TOKEN",), ("movie", "person"),
    ("metadata", "poster", "credits", "image"), "harvester/1",
    lambda config: bool(config.tmdb_api_key or config.tmdb_bearer_token),
))
register(ProviderProfile(
    "tvdb", "TheTVDB",
    ("tv.identity", "tv.metadata", "tv.poster", "tv.credits", "person.image"),
    ("TVDB_API_KEY",), ("show", "person"),
    ("metadata", "poster", "credits", "image"),
    "local-tv-tvdb-url-scanner/1.0",
    lambda config: bool(config.tvdb_api_key),
))
