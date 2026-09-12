import argparse
import asyncio
import logging
from app.config import Settings
from app.logging_config import configure_logging
from app.pipeline import run_pipeline
from app.scheduler import run_daemon


def main() -> int:
    parser = argparse.ArgumentParser(description='adely sales candidate research (no outreach)')
    parser.add_argument('mode', choices=['run-once', 'daemon'])
    args = parser.parse_args()
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
        return 0 if asyncio.run(run_pipeline(settings)) else 1
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logging.getLogger(__name__).error('Application failed type=%s', type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
