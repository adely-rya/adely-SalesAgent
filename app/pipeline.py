"""A sequential pipeline with short transactions and per-item error isolation."""
import asyncio
from datetime import datetime
import logging
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.database import init_database
from app.config import Settings
from app.models import Run, Score, Strategy, ProcessingError, utcnow
from app.llm import LLMClient
from app.discovery import DISCOVERY_TOPICS, discover_topic
from app.deduplication import persist_candidate
from app.scoring import score_company, calculate_total_score, select_top_candidates
from app.strategy import generate_strategy
from app.report import format_report, send_discord_report

log = logging.getLogger(__name__)


async def run_pipeline(settings: Settings, client: LLMClient | None = None) -> bool:
    factory = init_database(settings.database_url)
    with factory.begin() as session:
        run = Run(config_json={
            key: getattr(settings, key) for key in (
                'discovery_model', 'scoring_model', 'strategy_model',
                'discovery_prompt_version', 'scoring_prompt_version', 'strategy_prompt_version',
                'top_candidates', 'timezone', 'strategy_web_search')})
        session.add(run)
    run_id = run.id
    log.info('run started id=%d', run_id)
    errors: list[str] = []
    owned_client = client is None

    def record_error(stage: str, subject: str, exc: Exception) -> None:
        # Exception messages can contain URLs/tokens or HTTP bodies: save only types.
        error_type = type(exc).__name__
        errors.append(f'{stage}: {error_type}')
        log.error('processing failed stage=%s subject=%s type=%s', stage, subject, error_type)
        with factory.begin() as session:
            session.add(ProcessingError(run_id=run_id, stage=stage, subject=subject,
                error_type=error_type, model=getattr(settings, f'{stage}_model', ''),
                prompt_version=getattr(settings, f'{stage}_prompt_version', '')))

    try:
        if not settings.has_api_key:
            log.error('OPENAI_API_KEY is not configured.\nSet OPENAI_API_KEY in .env.')
            raise ValueError('Missing API key')
        client = client or LLMClient(settings)
        candidates = {}  # trigger_id -> (company_id, validated candidate)
        observed_companies = set()
        for topic in DISCOVERY_TOPICS:
            try:
                found, evidence, rejected = await discover_topic(client, settings, topic)
                if rejected:
                    record_error('discovery', topic, ValueError('Rejected candidates'))
                for candidate in found:
                    try:
                        with factory.begin() as session:
                            company, trigger, is_new = persist_candidate(session, candidate, run_id,
                                settings.discovery_model, settings.discovery_prompt_version, evidence)
                            # Retry a previously discovered trigger only if it has never been scored.
                            has_score = session.scalar(select(Score.id).where(Score.trigger_id == trigger.id).limit(1))
                            observed_companies.add(company.id)
                        if is_new or has_score is None:
                            candidates[trigger.id] = (company.id, candidate)
                    except Exception as exc:
                        record_error('discovery', candidate.company_name, exc)
            except Exception as exc:
                record_error('discovery', topic, exc)
        with factory.begin() as session:
            session.get(Run, run_id).candidate_count = len(observed_companies)
        log.info('companies deduplicated observed=%d eligible_triggers=%d', len(observed_companies), len(candidates))
        scores = []
        for trigger_id, (company_id, candidate) in candidates.items():
            try:
                output = await score_company(client, settings, candidate)
                values = output.model_dump(exclude={'risks'})
                score = Score(company_id=company_id, trigger_id=trigger_id, run_id=run_id,
                    **values, risks_json=output.risks, total_score=calculate_total_score(output),
                    model=settings.scoring_model, prompt_version=settings.scoring_prompt_version)
                with factory.begin() as session:
                    session.add(score)
                scores.append(score)
            except Exception as exc:
                record_error('scoring', candidate.company_name, exc)
        top = select_top_candidates(scores, settings.top_candidates)
        with factory.begin() as session:
            session.get(Run, run_id).scored_count = len({score.company_id for score in scores})
            for rank, score in enumerate(top, 1):
                session.get(Score, score.id).selected_rank = rank
        log.info('top companies selected count=%d', len(top))
        entries = []
        for score in top:
            candidate = candidates[score.trigger_id][1]
            entry = {'candidate': candidate, 'score': score}
            entries.append(entry)
            try:
                generated = await generate_strategy(client, settings, candidate, score)
                with factory.begin() as session:
                    session.add(Strategy(company_id=score.company_id, trigger_id=score.trigger_id,
                        content_json=generated.value.model_dump(), model=settings.strategy_model,
                        prompt_version=settings.strategy_prompt_version, run_id=run_id,
                        evidence_urls=sorted(generated.evidence_urls)))
                    session.get(Run, run_id).strategy_count += 1
                entry['strategy'] = generated.value
            except Exception as exc:
                record_error('strategy', candidate.company_name, exc)
        with factory.begin() as session:
            run = session.get(Run, run_id)
            run.status = 'partial' if errors else 'completed'
        report = format_report(run, entries, str(datetime.now(ZoneInfo(settings.timezone)).date()))
        try:
            await send_discord_report(report, settings.discord_webhook_url.get_secret_value())
        except Exception as exc:
            record_error('discord', 'report', exc)
    except asyncio.CancelledError:
        record_error('pipeline', 'run', RuntimeError('Run cancelled'))
        with factory.begin() as session:
            session.get(Run, run_id).status = 'failed'
        raise
    except Exception as exc:
        record_error('pipeline', 'run', exc)
        with factory.begin() as session:
            session.get(Run, run_id).status = 'failed'
    finally:
        with factory.begin() as session:
            run = session.get(Run, run_id)
            if run.status != 'failed':
                run.status = 'partial' if errors else 'completed'
            run.finished_at = utcnow()
            run.error_message = '; '.join(errors) if errors else None
        if owned_client and client is not None:
            await client.close()
        log.info('run completed id=%d status=%s', run_id, run.status)
        factory.kw['bind'].dispose()
    return run.status == 'completed'
