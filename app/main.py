import argparse
from pathlib import Path
import asyncio
import logging
from sqlalchemy import select
from app.config import Settings
from app.logging_config import configure_logging
from app.pipeline import run_pipeline
from app.scheduler import run_daemon
from app.v2_pipeline import run_v2_pipeline
from app.collectors import collect_fixed_sources
from app.database import init_database
from app.vc_profiles import import_vc_profiles, load_profile_inputs
from app.models import SourceEvent
from app.prefilters import prefilter_source_events, summarize_prefilter


def main() -> int:
    parser = argparse.ArgumentParser(description='adely sales candidate research (no outreach)')
    parser.add_argument('mode', choices=['run-once', 'daemon', 'v2-run-once', 'collect-sources',
                                         'prefilter-events', 'import-vc-profiles'])
    parser.add_argument('--scoring-only', action='store_true', help='Stop after scoring; no Strategy or Discord')
    parser.add_argument('--scoring-report', action='store_true',
                        help='Stop after scoring and send the top candidates to Discord')
    parser.add_argument('--topic', action='append', help='Discovery topic; repeat to select a small batch')
    parser.add_argument('--discovery-cache', type=Path, help='Save accepted candidates for scoring replay')
    parser.add_argument('--replay', type=Path, help='Score a trusted local discovery cache without searching')
    parser.add_argument('--no-collect', action='store_true', help='V2: use already stored source_events only')
    parser.add_argument('--no-web-discovery', action='store_true', help='V2: skip Web Search discovery')
    parser.add_argument('--no-fixed-discovery', action='store_true', help='V2: skip source_events interpretation')
    parser.add_argument('--vc-profiles', type=Path, help='JSON or CSV file for import-vc-profiles')
    parser.add_argument('--prefilter-limit', type=int, default=None,
                        help='prefilter-events: maximum recent source_events to evaluate')
    args = parser.parse_args()
    if args.scoring_only and args.scoring_report:
        parser.error('--scoring-only and --scoring-report cannot be combined')
    if args.replay and (not (args.scoring_only or args.scoring_report) or args.topic or args.discovery_cache):
        parser.error('--replay requires --scoring-only or --scoring-report and cannot combine with --topic or --discovery-cache')
    if args.mode == 'daemon' and (args.scoring_only or args.scoring_report or args.topic or args.discovery_cache or args.replay):
        parser.error('--scoring-only, --scoring-report, and --topic require run-once')
    if args.mode != 'v2-run-once' and (args.no_collect or args.no_web_discovery or args.no_fixed_discovery):
        parser.error('--no-collect, --no-web-discovery, and --no-fixed-discovery require v2-run-once')
    if args.mode == 'import-vc-profiles' and args.vc_profiles is None:
        parser.error('import-vc-profiles requires --vc-profiles FILE')
    if args.mode != 'import-vc-profiles' and args.vc_profiles is not None:
        parser.error('--vc-profiles is only used by import-vc-profiles')
    if args.mode != 'prefilter-events' and args.prefilter_limit is not None:
        parser.error('--prefilter-limit is only used by prefilter-events')
    if args.prefilter_limit is not None and args.prefilter_limit < 1:
        parser.error('--prefilter-limit must be at least 1')
    try:
        settings = Settings.from_env()
    except Exception:
        print('Invalid configuration. Check environment variable values in .env.')
        return 2
    configure_logging(settings)
    try:
        if args.mode == 'daemon':
            asyncio.run(run_daemon(settings))
            return 0
        if args.mode == 'collect-sources':
            factory = init_database(settings.database_url)
            try:
                result = asyncio.run(collect_fixed_sources(settings, factory))
                return 0 if not result.errors else 1
            finally:
                factory.kw['bind'].dispose()
        if args.mode == 'prefilter-events':
            factory = init_database(settings.database_url)
            try:
                limit = args.prefilter_limit or settings.source_prefilter_batch_size
                with factory.begin() as session:
                    events = list(session.scalars(select(SourceEvent).order_by(
                        SourceEvent.collected_at.desc(), SourceEvent.id.desc()).limit(limit)))
                    evaluated = prefilter_source_events(session, events, settings)
                for item in evaluated:
                    print(f'{item.decision.status}\t{item.source_name}\t{item.decision.classified_event_type}'
                          f'\t{item.decision.score:g}\t{item.title}\t{item.decision.reason}')
                for source_name, counts in summarize_prefilter(evaluated).items():
                    print(f'SUMMARY\t{source_name}\tcollected={counts["collected"]}\tpass={counts["pass"]}'
                          f'\thold={counts["hold"]}\tdrop={counts["drop"]}')
                return 0
            finally:
                factory.kw['bind'].dispose()
        if args.mode == 'import-vc-profiles':
            profiles = load_profile_inputs(args.vc_profiles)
            factory = init_database(settings.database_url)
            try:
                with factory.begin() as session:
                    count = import_vc_profiles(session, profiles)
                logging.getLogger(__name__).info('vc profiles imported count=%d', count)
                return 0
            finally:
                factory.kw['bind'].dispose()
        if args.mode == 'v2-run-once':
            return 0 if asyncio.run(run_v2_pipeline(
                settings, collect=not args.no_collect, web_discovery=not args.no_web_discovery,
                fixed_discovery=not args.no_fixed_discovery, topics=args.topic)) else 1
        return 0 if asyncio.run(run_pipeline(
            settings, scoring_only=args.scoring_only, scoring_report=args.scoring_report,
            topics=args.topic, discovery_cache=args.discovery_cache, replay=args.replay)) else 1
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logging.getLogger(__name__).error('Application failed type=%s', type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
