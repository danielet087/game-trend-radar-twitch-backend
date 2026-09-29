from __future__ import annotations

import argparse
import logging
import os

from collectors.twitch_live import write_json
from collectors.twitch_candidates import REGISTRY_PATH, collect_candidates as collect_twitch
from collectors.twitch_newness import RELEASE_DATES_PATH
from collectors.twitch_audience import CACHE_PATH
from scripts.collection_guard import validate_slot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Twitch categories with at least 7,000 viewers and queue NEW-badge verification.")
    parser.add_argument("--output", default="output/twitch_live.json")
    parser.add_argument("--min-viewers", type=int, default=7000)
    parser.add_argument("--max-category-pages", type=int, default=5)
    parser.add_argument("--max-stream-pages", type=int, default=150)
    parser.add_argument("--max-api-calls", type=int, default=1200)
    parser.add_argument("--max-collection-seconds", type=float, default=1500,
                        help="Shared census + metadata + follower soft deadline; reserves time for publishing")
    parser.add_argument("--verification-registry", default=str(REGISTRY_PATH))
    parser.add_argument("--release-dates", default=str(RELEASE_DATES_PATH), help="Dated Twitch originalReleaseDate export for the 14-day trial")
    parser.add_argument("--no-release-hints", action="store_true")
    parser.add_argument("--followers-cache", default=str(CACHE_PATH), help="Runner-local daily follower-total cache")
    parser.add_argument("--followers-max-calls", type=int, default=None,
                        help="Optional diagnostic follower request cap; default: no separate cap")
    parser.add_argument("--followers-max-seconds", type=float, default=None,
                        help="Optional diagnostic follower time cap; default: collection deadline only")
    parser.add_argument("--no-filtered-audience", action="store_true", help="Disable follower-qualified statistics (CLI default: enabled)")
    parser.add_argument("--target-slot", type=validate_slot, default=None,
                        help="Requested UTC hour; actual sampling timestamps remain separate")
    parser.add_argument("--trigger-source", choices=("manual", "schedule", "cloudflare"), default="manual")
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
        max_collection_seconds=args.max_collection_seconds,
        registry_path=args.verification_registry,
        release_dates_path=args.release_dates,
        include_release_hints=not args.no_release_hints,
        include_filtered_audience=not args.no_filtered_audience,
        followers_cache_path=args.followers_cache,
        followers_max_calls=args.followers_max_calls,
        followers_max_seconds=args.followers_max_seconds,
    )
    if args.target_slot:
        payload["collection_schedule"] = {
            "target_slot": args.target_slot,
            "trigger_source": args.trigger_source,
            "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", ""),
        }
    path = write_json(payload, args.output)
    experiment = payload["newness_experiment"]
    twitch_trial, igdb_trial = (experiment[key] for key in ("twitch_original_release_date", "igdb_first_release_date"))
    print(
        f"Twitch category census complete: "
        f"{len(payload['candidate_games'])} qualifying categories, "
        f"{len(payload['top_games'])} verified NEW, "
        f"{len(payload['pending_verification'])} pending -> {path}"
    )
    print(f"14-day trial: Twitch dates {twitch_trial['evaluated_candidates']} evaluated / "
          f"{twitch_trial['unknown_candidates']} unknown; IGDB comparison {igdb_trial['evaluated_candidates']} evaluated. "
          "Date predictions are not confirmed Twitch NEW badges.")
    audience = payload["coverage"].get("filtered_audience")
    if audience:
        print(f"Filtered audience: {audience['complete_categories']} complete / {audience['partial_categories']} partial categories; "
              f"{audience['follower_lookup_calls']} follower lookups, {audience['follower_cache_hits']} cache hits; "
              f"{audience['stop_reason']}. Only followers > 1,000 and viewers >= 10 qualify.")
        print(f"Collection elapsed {payload['coverage']['collection_elapsed_seconds']:.1f}s / "
              f"{args.max_collection_seconds:g}s overall deadline. "
              f"Follower request cap: {args.followers_max_calls if args.followers_max_calls is not None else 'none'}; "
              f"separate follower time cap: {args.followers_max_seconds if args.followers_max_seconds is not None else 'none'}.")
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
                f"- 本輪收集耗時：{payload['coverage']['collection_elapsed_seconds']:.1f} 秒；整體期限：{args.max_collection_seconds:g} 秒\n\n"
                "全體總觀眾、開台數及原始中位數保留所有直播台；篩選中位數使用下方獨立條件。\n"
                "新標記仍以附時間的 Twitch 直接觀察為準。\n\n"
                "### 14 天日期實驗（不等於官方 NEW）\n\n"
                f"- Twitch 原始日期：{twitch_trial['evaluated_candidates']} 款可判定、{twitch_trial['unknown_candidates']} 款缺資料或已過期\n"
                f"- Twitch 日期推算 NEW：{len(twitch_trial['predicted_new_game_ids'])} 款，其中尚未發售 {len(twitch_trial['upcoming_game_ids'])} 款\n"
                f"- IGDB 平行試算：{igdb_trial['evaluated_candidates']} 款可判定，推算 NEW {len(igdb_trial['predicted_new_game_ids'])} 款\n"
                f"- 與先前官方標記對照：{len(experiment['reference_checks'])} 筆來源／遊戲組合；詳見 JSON，回溯對照不代表官方規則驗證通過\n\n"
                "Glance 的判斷為日期差 < 14 天，包含未來日期；滿 14 天即不符合。\n"
                "缺少 Twitch 原始日期時保持未知；IGDB 結果分開顯示，不補成 Twitch 原始日期。\n"
            )
            if audience:
                summary.write(
                    "\n### 篩選中位數\n\n"
                    "- 條件：免費追隨者 > 1,000，且當次觀眾 ≥ 10（0～9 人不納入）\n"
                    f"- 完整分類：{audience['complete_categories']}；待補齊分類：{audience['partial_categories']}\n"
                    f"- 追隨數 API 查詢：{audience['follower_lookup_calls']}；24 小時內快取命中：{audience['follower_cache_hits']}\n"
                    f"- 追隨數查詢失敗：{audience['follower_lookup_failures']}；停止原因：{audience['stop_reason']}\n"
                    f"- 追隨數額外查詢上限：{args.followers_max_calls if args.followers_max_calls is not None else '無'}；額外時間上限：{args.followers_max_seconds if args.followers_max_seconds is not None else '無'}\n"
                    f"- 快取保存：{'成功' if audience['cache_saved'] else '失敗'}\n\n"
                    "任何 ≥10 觀眾的頻道追隨數未知時，該分類中位數暫不提供，避免使用偏差的部分樣本。\n"
                    "合格樣本為零時中位數也是空值；未知與空樣本皆不填 0。\n"
                    "舊歷史不倒填、不以原始中位數冒充新指標。\n"
                )


if __name__ == "__main__":
    main()
