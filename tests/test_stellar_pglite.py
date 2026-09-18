"""
Extensive integration tests for Stellar running against a real
py-pglite PostgreSQL instance.  No mocking or monkey-patching.
"""

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.usefixtures("_clean_schemas")


# ── helpers ──────────────────────────────────────────────────────────


def movie_titles(engine):
    with engine.connect() as c:
        return [
            r[0]
            for r in c.execute(text("SELECT title FROM rentals.movies ORDER BY id"))
        ]


def customer_names(engine):
    with engine.connect() as c:
        return [
            r[0]
            for r in c.execute(text("SELECT name FROM rentals.customers ORDER BY id"))
        ]


def movie_count(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT count(*) FROM rentals.movies")).scalar()


def customer_count(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT count(*) FROM rentals.customers")).scalar()


def insert_movie(engine, title, genre="Test", year=2025):
    with engine.connect() as c:
        c.execute(
            text(
                "INSERT INTO rentals.movies (title, genre, release_year) "
                "VALUES (:t, :g, :y)"
            ),
            {"t": title, "g": genre, "y": year},
        )


def delete_all_customers(engine):
    with engine.connect() as c:
        c.execute(text("DELETE FROM rentals.customers"))


# ── Operations layer ─────────────────────────────────────────────────


class TestOperations:
    """Test low-level database (schema) operations."""

    def test_create_database(self, pg_engine, stellar_config, rentals_schema):
        from stellar.operations import create_database, database_exists

        conn = pg_engine.connect()
        create_database(conn, "test_schema_op")
        assert database_exists(conn, "test_schema_op")
        conn.close()

    def test_database_exists_false(self, pg_engine, stellar_config):
        from stellar.operations import database_exists

        conn = pg_engine.connect()
        assert not database_exists(conn, "nonexistent_schema_xyz")
        conn.close()

    def test_remove_database(self, pg_engine, stellar_config, rentals_schema):
        from stellar.operations import (
            create_database,
            database_exists,
            remove_database,
        )

        conn = pg_engine.connect()
        create_database(conn, "to_remove")
        assert database_exists(conn, "to_remove")
        remove_database(conn, "to_remove")
        assert not database_exists(conn, "to_remove")
        conn.close()

    def test_rename_database(self, pg_engine, stellar_config, rentals_schema):
        from stellar.operations import (
            create_database,
            database_exists,
            rename_database,
        )

        conn = pg_engine.connect()
        create_database(conn, "old_name")
        rename_database(conn, "old_name", "new_name")
        assert not database_exists(conn, "old_name")
        assert database_exists(conn, "new_name")
        conn.close()

    def test_list_of_databases(self, pg_engine, stellar_config, rentals_schema):
        from stellar.operations import create_database, list_of_databases

        conn = pg_engine.connect()
        create_database(conn, "list_test_a")
        create_database(conn, "list_test_b")
        dbs = list_of_databases(conn)
        assert "list_test_a" in dbs
        assert "list_test_b" in dbs
        assert "pg_catalog" not in dbs
        assert "public" not in dbs
        conn.close()

    def test_copy_database_copies_structure_and_data(
        self, pg_engine, stellar_config, rentals_schema
    ):
        from stellar.operations import copy_database, database_exists

        conn = pg_engine.connect()
        copy_database(conn, "rentals", "rentals_copy")
        assert database_exists(conn, "rentals_copy")

        rows = conn.execute(
            text("SELECT title FROM rentals_copy.movies ORDER BY id")
        ).fetchall()
        assert [r[0] for r in rows] == [
            "The Matrix",
            "Inception",
            "The Godfather",
        ]

        rows = conn.execute(
            text("SELECT name FROM rentals_copy.customers ORDER BY id")
        ).fetchall()
        assert [r[0] for r in rows] == ["Alice Johnson", "Bob Smith"]
        conn.close()

    def test_copy_database_preserves_sequences(
        self, pg_engine, stellar_config, rentals_schema
    ):
        from stellar.operations import copy_database

        conn = pg_engine.connect()
        copy_database(conn, "rentals", "seq_test")

        conn.execute(
            text(
                "INSERT INTO seq_test.movies (title, genre, release_year) "
                "VALUES ('New', 'Test', 2025)"
            )
        )
        new_id = conn.execute(
            text("SELECT id FROM seq_test.movies WHERE title = 'New'")
        ).scalar()
        assert new_id == 4
        conn.close()

    def test_copy_database_preserves_constraints(
        self, pg_engine, stellar_config, rentals_schema
    ):
        from stellar.operations import copy_database

        conn = pg_engine.connect()
        copy_database(conn, "rentals", "constraint_test")

        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO constraint_test.movies "
                    "(id, title, genre, release_year) "
                    "VALUES (1, 'Dup', 'Test', 2025)"
                )
            )
        conn.close()

    def test_terminate_database_connections_does_not_error(
        self, pg_engine, stellar_config, rentals_schema
    ):
        from stellar.operations import terminate_database_connections

        conn = pg_engine.connect()
        terminate_database_connections(conn, "rentals")
        conn.close()

    def test_get_pid_column(self, pg_engine, stellar_config):
        from stellar.operations import _get_pid_column

        conn = pg_engine.connect()
        col = _get_pid_column(conn)
        assert col == "pid"
        conn.close()


