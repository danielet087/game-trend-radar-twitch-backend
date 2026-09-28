from __future__ import annotations

import argparse
import logging
import os

from collectors.twitch_live import write_json
from collectors.twitch_candidates import REGISTRY_PATH, collect_candidates as collect_twitch


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Twitch categories with at least 7,000 viewers and queue NEW-badge verification.")
    parser.add_argument("--output", default="output/twitch_live.json")
    parser.add_argument("--min-viewers", type=int, default=7000)
    parser.add_argument("--max-category-pages", type=int, default=5)
    parser.add_argument("--max-stream-pages", type=int, default=150)
    parser.add_argument("--max-api-calls", type=int, default=1200)
    parser.add_argument("--verification-registry", default=str(REGISTRY_PATH))
    parser.add_argument("--no-release-hints", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise SystemExit("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are required")

    payload = collect_twitch(
        client_id=client_id,
        client_secret=client_secret,
        min_viewers=args.min_viewers,
        max_category_pages=args.max_category_pages,
        max_stream_pages=args.max_stream_pages,
        max_api_calls=args.max_api_calls,
        registry_path=args.verification_registry,
        include_release_hints=not args.no_release_hints,
    )
    path = write_json(payload, args.output)
    print(
        f"Twitch collection complete: "
        f"{len(payload['candidate_games'])} qualifying categories, "
        f"{len(payload['top_games'])} verified NEW, "
        f"{len(payload['pending_verification'])} pending -> {path}"
    )
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(
                "### Twitch 候選掃描\n\n"
                f"- 門檻：{args.min_viewers:,} 人\n"
                f"- 達標候選：{len(payload['candidate_games'])} 款\n"
                f"- 已確認全新：{len(payload['top_games'])} 款\n"
                f"- 待驗證：{len(payload['pending_verification'])} 款\n"
                f"- 暫時排除：{len(payload['excluded_games'])} 類\n"
                f"- Helix 呼叫（不含重試）：{payload['coverage']['helix_calls_excluding_retries']}\n"
                f"- 掃描停止原因：{payload['coverage']['stop_reason']}\n\n"
                "每款候選都已翻完直播分頁，包含零觀眾台；中位數不是平均數。\n"
                "IGDB 日期只供待驗證排序；新標記仍以附時間的 Twitch 直接觀察為準。\n"
            )


if __name__ == "__main__":
    main()
