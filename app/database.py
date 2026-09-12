from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from app.models import Base


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
    return sessionmaker(engine, expire_on_commit=False)