# ── Models ───────────────────────────────────────────────────────────


class TestModels:
    def test_unique_hash(self):
        from stellar.models import get_unique_hash

        h1 = get_unique_hash()
        h2 = get_unique_hash()
        assert h1 != h2
        assert len(h1) == 32

    def test_table_name_generation(self):
        from stellar.models import Snapshot, Table

        snap = Snapshot(
            snapshot_name="snap",
            project_name="proj",
            hash="a" * 32,
        )
        tbl = Table(table_name="mydb", snapshot=snap)
        master = tbl.get_table_name("master")
        slave = tbl.get_table_name("slave")
        assert master.startswith("stellar_")
        assert slave.startswith("stellar_")
        assert master != slave
        assert len(master) == 24
        assert len(slave) == 24

    def test_table_name_old_format(self):
        from stellar.models import Snapshot, Table

        snap = Snapshot(
            snapshot_name="snap",
            project_name="proj",
            hash="b" * 32,
        )
        tbl = Table(table_name="mydb", snapshot=snap)
        old = tbl.get_table_name("master", old=True)
        assert old == "stellar_mydb_%s_master" % ("b" * 32)

    def test_snapshot_slaves_ready(self):
        from stellar.models import Snapshot

        s = Snapshot(snapshot_name="s", project_name="p")
        assert s.slaves_ready is True
        s.worker_pid = 123
        assert s.slaves_ready is False
        s.worker_pid = None
        assert s.slaves_ready is True


# ── Stellar App ──────────────────────────────────────────────────────


class TestStellarSnapshot:
    def test_create_snapshot(self, app, pg_engine):
        app.create_snapshot("snap1")
        snap = app.get_snapshot("snap1")
        assert snap is not None
        assert snap.snapshot_name == "snap1"
        assert len(snap.tables) == 1
        assert snap.tables[0].table_name == "rentals"

    def test_snapshot_preserves_data(self, app, pg_engine):
        app.create_snapshot("data_snap")
        insert_movie(pg_engine, "Extra")
        assert movie_count(pg_engine) == 4

        snap = app.get_snapshot("data_snap")
        if not snap.slaves_ready:
            app.inline_slave_copy(snap)
        app.restore(snap)
        assert movie_count(pg_engine) == 3
        assert movie_titles(pg_engine) == [
            "The Matrix",
            "Inception",
            "The Godfather",
        ]

    def test_snapshot_preserves_customers(self, app, pg_engine):
        app.create_snapshot("cust_snap")
        delete_all_customers(pg_engine)
        assert customer_count(pg_engine) == 0

        snap = app.get_snapshot("cust_snap")
        if not snap.slaves_ready:
            app.inline_slave_copy(snap)
        app.restore(snap)
        assert customer_count(pg_engine) == 2
        assert customer_names(pg_engine) == ["Alice Johnson", "Bob Smith"]

    def test_get_existing_snapshot(self, app):
        app.create_snapshot("dup")
        assert app.get_snapshot("dup") is not None
        assert app.get_snapshot("nonexistent") is None

    def test_default_snapshot_name(self, app):
        assert app.default_snapshot_name == "snap1"
        app.create_snapshot("snap1")
        assert app.default_snapshot_name == "snap2"
        app.create_snapshot("snap2")
        assert app.default_snapshot_name == "snap3"


