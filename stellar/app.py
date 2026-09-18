import logging
import os
import sys
import click
from functools import partial

from .config import load_config
from .models import Snapshot, Table, Base
from .operations import (
    copy_database,
    copy_cluster_database,
    create_database,
    create_cluster_database,
    database_exists,
    cluster_database_exists,
    remove_database,
    remove_cluster_database,
    rename_database,
    rename_cluster_database,
    terminate_database_connections,
    list_of_databases,
    list_of_cluster_databases,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import StaticPool
from psutil import pid_exists


__version__ = "0.4.6"
logger = logging.getLogger(__name__)


class Operations(object):
    def __init__(self, raw_connection, config, use_cluster=False):
        self.use_cluster = use_cluster
        self.terminate_database_connections = partial(
            terminate_database_connections, raw_connection
        )
        if use_cluster:
            self.create_database = partial(create_cluster_database, raw_connection)
            self.copy_database = partial(copy_cluster_database, raw_connection)
            self.database_exists = partial(cluster_database_exists, raw_connection)
            self.rename_database = partial(rename_cluster_database, raw_connection)
            self.remove_database = partial(remove_cluster_database, raw_connection)
            self.list_of_databases = partial(list_of_cluster_databases, raw_connection)
        else:
            self.create_database = partial(create_database, raw_connection)
            self.copy_database = partial(copy_database, raw_connection)
            self.database_exists = partial(database_exists, raw_connection)
            self.rename_database = partial(rename_database, raw_connection)
            self.remove_database = partial(remove_database, raw_connection)
            self.list_of_databases = partial(list_of_databases, raw_connection)


class Stellar(object):
    def __init__(self, engine=None):
        logger.debug("Initialized Stellar()")
        self._engine_override = engine
        self.load_config()
        self.init_database()

    def load_config(self):
        self.config = load_config()
        logging.basicConfig(level=self.config["logging"])

    def init_database(self):
        self.use_cluster = False
        if self._engine_override is not None:
            self.raw_db = self._engine_override
            self.db = self._engine_override
        else:
            parsed = make_url(self.config["url"])
            if parsed.get_backend_name() == "postgresql":
                self.use_cluster = True
                tracked = parsed.database
                admin_name = "template1" if tracked == "postgres" else "postgres"
                admin_url = parsed.set(database=admin_name)
                if self.config["stellar_url"] == self.config["url"]:
                    stellar_url = parsed.set(database="stellar_data")
                else:
                    stellar_url = make_url(self.config["stellar_url"])
                self.raw_db = create_engine(
                    admin_url, echo=False, isolation_level="AUTOCOMMIT"
                )
                self.db = create_engine(stellar_url, echo=False)
            else:
                same_url = self.config["url"] == self.config["stellar_url"]
                self.raw_db = create_engine(
                    self.config["url"],
                    echo=False,
                    poolclass=StaticPool if same_url else None,
                )
                if same_url:
                    self.db = self.raw_db
                else:
                    self.db = create_engine(self.config["stellar_url"], echo=False)
        self.raw_conn = self.raw_db.connect()
        try:
            self.raw_conn.connection.dbapi_connection.autocommit = True
        except Exception:
            logger.info("Could not set autocommit on dbapi connection")
        self.operations = Operations(
            self.raw_conn, self.config, use_cluster=self.use_cluster
        )
        self.db.session = sessionmaker(bind=self.db)()
        self.raw_db.session = sessionmaker(bind=self.raw_db)()
        self.create_stellar_database()
        self.create_stellar_tables()

        # logger.getLogger('sqlalchemy.engine').setLevel(logger.WARN)

    def create_stellar_database(self):
        existed = self.operations.database_exists("stellar_data")
        if not existed:
            self.operations.create_database("stellar_data")
        if self.use_cluster:
            with self.db.connect() as conn:
                conn.execute(text('CREATE SCHEMA IF NOT EXISTS "stellar_data"'))
                conn.commit()
        return not existed

    def create_stellar_tables(self):
        Base.metadata.create_all(self.db)
        self.db.session.commit()

    def get_snapshot(self, snapshot_name):
        return (
            self.db.session.query(Snapshot)
            .filter(
                Snapshot.snapshot_name == snapshot_name,
                Snapshot.project_name == self.config["project_name"],
            )
            .first()
        )

    def get_snapshots(self):
        return (
            self.db.session.query(Snapshot)
            .filter(Snapshot.project_name == self.config["project_name"])
            .order_by(Snapshot.created_at.desc())
            .all()
        )

    def get_latest_snapshot(self):
        return (
            self.db.session.query(Snapshot)
            .filter(Snapshot.project_name == self.config["project_name"])
            .order_by(Snapshot.created_at.desc())
            .first()
        )

    def _copy_sources(self):
        if self.use_cluster:
            return [make_url(self.config["url"]).database]
        return self.config["tracked_databases"]

    def create_snapshot(self, snapshot_name, before_copy=None):
        snapshot = Snapshot(
            snapshot_name=snapshot_name, project_name=self.config["project_name"]
        )
        self.db.session.add(snapshot)
        self.db.session.flush()

        for table_name in self._copy_sources():
            if before_copy:
                before_copy(table_name)
            table = Table(table_name=table_name, snapshot=snapshot)
            logger.debug(
                "Copying %s to %s" % (table_name, table.get_table_name("master"))
            )
            self.operations.copy_database(table_name, table.get_table_name("master"))
            self.db.session.add(table)
        self.db.session.commit()

        self.start_background_slave_copy(snapshot)

    def remove_snapshot(self, snapshot):
        for table in snapshot.tables:
            try:
                self.operations.remove_database(table.get_table_name("master"))
            except ProgrammingError:
                pass
            try:
                self.operations.remove_database(table.get_table_name("slave"))
            except ProgrammingError:
                pass
            self.db.session.delete(table)
        self.db.session.delete(snapshot)
        self.db.session.commit()

    def rename_snapshot(self, snapshot, new_name):
        snapshot.snapshot_name = new_name
        self.db.session.commit()

    def restore(self, snapshot):
        for table in snapshot.tables:
            click.echo("Restoring database %s" % table.table_name)
            if not self.operations.database_exists(table.get_table_name("slave")):
                click.echo(
                    "Database %s does not exist." % table.get_table_name("slave")
                )
                sys.exit(1)
            try:
                self.operations.remove_database(table.table_name)
            except ProgrammingError:
                logger.warn("Database %s does not exist." % table.table_name)
            self.operations.rename_database(
                table.get_table_name("slave"), table.table_name
            )
        snapshot.worker_pid = 1
        self.db.session.commit()

        self.start_background_slave_copy(snapshot)

    def start_background_slave_copy(self, snapshot):
        logger.debug("Starting background slave copy")
        self.inline_slave_copy(snapshot)

    def inline_slave_copy(self, snapshot):
        for table in snapshot.tables:
            self.operations.copy_database(
                table.get_table_name("master"), table.get_table_name("slave")
            )
        snapshot.worker_pid = None
        self.db.session.commit()

    def is_copy_process_running(self, snapshot):
        return pid_exists(snapshot.worker_pid)

    def is_old_database(self):
        for snapshot in self.db.session.query(Snapshot):
            for table in snapshot.tables:
                for postfix in ("master", "slave"):
                    old_name = table.get_table_name(postfix=postfix, old=True)
                    if self.operations.database_exists(old_name):
                        return True
        return False

    def update_database_names_to_new_version(self, after_rename=None):
        for snapshot in self.db.session.query(Snapshot):
            for table in snapshot.tables:
                for postfix in ("master", "slave"):
                    old_name = table.get_table_name(postfix=postfix, old=True)
                    new_name = table.get_table_name(postfix=postfix, old=False)
                    if self.operations.database_exists(old_name):
                        self.operations.rename_database(old_name, new_name)
                        if after_rename:
                            after_rename(old_name, new_name)

    def delete_orphan_snapshots(self, after_delete=None):
        stellar_databases = set()
        for snapshot in self.db.session.query(Snapshot):
            for table in snapshot.tables:
                stellar_databases.add(table.get_table_name("master"))
                stellar_databases.add(table.get_table_name("slave"))

        databases = set(self.operations.list_of_databases())

        for database in filter(
            lambda database: (
                database.startswith("stellar_") and database != "stellar_data"
            ),
            (databases - stellar_databases),
        ):
            self.operations.remove_database(database)
            if after_delete:
                after_delete(database)

    @property
    def default_snapshot_name(self):
        n = 1
        while (
            self.db.session.query(Snapshot)
            .filter(
                Snapshot.snapshot_name == "snap%d" % n,
                Snapshot.project_name == self.config["project_name"],
            )
            .count()
        ):
            n += 1
        return "snap%d" % n
