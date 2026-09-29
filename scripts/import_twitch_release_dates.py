"""Import only game-date fields from a saved JSON response; no web requests."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from collectors.twitch_live import write_json
from collectors.twitch_newness import RELEASE_DATES_PATH, load_release_dates, parse_timestamp, timestamp


def extract_records(payload: object, *, source_name: str, source_url: str, observed_at: str | None = None) -> dict:
    documents = payload if isinstance(payload, list) else [payload]
    rows = []
    for document in documents:
        if not isinstance(document, dict):
            raise ValueError("Expected a JSON response body or category-export array")
        if document.get("errors"):
            raise ValueError("Response contains source errors; refusing a partial export")
        if "data" in document:
            data = document["data"]
            if not isinstance(data, dict):
                raise ValueError("Invalid response data")
            if isinstance(data.get("directoriesWithTags"), dict):
                rows.extend(edge["node"] for edge in data["directoriesWithTags"].get("edges", []))
            elif isinstance(data.get("game"), dict):
                rows.append(data["game"])
            else:
                raise ValueError("Response has no supported game/category data")
        elif "categoryId" in document:
            rows.append(document)
        else:
            raise ValueError("Unsupported export; use response JSON, not a HAR, headers or IGDB data")
    records = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid category record")
        if not row.get("originalReleaseDate"):
            continue  # Missing is unknown; never manufacture a release date.
        game_id = str(row.get("categoryId") or row.get("id") or "")
        if not game_id.isdigit():
            raise ValueError("Missing Twitch category ID")
        captured = row.get("scrapedAt") or observed_at
        if not captured:
            raise ValueError("Original observation time is required; do not use import time")
        record = {
            "original_release_date": timestamp(parse_timestamp(row["originalReleaseDate"])),
            "observed_at": timestamp(parse_timestamp(captured)), "source_field": "originalReleaseDate",
            "source_name": source_name, "source_url": source_url,
        }
        previous = records.get(game_id)
        if previous and previous["observed_at"] == record["observed_at"] and previous["original_release_date"] != record["original_release_date"]:
            raise ValueError("Conflicting dates at the same observation time")
        if previous is None or record["observed_at"] > previous["observed_at"]:
            records[game_id] = record
    if not records:
        raise ValueError("No originalReleaseDate values found; nothing imported")
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Saved response-body JSON or category dataset; no credentials")
    parser.add_argument("--source-name", required=True)
    parser.add_argument("--source-url", required=True, help="Public HTTPS source URL without query parameters")
    parser.add_argument("--observed-at", help="Original capture time, including timezone; required without scrapedAt")
    parser.add_argument("--output", default=str(RELEASE_DATES_PATH))
    args = parser.parse_args()
    records = extract_records(json.loads(Path(args.input).read_text(encoding="utf-8")),
                              source_name=args.source_name, source_url=args.source_url, observed_at=args.observed_at)
    destination = Path(args.output)
    existing = load_release_dates(destination) if destination.exists() else {}
    updated = 0
    for game_id, row in records.items():
        old = existing.get(game_id)
        if old is None or row["observed_at"] > old["observed_at"]:
            existing[game_id] = row
            updated += 1
        elif row["observed_at"] == old["observed_at"] and row["original_release_date"] != old["original_release_date"]:
            raise ValueError("Conflicting dates at the same observation time")
    # Validate a temporary file before replacing the accepted metadata registry.
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".import.tmp")
    try:
        write_json({"schema_version": 1, "records": existing}, temporary)
        load_release_dates(temporary)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Imported {updated} newer Twitch release-date records; total {len(existing)}")


if __name__ == "__main__":
    main()
