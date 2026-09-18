"""
Shared fixtures that spin up a real pglite PostgreSQL instance.
No mocking, no monkey-patching – every test hits a live database.
"""

import os
import time

import pytest
from py_pglite import PGliteConfig, PGliteManager
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool


@pytest.fixture(scope="session")
def pglite_manager():
    """Start a pglite instance for the whole test session."""
    cfg = PGliteConfig(use_tcp=False)
    mgr = PGliteManager(config=cfg)
    mgr.start()
    time.sleep(2)
    yield mgr, cfg
    mgr.stop()


@pytest.fixture(scope="session")
def pg_engine(pglite_manager):
    """A SQLAlchemy engine connected to the pglite instance."""
    mgr, cfg = pglite_manager
    socket_dir = os.path.dirname(cfg.socket_path)
    url = "postgresql://postgres:postgres@/postgres?host=%s" % socket_dir
    engine = create_engine(url, poolclass=StaticPool)
    conn = engine.connect()
    conn.connection.dbapi_connection.autocommit = True
    conn.close()
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def pg_url(pglite_manager):
    """The SQLAlchemy URL for the pglite instance."""
    _, cfg = pglite_manager
    socket_dir = os.path.dirname(cfg.socket_path)
    return "postgresql://postgres:postgres@/postgres?host=%s" % socket_dir


@pytest.fixture()
def _clean_schemas(pg_engine):
    """Drop all non-system schemas before each test so tests are isolated."""
    with pg_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT nspname FROM pg_namespace "
                "WHERE nspname NOT IN "
                "('information_schema','pg_catalog','pg_toast','public') "
                "AND nspname NOT LIKE 'pg_%%'"
            )
        ).fetchall()
        for (name,) in rows:
            conn.execute(text('DROP SCHEMA IF EXISTS "%s" CASCADE' % name))
    # also clean the public ORM tables
    with pg_engine.connect() as conn:
        conn.execute(text('DROP TABLE IF EXISTS "table" CASCADE'))
        conn.execute(text("DROP TABLE IF EXISTS snapshot CASCADE"))
    yield


@pytest.fixture()
def stellar_config(pg_url, tmp_path, monkeypatch):
    """Write a stellar.yaml and chdir so Stellar finds it."""
    config_path = tmp_path / "stellar.yaml"
    config_path.write_text(
        "project_name: 'test_project'\n"
        "tracked_databases: ['rentals']\n"
        "url: '%s'\n"
        "stellar_url: '%s'\n" % (pg_url, pg_url)
    )
    monkeypatch.chdir(tmp_path)
    return config_path


@pytest.fixture()
def rentals_schema(pg_engine, _clean_schemas):
    """Create the rentals schema with sample data."""
    with pg_engine.connect() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS rentals"))
        conn.execute(
            text(
                """
            CREATE TABLE rentals.movies (
                id SERIAL PRIMARY KEY,
                title VARCHAR(255) NOT NULL,
                genre VARCHAR(100),
                release_year INTEGER
            )
        """
            )
        )
        conn.execute(
            text(
                """
            CREATE TABLE rentals.customers (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                email VARCHAR(255),
                joined_date DATE DEFAULT CURRENT_DATE
            )
        """
            )
        )
        conn.execute(
            text(
                """
            INSERT INTO rentals.movies (title, genre, release_year) VALUES
                ('The Matrix', 'Sci-Fi', 1999),
                ('Inception', 'Sci-Fi', 2010),
                ('The Godfather', 'Crime', 1972)
        """
            )
        )
        conn.execute(
            text(
                """
            INSERT INTO rentals.customers (name, email) VALUES
                ('Alice Johnson', 'alice@example.com'),
                ('Bob Smith', 'bob@example.com')
        """
            )
        )


@pytest.fixture()
def app(pg_engine, stellar_config, rentals_schema):
    """A fully-initialised Stellar app backed by pglite."""
    from stellar.app import Stellar

    return Stellar(engine=pg_engine)
