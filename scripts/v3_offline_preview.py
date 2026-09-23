"""Render a stored V2 run as a V3 Scout report without calling any API."""
from __future__ import annotations

import argparse
import os

from sqlalchemy import select

from app.database import init_database
from app.domain import Opportunity
from app.models import CandidateRecord
from app.schemas import Candidate, Event
from app.v3_scout import build_scout_report, scout_distribution


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', type=int, required=True)
    parser.add_argument('--database-url', default=os.environ.get('DATABASE_URL', 'sqlite:///data/sales.db'))
    args = parser.parse_args()
    factory = init_database(args.database_url)
    try:
        with factory() as session:
            rows = list(session.scalars(select(CandidateRecord).where(
                CandidateRecord.run_id == args.run_id).order_by(CandidateRecord.id)))
        opportunities = []
        for row in rows:
            snapshot = row.merged_json or {}
            events = [Event.model_validate(item) for item in snapshot.get('events', [])]
            if not events:
                continue
            candidate = Candidate.model_validate(row.candidate_json)
            opportunities.append(Opportunity(
                company_name=row.company_name if hasattr(row, 'company_name') else events[0].company_name,
                events=events, candidate=candidate,
                origins=list(row.discovery_origins or []),
                sources=list(row.discovery_sources or []), status=row.status))
        report, records = build_scout_report(opportunities)
        print(f'Offline V3 Scout preview: Run {args.run_id}')
        print(f'Candidates: {len(records)}')
        print(f'Distribution: {scout_distribution(records)}')
        print(report)
        return 0
    finally:
        factory.kw['bind'].dispose()


if __name__ == '__main__':
    raise SystemExit(main())
