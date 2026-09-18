#!/usr/bin/env python3
"""
Launcher script that starts a py-pglite PostgreSQL instance,
creates the rentals schema with sample data, writes stellar.yaml,
and provides an interactive CLI for Stellar operations.

Because pglite only supports a single connection at a time, all
operations run inside this process.

Usage:
    python start_pglite.py          # interactive CLI
    python start_pglite.py --setup  # setup only, return manager (for tests)

Interactive commands:
    snapshot [name]   - take a snapshot (default name: snap1, snap2, ...)
    restore  [name]   - restore a snapshot (default: latest)
    list              - list snapshots
    remove   <name>   - remove a snapshot
    show              - show current tables and data
    sql <query>       - run arbitrary SQL
    quit / exit       - stop pglite and exit
"""
import os
import sys
import time

from py_pglite import PGliteConfig, PGliteManager
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool


def get_engine_url(socket_dir, dbname="postgres"):
    return "postgresql://postgres:postgres@/%s?host=%s" % (dbname, socket_dir)


def setup_rentals(conn):
    """Create the rentals schema with movies and customers tables."""
    conn.execute(text('CREATE SCHEMA IF NOT EXISTS rentals'))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS rentals.movies (
            id SERIAL PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            genre VARCHAR(100),
            release_year INTEGER
        )
    """))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS rentals.customers (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            email VARCHAR(255) UNIQUE,
            joined_date DATE DEFAULT CURRENT_DATE
        )
    """))
    existing = conn.execute(
        text("SELECT count(*) FROM rentals.movies")
    ).scalar()
    if existing == 0:
        conn.execute(text("""
            INSERT INTO rentals.movies (title, genre, release_year) VALUES
                ('The Matrix', 'Sci-Fi', 1999),
                ('Inception', 'Sci-Fi', 2010),
                ('The Godfather', 'Crime', 1972)
        """))
        conn.execute(text("""
            INSERT INTO rentals.customers (name, email) VALUES
                ('Alice Johnson', 'alice@example.com'),
                ('Bob Smith', 'bob@example.com')
        """))


def write_stellar_config(url):
    """Write stellar.yaml pointing at the running pglite instance."""
    with open("stellar.yaml", "w") as f:
        f.write(
            "project_name: 'rentals'\n"
            "tracked_databases: ['rentals']\n"
            "url: '%s'\n"
            "stellar_url: '%s'\n" % (url, url)
        )


def start_pglite_env():
    """Start pglite, create the engine + connection, and setup data.

    Returns (manager, engine, connection, url).
    """
    cfg = PGliteConfig(use_tcp=False)
    mgr = PGliteManager(config=cfg)
    mgr.start()
    time.sleep(2)

    socket_dir = os.path.dirname(cfg.socket_path)
    url = get_engine_url(socket_dir)

    engine = create_engine(url, poolclass=StaticPool)
    conn = engine.connect()
    conn.connection.dbapi_connection.autocommit = True

    setup_rentals(conn)
    write_stellar_config(url)
    return mgr, engine, conn, url


def run_interactive(engine):
    """Interactive REPL that wraps Stellar commands."""
    from stellar.app import Stellar

    app = Stellar(engine=engine)

    print("\nStellar interactive shell  (type 'help' for commands)")
    print("=" * 55)

    while True:
        try:
            line = input("stellar> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue

        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else None

        try:
            if cmd in ("quit", "exit"):
                break
            elif cmd == "help":
                print("  snapshot [name]  - take a snapshot")
                print("  restore  [name]  - restore (default: latest)")
                print("  list             - list snapshots")
                print("  remove   <name>  - remove a snapshot")
                print("  show             - show rentals data")
                print("  sql <query>      - execute SQL")
                print("  quit             - exit")
            elif cmd == "snapshot":
                name = arg or app.default_snapshot_name
                if app.get_snapshot(name):
                    print("Snapshot '%s' already exists" % name)
                else:
                    app.create_snapshot(name)
                    print("Snapshot '%s' created" % name)
            elif cmd == "restore":
                if arg:
                    snap = app.get_snapshot(arg)
                else:
                    snap = app.get_latest_snapshot()
                if not snap:
                    print("Snapshot not found")
                else:
                    if not snap.slaves_ready:
                        app.inline_slave_copy(snap)
                    app.restore(snap)
                    print("Restored '%s'" % snap.snapshot_name)
            elif cmd == "list":
                for s in app.get_snapshots():
                    print("  %s" % s.snapshot_name)
            elif cmd == "remove":
                if not arg:
                    print("Usage: remove <name>")
                else:
                    snap = app.get_snapshot(arg)
                    if not snap:
                        print("Snapshot '%s' not found" % arg)
                    else:
                        app.remove_snapshot(snap)
                        print("Removed '%s'" % arg)
            elif cmd == "show":
                with engine.connect() as c:
                    print("-- rentals.movies --")
                    for row in c.execute(text(
                        "SELECT * FROM rentals.movies ORDER BY id"
                    )):
                        print("  ", dict(row._mapping))
                    print("-- rentals.customers --")
                    for row in c.execute(text(
                        "SELECT * FROM rentals.customers ORDER BY id"
                    )):
                        print("  ", dict(row._mapping))
            elif cmd == "sql":
                if not arg:
                    print("Usage: sql <query>")
                else:
                    with engine.connect() as c:
                        result = c.execute(text(arg))
                        if result.returns_rows:
                            for row in result:
                                print("  ", dict(row._mapping))
                        else:
                            print("OK")
            else:
                print("Unknown command '%s' (type 'help')" % cmd)
        except Exception as exc:
            print("Error: %s" % exc)


def main():
    setup_only = "--setup" in sys.argv

    mgr, engine, conn, url = start_pglite_env()

    print("pglite is running")
    print("SQLAlchemy URL: %s" % url)
    print("stellar.yaml written")

    if setup_only:
        return mgr, engine

    try:
        run_interactive(engine)
    finally:
        print("Shutting down pglite...")
        conn.close()
        engine.dispose()
        mgr.stop()


if __name__ == "__main__":
    main()
