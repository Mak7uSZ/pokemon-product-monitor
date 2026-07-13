from __future__ import annotations

import argparse
import asyncio
import json
import queue
from pathlib import Path

import aiohttp

from pokemon_parser.config import load_config
from pokemon_parser.engine.antiban_policies import build_antiban
from pokemon_parser.engine.pipeline import Pipeline
from pokemon_parser.engine.runtime_state import RuntimeStateStore
from pokemon_parser.engine.selenium_dispatcher import SeleniumDispatcher
from pokemon_parser.engine.startup import bootstrap_runtime_storage
from pokemon_parser.engine.watchlist import WatchlistTracker
from pokemon_parser.models import WatchlistProduct
from pokemon_parser.notifications.telegram import TelegramNotifier
from pokemon_parser.parsers import SITE_LABELS
from pokemon_parser.utils.proxy import ProxyAwareSession
from pokemon_parser.workers.selenium_worker import SeleniumWorker
from pokemon_parser.engine.heartbeat import heartbeat_loop
from pokemon_parser.utils.logging_setup import setup_debug_logging
from pokemon_parser.utils.runtime_diagnostics import log_startup_diagnostics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="initialize or migrate the configured database, print the preflight report, and exit",
    )
    parser.add_argument("--sync-filters-json", action="store_true")
    parser.add_argument("--verbose-filters", action="store_true")
    parser.add_argument("--verbose-selenium", action="store_true")
    parser.add_argument("--build-watchlist", action="store_true")
    parser.add_argument("--scan-watchlist-once", action="store_true")
    parser.add_argument("--watchlist-site", "--watchlist-channel", dest="watchlist_site")
    parser.add_argument("--watchlist-product")
    parser.add_argument("--watchlist-url")
    parser.add_argument("--watchlist-sku")
    return parser.parse_args()

async def async_main() -> int:
    args = parse_args()
    setup_debug_logging(Path.cwd())

    cfg = load_config(
        verbose_filters=args.verbose_filters,
        verbose_selenium=args.verbose_selenium,
    )

    conn, storage, _startup_report = bootstrap_runtime_storage(
        cfg,
        sync_filters_json=args.sync_filters_json,
    )
    log_startup_diagnostics(cfg=cfg, project_root=cfg.base_dir.parent, startup_report=_startup_report)

    if args.init_db:
        print(json.dumps(_startup_report.to_dict(), ensure_ascii=False, indent=2))
        conn.close()
        return 0

    notifier = TelegramNotifier(cfg.telegram_bot_token, cfg.telegram_chat_id)
    antiban = build_antiban(cfg)

    if args.watchlist_url and (args.watchlist_product or args.watchlist_sku) and args.watchlist_site:
        product_key = args.watchlist_product or args.watchlist_sku
        storage.upsert_watchlist_entry(
            WatchlistProduct(
                site=args.watchlist_site,
                product_key=str(product_key),
                article_number=args.watchlist_sku,
                sku=args.watchlist_sku,
                title=str(product_key),
                url=args.watchlist_url,
                pinned=True,
                source="manual",
            )
        )

    if args.build_watchlist or args.scan_watchlist_once:
        tracker = WatchlistTracker(cfg=cfg, storage=storage, notifier=notifier)
        timeout = aiohttp.ClientTimeout(total=max(20, cfg.watchlist_request_timeout_seconds() * 3))
        async with aiohttp.ClientSession(timeout=timeout) as raw_session:
            session = ProxyAwareSession(raw_session, cfg)
            if args.build_watchlist:
                result = await tracker.build_from_filters(session, site=args.watchlist_site)
            else:
                result = await tracker.scan_once(
                    session,
                    site=args.watchlist_site,
                    product_key=args.watchlist_product or args.watchlist_sku,
                )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        conn.close()
        return 0

    runtime_state = RuntimeStateStore(
        site_labels=SITE_LABELS,
        enabled_map=cfg.parser_enabled_map(),
        action_mode=cfg.action_mode,
        scan_concurrency=cfg.parser_concurrency,
    )

    selenium_queue: "queue.Queue" = queue.Queue()

    selenium_dispatcher: SeleniumDispatcher

    def build_selenium_worker() -> SeleniumWorker:
        return SeleniumWorker(
            cfg=cfg,
            job_queue=selenium_queue,
            dispatcher=selenium_dispatcher,
            antiban=antiban,
            storage=storage,
        )

    selenium_dispatcher = SeleniumDispatcher(
        selenium_queue,
        worker_factory=build_selenium_worker if cfg.action_mode == "selenium" else None,
    )
    if hasattr(selenium_dispatcher, "set_runtime_config"):
        selenium_dispatcher.set_runtime_config(cfg.selenium_runtime_config())

    selenium_worker: SeleniumWorker | None = None
    if cfg.should_prewarm_selenium_on_runtime_start():
        await asyncio.to_thread(
            selenium_dispatcher.prewarm_browser,
            reason="cli_start",
            wait_ready_timeout=20,
        )
        selenium_worker = selenium_dispatcher.worker

    pipeline = Pipeline(
        cfg=cfg,
        storage=storage,
        notifier=notifier,
        selenium_dispatcher=selenium_dispatcher,
        antiban=antiban,
        runtime_state=runtime_state,
    )

    heartbeat_task = asyncio.create_task(
        heartbeat_loop(
            cfg=cfg,
            antiban=antiban,
            selenium_state=None,
            selenium_dispatcher=selenium_dispatcher,
            runtime_state=runtime_state,
            storage=storage,
            interval_seconds=cfg.heartbeat_interval_seconds,
        )
    )

    try:
        runtime_state.mark_runtime_started()
        if args.once:
            await pipeline.run_once()
            if selenium_dispatcher.worker is not None:
                await asyncio.to_thread(selenium_queue.join)
        else:
            await pipeline.run_forever()
        return 0

    finally:
        runtime_state.mark_runtime_stopped("runtime stopped")
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)

        selenium_dispatcher.stop(timeout=5)

        conn.close()


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
