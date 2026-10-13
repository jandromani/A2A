from collections.abc import Generator
from pathlib import Path

from fastapi import Request
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str) -> Engine:
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        if database_url.startswith("sqlite:///"):
            path = database_url.removeprefix("sqlite:///").split("?", 1)[0]
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        database_url,
        connect_args=connect_args,
        pool_pre_ping=not database_url.startswith("sqlite"),
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def initialize_database(
    engine: Engine,
    factory: sessionmaker[Session],
    config: Settings | None = None,
) -> None:
    from app import models  # noqa: F401
    from app.seed import seed_database

    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(bind=engine)
    with factory() as session:
        seed_database(session, config)


def get_db(request: Request) -> Generator[Session, None, None]:
    factory = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()
