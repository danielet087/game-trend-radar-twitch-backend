"""Authoritative follower cache, with legacy runner-local persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from uuid import uuid4
from radar_core.storage import primary
from radar_core.storage.sql import StorageError

SOURCE = "danielet087/game-trend-radar-twitch-backend@main"
DATABASE_PATH = "data/twitch_followers_cache.json"


def _database_load(json_module):
    head, files = primary.read_files(SOURCE, [DATABASE_PATH])
    if DATABASE_PATH not in files:
        # Absence in a successfully read, activated snapshot is explicit first use.
        return head, {"schema_version": 1, "followers": {}}
    try:
        payload = json_module.loads(files[DATABASE_PATH])
        if (not isinstance(payload, dict) or payload.get("schema_version") != 1
                or not isinstance(payload.get("followers"), dict)):
            raise ValueError
        return head, payload
    except (ValueError, TypeError, UnicodeError):
        raise StorageError("INVALID_FOLLOWER_CACHE", "The primary follower cache is invalid.") from None


def _fresh(row, now):
    if (not isinstance(row, dict) or type(row.get("total")) is not int or row["total"] < 0
            or not isinstance(row.get("observed_at"), str)):
        return None
    try:
        observed = datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))
        if observed.tzinfo is None or not timedelta(0) <= now - observed < timedelta(hours=24):
            return None
        return observed
    except (ValueError, TypeError):
        return None


def _database_save(rows, json_module):
    operation = "twitch-followers:" + uuid4().hex
    frozen = json_module.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    payload_revision = hashlib.sha256(frozen).hexdigest()
    for _ in range(3):
        head, existing = _database_load(json_module)
        now = datetime.now(timezone.utc)
        merged = {key: row for key, row in existing["followers"].items() if _fresh(row, now) is not None}
        for key, row in rows.items():
            observed = _fresh(row, now)
            if observed is not None and (key not in merged or observed > _fresh(merged[key], now)):
                merged[key] = row
        raw = json_module.dumps({"schema_version": 1, "followers": merged}, sort_keys=True,
                                separators=(",", ":"), allow_nan=False).encode() + b"\n"
        try:
            primary.write_files(SOURCE, {DATABASE_PATH: raw}, base_generation=head["generation"],
                                operation_id=operation, payload_revision=payload_revision, projection_required=False)
            return True
        except StorageError as error:
            if error.code != "STALE_GENERATION":
                raise
    raise StorageError("STALE_GENERATION", "Follower cache conflicts exceeded the bounded retry limit.")


def load(cache_path, *, json_module):
    if primary.enabled():
        return _database_load(json_module)[1]
    payload = json_module.loads(cache_path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("followers"), dict)
    ):
        raise ValueError("Invalid follower cache")
    return payload


def save(cache_path, rows, *, path_type, temporary_file, replace, json_module, warning) -> bool:
    """Atomically persist fresh successes even when the lookup budget ended."""
    if primary.enabled():
        return _database_save(rows, json_module)
    temporary = None
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with temporary_file(
            "w",
            encoding="utf-8",
            dir=cache_path.parent,
            prefix=".followers-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = path_type(handle.name)
            json_module.dump(
                {"schema_version": 1, "followers": rows},
                handle,
                separators=(",", ":"),
                sort_keys=True,
            )
            handle.write("\n")
        replace(temporary, cache_path)
        return True
    except OSError:
        warning("Follower cache could not be saved; base metrics remain publishable")
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
