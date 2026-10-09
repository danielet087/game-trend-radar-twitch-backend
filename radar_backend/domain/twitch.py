"""Pure Twitch identities and collection deadline error."""
from dataclasses import dataclass


@dataclass(frozen=True)
class TwitchGame:
    id: str
    name: str
    box_art_url: str | None
    igdb_id: str | None



class CollectionDeadlineExceeded(RuntimeError):
    """No new request or retry may start after the collection soft deadline."""

