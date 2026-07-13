from __future__ import annotations

from pokemon_parser.config import AppConfig
from pokemon_parser.engine.antiban import (
    AntiBanManager,
    BackoffPolicy,
    LayerPolicy,
    SitePolicy,
)


def build_antiban(cfg: AppConfig | None = None) -> AntiBanManager:
    """
    Central antiban policy factory.

    Design goals:
    - parser traffic is cheaper and can be more frequent
    - worker traffic is more dangerous and must be slower
    - MediaMarkt and PocketGames worker flows are treated as high-risk
    - Bol worker is slightly less strict, but still protected
    """
    policies = {
        "bol": SitePolicy(
            site="bol",
            parser=LayerPolicy(
                min_interval_seconds=2.5,
                open_after_denies=3,
                backoff=BackoffPolicy(
                    base_seconds=30,
                    max_seconds=1800,
                    jitter_seconds=5,
                ),
                half_open_probe_allowed=1,
            ),
            worker=LayerPolicy(
                min_interval_seconds=60.0,
                open_after_denies=2,
                backoff=BackoffPolicy(
                    base_seconds=300,
                    max_seconds=7200,
                    jitter_seconds=30,
                ),
                half_open_probe_allowed=1,
                job_timeout_seconds=240.0,
            ),
        ),
        "mediamarkt": SitePolicy(
            site="mediamarkt",
            parser=LayerPolicy(
                min_interval_seconds=3.0,
                open_after_denies=3,
                backoff=BackoffPolicy(
                    base_seconds=45,
                    max_seconds=1800,
                    jitter_seconds=5,
                ),
                half_open_probe_allowed=1,
            ),
            worker=LayerPolicy(
                min_interval_seconds=90.0,
                open_after_denies=1,
                backoff=BackoffPolicy(
                    base_seconds=600,
                    max_seconds=10800,
                    jitter_seconds=60,
                ),
                half_open_probe_allowed=1,
                job_timeout_seconds=300.0,
            ),
        ),
        "pocketgames": SitePolicy(
            site="pocketgames",
            parser=LayerPolicy(
                min_interval_seconds=2.0,
                open_after_denies=3,
                backoff=BackoffPolicy(
                    base_seconds=30,
                    max_seconds=1800,
                    jitter_seconds=5,
                ),
                half_open_probe_allowed=1,
            ),
            worker=LayerPolicy(
                min_interval_seconds=90.0,
                open_after_denies=1,
                backoff=BackoffPolicy(
                    base_seconds=600,
                    max_seconds=10800,
                    jitter_seconds=60,
                ),
                half_open_probe_allowed=1,
                job_timeout_seconds=420.0,
            ),
        ),
        "dreamland": SitePolicy(
        site="dreamland",
        parser=LayerPolicy(
            min_interval_seconds=3.0,
            open_after_denies=3,
            backoff=BackoffPolicy(
                base_seconds=45,
                max_seconds=1800,
                jitter_seconds=5,
            ),
            half_open_probe_allowed=1,
        ),
        worker=LayerPolicy(
            min_interval_seconds=90.0,
            open_after_denies=1,
            backoff=BackoffPolicy(
                base_seconds=600,
                max_seconds=10800,
                jitter_seconds=60,
            ),
            half_open_probe_allowed=1,
            job_timeout_seconds=300.0,
        ),
    ),
    }

    if cfg is not None:
        adjusted: dict[str, SitePolicy] = {}
        for site, policy in policies.items():
            parser_policy = policy.parser
            parser_cooldown = max(1.0, cfg.site_cooldown_seconds(site))
            parser_interval = max(0.1, cfg.effective_scan_delay_seconds(site))
            adjusted[site] = SitePolicy(
                site=site,
                parser=LayerPolicy(
                    min_interval_seconds=parser_interval,
                    open_after_denies=parser_policy.open_after_denies,
                    backoff=BackoffPolicy(
                        base_seconds=parser_cooldown,
                        max_seconds=max(parser_cooldown, parser_policy.backoff.max_seconds),
                        jitter_seconds=parser_policy.backoff.jitter_seconds,
                        factor=parser_policy.backoff.factor,
                    ),
                    half_open_probe_allowed=parser_policy.half_open_probe_allowed,
                    job_timeout_seconds=parser_policy.job_timeout_seconds,
                ),
                worker=policy.worker,
            )
        policies = adjusted

    return AntiBanManager(policies)
