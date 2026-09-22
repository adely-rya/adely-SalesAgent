"""Import and lookup support for locally curated VC profiles."""
from __future__ import annotations

import csv
from datetime import datetime
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deduplication import normalize_name
from app.models import VCProfile
from app.schemas import VCProfileInput


def _coerce_csv(value: str) -> object:
    value = value.strip()
    if not value:
        return None
    if value.lower() in {'true', 'false'}:
        return value.lower() == 'true'
    if value.startswith('[') or value.startswith('{'):
        return json.loads(value)
    if ',' in value:
        return [item.strip() for item in value.split(',') if item.strip()]
    return value


def load_profile_inputs(path: Path) -> list[VCProfileInput]:
    """Load a JSON list/object or CSV; validation keeps imports safe and explicit."""
    if path.suffix.lower() == '.json':
        raw = json.loads(path.read_text(encoding='utf-8'))
        rows = raw if isinstance(raw, list) else raw.get('vc_profiles', [])
    elif path.suffix.lower() == '.csv':
        with path.open(encoding='utf-8-sig', newline='') as stream:
            rows = [{key: _coerce_csv(value) for key, value in row.items() if value is not None}
                    for row in csv.DictReader(stream)]
    else:
        raise ValueError('VC profile import must be .json or .csv')
    return [VCProfileInput.model_validate(row) for row in rows]


def import_vc_profiles(session: Session, profiles: list[VCProfileInput]) -> int:
    """Upsert by normalized name and return the number of imported records."""
    identity_owner: dict[str, str] = {}
    for data in profiles:
        for identity in (data.name, *data.aliases):
            normalized_identity = normalize_name(identity)
            owner = identity_owner.get(normalized_identity)
            if owner is not None and owner != data.name:
                raise ValueError(f'VC profile identity {identity!r} collides between {owner!r} and {data.name!r}')
            identity_owner[normalized_identity] = data.name

    for data in profiles:
        normalized = normalize_name(data.name)
        profile = session.scalar(select(VCProfile).where(VCProfile.normalized_name == normalized))
        # Keep datetime values as datetimes for SQLAlchemy DateTime columns.
        values = data.model_dump(mode='python')
        if profile is None:
            profile = VCProfile(normalized_name=normalized, **values)
            session.add(profile)
        else:
            for key, value in values.items():
                setattr(profile, key, value)
    session.flush()
    return len(profiles)


def profile_context(session: Session, provider_names: set[str]) -> list[dict]:
    """Only include profiles whose provider is a discovery source for the opportunity."""
    normalized = {normalize_name(name) for name in provider_names if name}
    if not normalized:
        return []
    # Alias JSON is intentionally matched in Python: only exact normalized
    # canonical-name/alias matches are accepted, never substring matches.
    profiles = session.scalars(select(VCProfile)).all()
    return select_profile_context([profile_context_record(profile) for profile in profiles], provider_names)


def profile_context_record(profile: VCProfile) -> dict:
    return {
        'name': profile.name,
        'aliases': profile.aliases or [],
        'stage_focus': profile.stage_focus,
        'sector_focus': profile.sector_focus,
        'support': {
            'recruiting': profile.recruiting_support, 'sales': profile.sales_support,
            'marketing': profile.marketing_support, 'pr': profile.pr_support,
            'branding': profile.branding_support, 'creative': profile.creative_support,
            'video': profile.video_support,
        },
        'creative_support_level': profile.creative_support_level,
        'potential_partner_score': profile.potential_partner_score,
        'notes': profile.notes,
        'evidence': profile.evidence,
        'verified_at': profile.verified_at.isoformat() if profile.verified_at else None,
    }


def select_profile_context(profiles: list[dict], provider_names: set[str]) -> list[dict]:
    """Select known VC records in memory after a single repository read."""
    normalized = {normalize_name(name) for name in provider_names if name}
    return [profile for profile in profiles
            if normalized.intersection({normalize_name(profile['name']),
                                        *(normalize_name(alias) for alias in profile.get('aliases', []))})]

