import argparse
from pathlib import Path
import asyncio
import logging
from app.config import Settings
from app.logging_config import configure_logging
from app.pipeline import run_pipeline
from app.scheduler import run_daemon


def main() -> int:
    parser = argparse.ArgumentParser(description='adely sales candidate research (no outreach)')
    parser.add_argument('mode', choices=['run-once', 'daemon'])
    parser.add_argument('--scoring-only', action='store_true', help='Stop after scoring; no Strategy or Discord')
    parser.add_argument('--scoring-report', action='store_true',
                        help='Stop after scoring and send the top candidates to Discord')
    parser.add_argument('--topic', action='append', help='Discovery topic; repeat to select a small batch')
    parser.add_argument('--discovery-cache', type=Path, help='Save accepted candidates for scoring replay')
    parser.add_argument('--replay', type=Path, help='Score a trusted local discovery cache without searching')
    args = parser.parse_args()
    if args.scoring_only and args.scoring_report:
        parser.error('--scoring-only and --scoring-report cannot be combined')
    if args.replay and (not (args.scoring_only or args.scoring_report) or args.topic or args.discovery_cache):
        parser.error('--replay requires --scoring-only or --scoring-report and cannot combine with --topic or --discovery-cache')
    if args.mode == 'daemon' and (args.scoring_only or args.scoring_report or args.topic or args.discovery_cache or args.replay):
        parser.error('--scoring-only, --scoring-report, and --topic require run-once')
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