class TestStellarRestore:
    def test_restore_latest(self, app, pg_engine):
        app.create_snapshot("latest_snap")
        insert_movie(pg_engine, "Added")
        assert movie_count(pg_engine) == 4

        snap = app.get_latest_snapshot()
        if not snap.slaves_ready:
            app.inline_slave_copy(snap)
        app.restore(snap)
        assert movie_count(pg_engine) == 3

    def test_restore_by_name(self, app, pg_engine):
        app.create_snapshot("named")
        insert_movie(pg_engine, "X")
        snap = app.get_snapshot("named")
        if not snap.slaves_ready:
            app.inline_slave_copy(snap)
        app.restore(snap)
        assert "X" not in movie_titles(pg_engine)

    def test_restore_preserves_sequence(self, app, pg_engine):
        app.create_snapshot("seq_snap")
        snap = app.get_snapshot("seq_snap")
        if not snap.slaves_ready:
            app.inline_slave_copy(snap)
        app.restore(snap)
        insert_movie(pg_engine, "PostRestore")
        new_id = (
            pg_engine.connect()
            .execute(text("SELECT id FROM rentals.movies WHERE title='PostRestore'"))
            .scalar()
        )
        assert new_id == 4

    def test_restore_then_snapshot_again(self, app, pg_engine):
        app.create_snapshot("first")
        insert_movie(pg_engine, "middle")

        snap = app.get_snapshot("first")
        if not snap.slaves_ready:
            app.inline_slave_copy(snap)
        app.restore(snap)
        assert movie_count(pg_engine) == 3

        app.create_snapshot("second")
        insert_movie(pg_engine, "after_second")
        snap2 = app.get_snapshot("second")
        if not snap2.slaves_ready:
            app.inline_slave_copy(snap2)
        app.restore(snap2)
        assert movie_count(pg_engine) == 3


class TestStellarList:
    def test_no_snapshots(self, app):
        assert app.get_snapshots() == []

    def test_list_returns_all(self, app):
        app.create_snapshot("a")
        app.create_snapshot("b")
        names = [s.snapshot_name for s in app.get_snapshots()]
        assert "a" in names
        assert "b" in names
        assert len(names) == 2

    def test_list_order_newest_first(self, app):
        app.create_snapshot("old")
        app.create_snapshot("new")
        names = [s.snapshot_name for s in app.get_snapshots()]
        assert names[0] == "new"
        assert names[1] == "old"

    def test_get_latest_snapshot(self, app):
        app.create_snapshot("first")
        app.create_snapshot("second")
        latest = app.get_latest_snapshot()
        assert latest.snapshot_name == "second"


class TestStellarRemove:
    def test_remove_snapshot(self, app):
        app.create_snapshot("to_del")
        snap = app.get_snapshot("to_del")
        app.remove_snapshot(snap)
        assert app.get_snapshot("to_del") is None

    def test_remove_cleans_schemas(self, app, pg_engine):
        app.create_snapshot("cleanup")
        snap = app.get_snapshot("cleanup")
        master = snap.tables[0].get_table_name("master")
        slave = snap.tables[0].get_table_name("slave")
        from stellar.operations import database_exists

        conn = pg_engine.connect()
        assert database_exists(conn, master)

        app.remove_snapshot(snap)
        assert not database_exists(conn, master)
        assert not database_exists(conn, slave)
        conn.close()

    def test_remove_nonexistent(self, app):
        assert app.get_snapshot("nope") is None


class TestStellarRename:
    def test_rename_snapshot(self, app):
        app.create_snapshot("orig")
        snap = app.get_snapshot("orig")
        app.rename_snapshot(snap, "renamed")
        assert app.get_snapshot("orig") is None
        assert app.get_snapshot("renamed") is not None


