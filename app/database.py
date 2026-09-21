from pathlib import Path
from sqlalchemy import MetaData, create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateTable
from sqlalchemy.orm import Session, sessionmaker
from app.models import Base, VCProfile


# V2 originally used create_all only.  These additive columns keep an existing
# SQLite installation usable while retaining the V1 tables untouched.
_PREFILTER_ADDITIVE_COLUMNS = {
    'event_strength': 'FLOAT NOT NULL DEFAULT 50',
    'matched_positive_signals': "JSON NOT NULL DEFAULT '[]'",
    'matched_negative_signals': "JSON NOT NULL DEFAULT '[]'",
    'supporting_signals': "JSON NOT NULL DEFAULT '[]'",
}


def _apply_additive_v2_schema(engine) -> None:
    if 'source_event_prefilters' not in inspect(engine).get_table_names():
        return
    existing = {column['name'] for column in inspect(engine).get_columns('source_event_prefilters')}
    missing = {name: ddl for name, ddl in _PREFILTER_ADDITIVE_COLUMNS.items() if name not in existing}
    if not missing:
        return
    # ALTER ADD COLUMN is supported by SQLite and PostgreSQL for these
    # defaults.  This is deliberately additive and does not rewrite any V1 row.
    with engine.begin() as connection:
        for name, ddl in missing.items():
            connection.execute(text(f'ALTER TABLE source_event_prefilters ADD COLUMN {name} {ddl}'))


def _migrate_vc_profile_semantics(engine) -> None:
    """Allow unknown capabilities and exact aliases without losing curated rows.

    Previous schemas used False / level 0 as defaults, so those values cannot
    safely be interpreted as confirmed absence.  During this one-time SQLite
    table rebuild they become NULL (unknown); positive values and all evidence
    are retained.
    """
    if 'vc_profiles' not in inspect(engine).get_table_names():
        return
    if engine.dialect.name != 'sqlite':
        raise RuntimeError('VC profile semantic migration currently supports SQLite only')

    old_columns = {column['name']: column for column in inspect(engine).get_columns('vc_profiles')}
    nullable_fields = {
        'recruiting_support', 'sales_support', 'marketing_support', 'pr_support',
        'branding_support', 'creative_support', 'video_support', 'creative_support_level',
    }
    if 'aliases' in old_columns and all(
        name in old_columns and old_columns[name]['nullable'] for name in nullable_fields
    ):
        return

    temporary_name = 'vc_profiles__semantic_migration'
    temporary_table = VCProfile.__table__.to_metadata(MetaData(), name=temporary_name)
    dialect = engine.dialect
    quote = dialect.identifier_preparer.quote
    columns = [column.name for column in VCProfile.__table__.columns]
    support_fields = nullable_fields - {'creative_support_level'}

    def source_expression(name: str) -> str:
        if name not in old_columns:
            if name in {'stage_focus', 'sector_focus', 'evidence', 'aliases'}:
                return "'[]'"
            return 'NULL'
        quoted = quote(name)
        if name in support_fields:
            return f'CASE WHEN {quoted} = 0 THEN NULL ELSE {quoted} END'
        if name == 'creative_support_level':
            return f'CASE WHEN {quoted} = 0 THEN NULL ELSE {quoted} END'
        return quoted

    with engine.begin() as connection:
        connection.exec_driver_sql(f'DROP TABLE IF EXISTS {quote(temporary_name)}')
        connection.exec_driver_sql(str(CreateTable(temporary_table).compile(dialect=dialect)))
        target_columns = ', '.join(quote(name) for name in columns)
        source_columns = ', '.join(source_expression(name) for name in columns)
        connection.exec_driver_sql(
            f'INSERT INTO {quote(temporary_name)} ({target_columns}) '
            f'SELECT {source_columns} FROM {quote("vc_profiles")}'
        )
        connection.exec_driver_sql('DROP TABLE vc_profiles')
        connection.exec_driver_sql(
            f'ALTER TABLE {quote(temporary_name)} RENAME TO {quote("vc_profiles")}'
        )
        connection.exec_driver_sql(
            'CREATE INDEX IF NOT EXISTS ix_vc_profiles_normalized_name ON vc_profiles (normalized_name)'
        )


def init_database(url: str) -> sessionmaker[Session]:
    parsed = make_url(url)
    kwargs = {}
    if parsed.get_backend_name() == 'sqlite':
        if parsed.database and parsed.database != ':memory:':
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        kwargs['connect_args'] = {'timeout': 30}
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == 'sqlite':
        @event.listens_for(engine, 'connect')
        def sqlite_settings(connection, _record) -> None:
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA journal_mode=WAL')
    Base.metadata.create_all(engine)
    _migrate_vc_profile_semantics(engine)
    _apply_additive_v2_schema(engine)
    return sessionmaker(engine, expire_on_commit=False)
