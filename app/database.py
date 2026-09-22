from pathlib import Path
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from app.models import Base


# V2 originally used create_all only.  These additive columns keep an existing
# SQLite installation usable while retaining the V1 tables untouched.
_PREFILTER_ADDITIVE_COLUMNS = {
    'event_strength': 'FLOAT NOT NULL DEFAULT 50',
    'matched_positive_signals': "JSON NOT NULL DEFAULT '[]'",
    'matched_negative_signals': "JSON NOT NULL DEFAULT '[]'",
    'supporting_signals': "JSON NOT NULL DEFAULT '[]'",
}

_PROCESSING_ERROR_ADDITIVE_COLUMNS = {
    'exception_type': 'VARCHAR(128)',
    'error_message': 'TEXT',
}


def _apply_additive_v2_schema(engine) -> None:
    tables = set(inspect(engine).get_table_names())
    if 'source_event_prefilters' in tables:
        existing = {column['name'] for column in inspect(engine).get_columns('source_event_prefilters')}
        missing = {name: ddl for name, ddl in _PREFILTER_ADDITIVE_COLUMNS.items() if name not in existing}
        # ALTER ADD COLUMN is supported by SQLite and PostgreSQL for these
        # defaults. This is deliberately additive and does not rewrite rows.
        if missing:
            with engine.begin() as connection:
                for name, ddl in missing.items():
                    connection.execute(text(f'ALTER TABLE source_event_prefilters ADD COLUMN {name} {ddl}'))

    if 'processing_errors' not in tables:
        return
    existing = {column['name'] for column in inspect(engine).get_columns('processing_errors')}
    with engine.begin() as connection:
        for name, ddl in _PROCESSING_ERROR_ADDITIVE_COLUMNS.items():
            if name not in existing:
                connection.execute(text(f'ALTER TABLE processing_errors ADD COLUMN {name} {ddl}'))


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
    _apply_additive_v2_schema(engine)
    return sessionmaker(engine, expire_on_commit=False)