class TestStellarGarbageCollection:
    def test_delete_orphan_snapshots(self, app, pg_engine):
        from stellar.operations import create_database, database_exists

        conn = pg_engine.connect()
        create_database(conn, "stellar_orphan_abc")
        assert database_exists(conn, "stellar_orphan_abc")

        deleted = []
        app.delete_orphan_snapshots(after_delete=deleted.append)
        assert "stellar_orphan_abc" in deleted
        assert not database_exists(conn, "stellar_orphan_abc")
        conn.close()

    def test_gc_ignores_stellar_data(self, app, pg_engine):
        from stellar.operations import database_exists

        conn = pg_engine.connect()
        assert database_exists(conn, "stellar_data")
        deleted = []
        app.delete_orphan_snapshots(after_delete=deleted.append)
        assert "stellar_data" not in deleted
        assert database_exists(conn, "stellar_data")
        conn.close()


# ── Config ───────────────────────────────────────────────────────────


class TestConfig:
    def test_load_config(self, stellar_config, pg_url):
        from stellar.config import load_config

        cfg = load_config()
        assert cfg["project_name"] == "test_project"
        assert cfg["tracked_databases"] == ["rentals"]
        assert cfg["url"] == pg_url

    def test_missing_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from stellar.config import MissingConfig, load_config

        with pytest.raises(MissingConfig):
            load_config()

    def test_save_config(self, stellar_config):
        from stellar.config import load_config, save_config

        cfg = load_config()
        cfg["project_name"] = "changed"
        save_config(cfg)
        cfg2 = load_config()
        assert cfg2["project_name"] == "changed"


# ── End-to-end workflow ──────────────────────────────────────────────


class TestEndToEnd:
    def test_full_snapshot_modify_restore_cycle(self, app, pg_engine):
        assert movie_count(pg_engine) == 3
        assert customer_count(pg_engine) == 2

        app.create_snapshot("baseline")

        insert_movie(pg_engine, "Tenet", "Sci-Fi", 2020)
        insert_movie(pg_engine, "Dune", "Sci-Fi", 2021)
        delete_all_customers(pg_engine)
        assert movie_count(pg_engine) == 5
        assert customer_count(pg_engine) == 0

        snap = app.get_snapshot("baseline")
        if not snap.slaves_ready:
            app.inline_slave_copy(snap)
        app.restore(snap)

        assert movie_count(pg_engine) == 3
        assert customer_count(pg_engine) == 2
        assert movie_titles(pg_engine) == [
            "The Matrix",
            "Inception",
            "The Godfather",
        ]

    def test_multiple_snapshot_restore_cycles(self, app, pg_engine):
        app.create_snapshot("v1")
        insert_movie(pg_engine, "A")

        app.create_snapshot("v2")
        insert_movie(pg_engine, "B")
        assert movie_count(pg_engine) == 5

        snap_v2 = app.get_snapshot("v2")
        if not snap_v2.slaves_ready:
            app.inline_slave_copy(snap_v2)
        app.restore(snap_v2)
        assert movie_count(pg_engine) == 4

        snap_v1 = app.get_snapshot("v1")
        if not snap_v1.slaves_ready:
            app.inline_slave_copy(snap_v1)
        app.restore(snap_v1)
        assert movie_count(pg_engine) == 3

    def test_snapshot_after_restore(self, app, pg_engine):
        app.create_snapshot("original")
        insert_movie(pg_engine, "Extra")
        assert movie_count(pg_engine) == 4

        snap = app.get_snapshot("original")
        if not snap.slaves_ready:
            app.inline_slave_copy(snap)
        app.restore(snap)
        assert movie_count(pg_engine) == 3

        app.create_snapshot("after_restore")
        insert_movie(pg_engine, "After")
        assert movie_count(pg_engine) == 4

        snap2 = app.get_snapshot("after_restore")
        if not snap2.slaves_ready:
            app.inline_slave_copy(snap2)
        app.restore(snap2)
        assert movie_count(pg_engine) == 3


# ── Additional operations tests ──────────────────────────────────────


