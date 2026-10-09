"""Runner-local follower cache reads and atomic replacement."""

from __future__ import annotations


def load(cache_path, *, json_module):
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
