import argparse
from pathlib import Path
import asyncio
import logging
from sqlalchemy import select
from app.config import Settings
from app.logging_config import configure_logging
from app.pipeline import run_pipeline
from app.scheduler import run_daemon, run_fixed_collector_daemon
from app.v2_pipeline import run_daily_pipeline, run_v2_pipeline
from app.collectors import collect_fixed_sources
from app.database import init_database
from app.vc_profiles import import_vc_profiles, load_profile_inputs
from app.models import CandidateRecord, Company, SourceEvent
from app.prefilters import prefilter_source_events, summarize_prefilter
from app.deduplication import normalize_name


def main() -> int:
    parser = argparse.ArgumentParser(description='adely sales candidate research (no outreach)')
    parser.add_argument('mode', choices=['run-once', 'daemon', 'v2-run-once', 'daily-run',
                                         'collect-sources', 'collect-fixed', 'collect-fixed-daemon',
                                         'prefilter-events', 'import-vc-profiles', 'trace-opportunity'])
    parser.add_argument('--scoring-only', action='store_true', help='Stop after scoring; no Strategy or Discord')
    parser.add_argument('--scoring-report', action='store_true',
                        help='Stop after scoring and send the top candidates to Discord')
    parser.add_argument('--topic', action='append', help='Discovery topic; repeat to select a small batch')
    parser.add_argument('--discovery-cache', type=Path, help='Save accepted candidates for scoring replay')
    parser.add_argument('--replay', type=Path, help='Score a trusted local discovery cache without searching')
    parser.add_argument('--no-collect', action='store_true', help='Legacy v2-run-once: use stored source_events only')
    parser.add_argument('--collect-before-run', action='store_true',
                        help='daily-run: also collect fixed sources once before processing')
    parser.add_argument('--no-web-discovery', action='store_true', help='V2: skip Web Search discovery')
    parser.add_argument('--no-fixed-discovery', action='store_true', help='V2: skip source_events interpretation')
    parser.add_argument('--vc-profiles', type=Path, help='JSON or CSV file for import-vc-profiles')
    parser.add_argument('--prefilter-limit', type=int, default=None,
                        help='prefilter-events: maximum recent source_events to evaluate')
    parser.add_argument('--opportunity-id', type=int, help='trace-opportunity: CandidateRecord ID')
    parser.add_argument('--company', help='trace-opportunity: exact company name')
    args = parser.parse_args()
    if args.scoring_only and args.scoring_report:
        parser.error('--scoring-only and --scoring-report cannot be combined')
    if args.replay and (not (args.scoring_only or args.scoring_report) or args.topic or args.discovery_cache):
        parser.error('--replay requires --scoring-only or --scoring-report and cannot combine with --topic or --discovery-cache')
    if args.mode == 'daemon' and (args.scoring_only or args.scoring_report or args.topic or args.discovery_cache or args.replay):
        parser.error('--scoring-only, --scoring-report, and --topic require run-once')
    if args.mode not in {'v2-run-once', 'daily-run'} and (args.no_collect or args.no_web_discovery or args.no_fixed_discovery or args.collect_before_run):
        parser.error('V2 pipeline options require v2-run-once or daily-run')
    if args.mode != 'daily-run' and args.collect_before_run:
        parser.error('--collect-before-run is only used by daily-run')
    if args.mode == 'daily-run' and args.no_collect and args.collect_before_run:
        parser.error('--no-collect and --collect-before-run cannot be combined')
    if args.mode == 'import-vc-profiles' and args.vc_profiles is None:
        parser.error('import-vc-profiles requires --vc-profiles FILE')
    if args.mode != 'import-vc-profiles' and args.vc_profiles is not None:
        parser.error('--vc-profiles is only used by import-vc-profiles')
    if args.mode != 'prefilter-events' and args.prefilter_limit is not None:
        parser.error('--prefilter-limit is only used by prefilter-events')
    if args.prefilter_limit is not None and args.prefilter_limit < 1:
        parser.error('--prefilter-limit must be at least 1')
    if args.mode == 'trace-opportunity' and not (args.opportunity_id or args.company):
        parser.error('trace-opportunity requires --opportunity-id ID or --company NAME')
    if args.mode != 'trace-opportunity' and (args.opportunity_id is not None or args.company is not None):
        parser.error('--opportunity-id and --company are only used by trace-opportunity')
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
        if args.mode == 'collect-fixed-daemon':
            asyncio.run(run_fixed_collector_daemon(settings))
            return 0
        if args.mode in {'collect-sources', 'collect-fixed'}:
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
                    print(f'{item.source_name}\t{item.decision.status}\t{item.decision.classified_event_type}'
                          f'\t{item.decision.event_strength:g}\t{item.decision.reason}\t{item.title}')
                for source_name, counts in summarize_prefilter(evaluated).items():
                    print(f'SUMMARY\t{source_name}\tcollected={counts["collected"]}\tpass={counts["pass"]}'
                          f'\thold={counts["hold"]}\tdrop={counts["drop"]}'
                          f'\tavg_strength={counts["avg_strength"]:.1f}')
                return 0
            finally:
                factory.kw['bind'].dispose()
        if args.mode == 'trace-opportunity':
            factory = init_database(settings.database_url)
            try:
                with factory() as session:
                    query = select(CandidateRecord, Company.name).join(
                        Company, Company.id == CandidateRecord.company_id)
                    if args.opportunity_id:
                        query = query.where(CandidateRecord.id == args.opportunity_id)
                    else:
                        query = query.where(Company.normalized_name == normalize_name(args.company))
                    record = session.execute(query.order_by(CandidateRecord.created_at.desc()).limit(1)).first()
                    if record is None:
                        print('Opportunity not found')
                        return 1
                    opportunity, company_name = record
                    snapshot = opportunity.merged_json or {}
                    print(f'Company: {company_name}')
                    print(f'Opportunity ID: {opportunity.id} / Run: {opportunity.run_id}')
                    print('EVENTS')
                    for event in snapshot.get('events', []):
                        print(f"- {event.get('source_name', '?')} / {event.get('event_type', '?')} / {event.get('title', '?')}")
                        print(f"  {event.get('source_url', '')}")
                    gate = snapshot.get('gate') or {'status': opportunity.status,
                        'win_pre': opportunity.win_pre_json}
                    print('GATE')
                    print(f"STATUS: {gate.get('status', opportunity.status)}")
                    if gate.get('win_pre'):
                        win_pre = gate['win_pre']
                        print(f"WIN_PRE: {win_pre.get('win_pre')} / CONFIDENCE: {win_pre.get('confidence')}")
                        print(f"REASON: {win_pre.get('reason')}")
                    print('RESEARCH')
                    print(snapshot.get('research') or 'not run')
                    print('FINAL')
                    print(snapshot.get('score') or 'not scored')
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
        if args.mode == 'daily-run':
            return 0 if asyncio.run(run_daily_pipeline(
                settings, collect=args.collect_before_run and not args.no_collect,
                web_discovery=not args.no_web_discovery,
                fixed_discovery=not args.no_fixed_discovery, topics=args.topic)) else 1
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