class TestCopyDatabaseAdvanced:
    """Extra copy tests for multi-column PKs, nullable columns, etc."""

    def test_copy_table_with_text_data(self, pg_engine, stellar_config, rentals_schema):
        from stellar.operations import copy_database

        conn = pg_engine.connect()
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS txt_src"))
        conn.execute(
            text("CREATE TABLE txt_src.docs " "(id SERIAL PRIMARY KEY, body TEXT)")
        )
        long_text = "x" * 5000
        conn.execute(
            text("INSERT INTO txt_src.docs (body) VALUES (:b)"), {"b": long_text}
        )
        copy_database(conn, "txt_src", "txt_dst")
        val = conn.execute(text("SELECT body FROM txt_dst.docs")).scalar()
        assert val == long_text
        conn.close()

    def test_copy_table_with_nulls(self, pg_engine, stellar_config, rentals_schema):
        from stellar.operations import copy_database

        conn = pg_engine.connect()
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS null_src"))
        conn.execute(
            text("CREATE TABLE null_src.t " "(id SERIAL PRIMARY KEY, a TEXT, b INT)")
        )
        conn.execute(
            text(
                "INSERT INTO null_src.t (a, b) VALUES "
                "('yes', 1), (NULL, NULL), ('no', 3)"
            )
        )
        copy_database(conn, "null_src", "null_dst")
        rows = conn.execute(text("SELECT a, b FROM null_dst.t ORDER BY id")).fetchall()
        assert rows[0] == ("yes", 1)
        assert rows[1] == (None, None)
        assert rows[2] == ("no", 3)
        conn.close()

    def test_copy_empty_table(self, pg_engine, stellar_config, rentals_schema):
        from stellar.operations import copy_database

        conn = pg_engine.connect()
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS empty_src"))
        conn.execute(text("CREATE TABLE empty_src.t (id SERIAL PRIMARY KEY, v TEXT)"))
        copy_database(conn, "empty_src", "empty_dst")
        cnt = conn.execute(text("SELECT count(*) FROM empty_dst.t")).scalar()
        assert cnt == 0
        conn.execute(text("INSERT INTO empty_dst.t (v) VALUES ('first')"))
        new_id = conn.execute(text("SELECT id FROM empty_dst.t")).scalar()
        assert new_id >= 1
        conn.close()

    def test_copy_table_with_defaults(self, pg_engine, stellar_config, rentals_schema):
        from stellar.operations import copy_database

        conn = pg_engine.connect()
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS def_src"))
        conn.execute(
            text(
                "CREATE TABLE def_src.cfg "
                "(id SERIAL PRIMARY KEY, active BOOLEAN DEFAULT TRUE, "
                "label TEXT DEFAULT 'untitled')"
            )
        )
        conn.execute(text("INSERT INTO def_src.cfg DEFAULT VALUES"))
        copy_database(conn, "def_src", "def_dst")
        row = conn.execute(text("SELECT active, label FROM def_dst.cfg")).first()
        assert row[0] is True
        assert row[1] == "untitled"
        conn.close()

    def test_copy_preserves_multiple_tables_independently(
        self, pg_engine, stellar_config, rentals_schema
    ):
        from stellar.operations import copy_database

        conn = pg_engine.connect()
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS ms"))
        conn.execute(text("CREATE TABLE ms.a (id SERIAL PRIMARY KEY, val INT)"))
        conn.execute(text("CREATE TABLE ms.b (id SERIAL PRIMARY KEY, val INT)"))
        conn.execute(text("INSERT INTO ms.a (val) VALUES (10), (20), (30)"))
        conn.execute(text("INSERT INTO ms.b (val) VALUES (100)"))
        copy_database(conn, "ms", "ms_copy")
        assert conn.execute(text("SELECT count(*) FROM ms_copy.a")).scalar() == 3
        assert conn.execute(text("SELECT count(*) FROM ms_copy.b")).scalar() == 1
        conn.close()

    def test_copy_database_with_cyclic_foreign_keys(self, pg_engine, stellar_config):
        from stellar.operations import copy_database

        conn = pg_engine.connect()
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS cyc"))
        conn.execute(text("CREATE TABLE cyc.organizations (" "id TEXT PRIMARY KEY)"))
        conn.execute(
            text(
                "CREATE TABLE cyc.bank_transactions ("
                "id TEXT PRIMARY KEY, "
                "organization_id TEXT NOT NULL "
                "REFERENCES cyc.organizations(id), "
                "expense_id TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE cyc.expenses ("
                "id TEXT PRIMARY KEY, "
                "organization_id TEXT NOT NULL "
                "REFERENCES cyc.organizations(id), "
                "bank_transaction_id TEXT "
                "REFERENCES cyc.bank_transactions(id))"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE cyc.bank_transactions "
                "ADD CONSTRAINT bank_tx_expense_fk "
                "FOREIGN KEY (expense_id) REFERENCES cyc.expenses(id)"
            )
        )
        conn.execute(text("INSERT INTO cyc.organizations (id) VALUES ('ORG1')"))
        conn.execute(
            text(
                "INSERT INTO cyc.bank_transactions "
                "(id, organization_id, expense_id) VALUES ('BT1', 'ORG1', NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO cyc.expenses "
                "(id, organization_id, bank_transaction_id) "
                "VALUES ('EX1', 'ORG1', 'BT1')"
            )
        )
        conn.execute(
            text("UPDATE cyc.bank_transactions SET expense_id = 'EX1' WHERE id = 'BT1'")
        )
        copy_database(conn, "cyc", "cyc_copy")
        assert (
            conn.execute(text("SELECT count(*) FROM cyc_copy.organizations")).scalar()
            == 1
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM cyc_copy.bank_transactions")
            ).scalar()
            == 1
        )
        assert (
            conn.execute(text("SELECT count(*) FROM cyc_copy.expenses")).scalar() == 1
        )
        with pytest.raises(Exception):
            conn.execute(
                text(
                    "INSERT INTO cyc_copy.bank_transactions (id, organization_id) "
                    "VALUES ('BT2', 'MISSING')"
                )
            )
        conn.close()


class TestListDatabasesFiltering:
    def test_excludes_system_schemas(self, pg_engine, stellar_config, rentals_schema):
        from stellar.operations import list_of_databases

        conn = pg_engine.connect()
        dbs = list_of_databases(conn)
        for sys_schema in ("pg_catalog", "information_schema", "pg_toast", "public"):
            assert sys_schema not in dbs
        conn.close()

    def test_includes_user_schemas(self, pg_engine, stellar_config, rentals_schema):
        from stellar.operations import create_database, list_of_databases

        conn = pg_engine.connect()
        create_database(conn, "user_schema_1")
        create_database(conn, "user_schema_2")
        dbs = list_of_databases(conn)
        assert "user_schema_1" in dbs
        assert "user_schema_2" in dbs
        conn.close()


# ── Snapshot data-integrity edge cases ───────────────────────────────


class TestSnapshotIntegrity:
    """Deeper checks on data fidelity across snapshot/restore."""

    def test_restore_does_not_leak_extra_rows(self, app, pg_engine):
        app.create_snapshot("clean")
        insert_movie(pg_engine, "A")
        insert_movie(pg_engine, "B")
        insert_movie(pg_engine, "C")
        assert movie_count(pg_engine) == 6

        snap = app.get_snapshot("clean")
        if not snap.slaves_ready:
            app.inline_slave_copy(snap)
        app.restore(snap)
        assert movie_count(pg_engine) == 3

    def test_restore_updates_are_reverted(self, app, pg_engine):
        app.create_snapshot("before_update")
        with pg_engine.connect() as c:
            c.execute(text("UPDATE rentals.movies SET title = 'CHANGED' WHERE id = 1"))
        snap = app.get_snapshot("before_update")
        if not snap.slaves_ready:
            app.inline_slave_copy(snap)
        app.restore(snap)
        with pg_engine.connect() as c:
            title = c.execute(
                text("SELECT title FROM rentals.movies WHERE id = 1")
            ).scalar()
        assert title == "The Matrix"

    def test_snapshot_isolates_from_later_changes(self, app, pg_engine):
        app.create_snapshot("iso")
        insert_movie(pg_engine, "LaterMovie")
        snap = app.get_snapshot("iso")
        master_name = snap.tables[0].get_table_name("master")
        with pg_engine.connect() as c:
            cnt = c.execute(
                text('SELECT count(*) FROM "%s".movies' % master_name)
            ).scalar()
        assert cnt == 3

    def test_restore_twice_is_idempotent(self, app, pg_engine):
        app.create_snapshot("idem")
        insert_movie(pg_engine, "Extra")

        snap = app.get_snapshot("idem")
        if not snap.slaves_ready:
            app.inline_slave_copy(snap)
        app.restore(snap)
        assert movie_count(pg_engine) == 3

        snap = app.get_snapshot("idem")
        if not snap.slaves_ready:
            app.inline_slave_copy(snap)
        app.restore(snap)
        assert movie_count(pg_engine) == 3


# ── Replace command ──────────────────────────────────────────────────


class TestStellarReplace:
    """Test the replace workflow (remove + recreate with same name)."""

    def test_replace_snapshot(self, app, pg_engine):
        app.create_snapshot("rep")
        insert_movie(pg_engine, "New1")
        assert movie_count(pg_engine) == 4

        snap = app.get_snapshot("rep")
        app.remove_snapshot(snap)
        app.create_snapshot("rep")

        snap2 = app.get_snapshot("rep")
        assert snap2 is not None
        assert movie_count(pg_engine) == 4

        delete_all_customers(pg_engine)
        if not snap2.slaves_ready:
            app.inline_slave_copy(snap2)
        app.restore(snap2)
        assert customer_count(pg_engine) == 2
        assert movie_count(pg_engine) == 4


# ── Launcher (start_pglite) tests ───────────────────────────────────


class TestLauncherSetup:
    """Tests for the start_pglite helper functions."""

    def test_setup_rentals_creates_schema_and_tables(self, pg_engine):
        import start_pglite

        with pg_engine.connect() as c:
            start_pglite.setup_rentals(c)
        with pg_engine.connect() as c:
            tables = c.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'rentals' ORDER BY tablename"
                )
            ).fetchall()
            names = [r[0] for r in tables]
        assert "movies" in names
        assert "customers" in names

    def test_setup_rentals_inserts_sample_data(self, pg_engine):
        import start_pglite

        with pg_engine.connect() as c:
            start_pglite.setup_rentals(c)
        with pg_engine.connect() as c:
            mcnt = c.execute(text("SELECT count(*) FROM rentals.movies")).scalar()
            ccnt = c.execute(text("SELECT count(*) FROM rentals.customers")).scalar()
        assert mcnt == 3
        assert ccnt == 2

    def test_setup_rentals_is_idempotent(self, pg_engine):
        import start_pglite

        with pg_engine.connect() as c:
            start_pglite.setup_rentals(c)
            start_pglite.setup_rentals(c)
        with pg_engine.connect() as c:
            cnt = c.execute(text("SELECT count(*) FROM rentals.movies")).scalar()
        assert cnt == 3

    def test_write_stellar_config(self, tmp_path, monkeypatch, pg_url):
        import start_pglite

        monkeypatch.chdir(tmp_path)
        start_pglite.write_stellar_config(pg_url)
        from stellar.config import load_config

        cfg = load_config()
        assert cfg["url"] == pg_url
        assert cfg["stellar_url"] == pg_url
        assert cfg["tracked_databases"] == ["rentals"]

    def test_full_launcher_snapshot_restore(
        self, pg_engine, tmp_path, monkeypatch, pg_url
    ):
        """Simulate the full launcher workflow in-process."""
        import start_pglite

        monkeypatch.chdir(tmp_path)
        with pg_engine.connect() as c:
            start_pglite.setup_rentals(c)
        start_pglite.write_stellar_config(pg_url)

        from stellar.app import Stellar

        app = Stellar(engine=pg_engine)
        app.create_snapshot("launcher_snap")

        with pg_engine.connect() as c:
            c.execute(text("DELETE FROM rentals.movies WHERE title = 'Inception'"))
        assert movie_count(pg_engine) == 2

        snap = app.get_snapshot("launcher_snap")
        if not snap.slaves_ready:
            app.inline_slave_copy(snap)
        app.restore(snap)
        assert movie_count(pg_engine) == 3
