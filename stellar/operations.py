import logging
import re
import warnings

from sqlalchemy import text, MetaData
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SAWarning

logger = logging.getLogger(__name__)


SUPPORTED_DIALECTS = ("postgresql", "mysql")


class NotSupportedDatabase(Exception):
    pass


def _quote_ident(name):
    return '"%s"' % name.replace('"', '""')


def _postgres_insert_column_info(raw_conn, schema, table_name):
    try:
        rows = raw_conn.execute(
            text(
                "SELECT a.attname, a.attgenerated, a.attidentity "
                "FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND c.relname = :table "
                "AND a.attnum > 0 AND NOT a.attisdropped "
                "ORDER BY a.attnum"
            ),
            {"schema": schema, "table": table_name},
        ).fetchall()
    except Exception:
        rows = raw_conn.execute(
            text(
                "SELECT a.attname, '' AS attgenerated, '' AS attidentity "
                "FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND c.relname = :table "
                "AND a.attnum > 0 AND NOT a.attisdropped "
                "ORDER BY a.attnum"
            ),
            {"schema": schema, "table": table_name},
        ).fetchall()
    copyable = [row[0] for row in rows if not row[1]]
    generated = [row[0] for row in rows if row[1]]
    has_always_identity = any(row[2] == "a" for row in rows)
    return copyable, generated, has_always_identity


def get_engine_url(raw_conn, database):
    url = str(raw_conn.engine.url)
    if url.count("/") == 3 and url.endswith("/"):
        return "%s%s" % (url, database)
    else:
        if not url.endswith("/"):
            url += "/"
        return "%s/%s" % ("/".join(url.split("/")[0:-2]), database)


def replace_database_url(url, database):
    return make_url(url).set(database=database)


def _get_pid_column(raw_conn):
    server_version = raw_conn.execute(text("SHOW server_version;")).first()[0]
    version_string = re.search(r"^(\d+\.\d+)", server_version).group(0)
    version = [int(x) for x in version_string.split(".")]
    return "pid" if version >= [9, 2] else "procpid"


def terminate_database_connections(raw_conn, database):
    logger.debug("terminate_database_connections(%r)", database)
    if raw_conn.engine.dialect.name != "postgresql":
        return
    pid_col = _get_pid_column(raw_conn)
    raw_conn.execute(
        text(
            "SELECT pg_terminate_backend(a.%s) FROM pg_stat_activity a "
            "WHERE a.datname = :name AND a.%s <> pg_backend_pid()" % (pid_col, pid_col)
        ),
        {"name": database},
    )


def create_database(raw_conn, database):
    logger.debug("create_database(%r)", database)
    if raw_conn.engine.dialect.name == "postgresql":
        raw_conn.execute(text('CREATE SCHEMA IF NOT EXISTS "%s"' % database))
    elif raw_conn.engine.dialect.name == "mysql":
        import sqlalchemy_utils

        sqlalchemy_utils.functions.create_database(get_engine_url(raw_conn, database))
    else:
        raise NotSupportedDatabase()


def copy_database(raw_conn, from_database, to_database):
    logger.debug("copy_database(%r, %r)", from_database, to_database)

    if raw_conn.engine.dialect.name == "postgresql":
        raw_conn.execute(text('CREATE SCHEMA IF NOT EXISTS "%s"' % to_database))

        src_meta = MetaData(schema=from_database)
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"Can't validate argument 'dialect_options'",
                category=SAWarning,
            )
            src_meta.reflect(bind=raw_conn.engine)

        dst_meta = MetaData(schema=to_database)
        for table in src_meta.tables.values():
            table.to_metadata(dst_meta, schema=to_database)
        dst_meta.create_all(bind=raw_conn.engine)

        fk_rows = raw_conn.execute(
            text(
                "SELECT rel.relname, c.conname, pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c "
                "JOIN pg_class rel ON rel.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = rel.relnamespace "
                "WHERE c.contype = 'f' AND n.nspname = :schema"
            ),
            {"schema": to_database},
        ).fetchall()
        for relname, conname, _condef in fk_rows:
            raw_conn.execute(
                text(
                    "ALTER TABLE %s.%s DROP CONSTRAINT %s"
                    % (
                        _quote_ident(to_database),
                        _quote_ident(relname),
                        _quote_ident(conname),
                    )
                )
            )

        for src_table in src_meta.tables.values():
            copyable, _generated, has_always_identity = _postgres_insert_column_info(
                raw_conn, to_database, src_table.name
            )
            if not copyable:
                continue
            col_list = ", ".join(_quote_ident(c) for c in copyable)
            overriding = " OVERRIDING SYSTEM VALUE" if has_always_identity else ""
            raw_conn.execute(
                text(
                    "INSERT INTO %s.%s (%s)%s SELECT %s FROM %s.%s"
                    % (
                        _quote_ident(to_database),
                        _quote_ident(src_table.name),
                        col_list,
                        overriding,
                        col_list,
                        _quote_ident(from_database),
                        _quote_ident(src_table.name),
                    )
                )
            )

        for relname, conname, condef in fk_rows:
            raw_conn.execute(
                text(
                    "ALTER TABLE %s.%s ADD CONSTRAINT %s %s"
                    % (
                        _quote_ident(to_database),
                        _quote_ident(relname),
                        _quote_ident(conname),
                        condef,
                    )
                )
            )

        for src_table in src_meta.tables.values():
            for col in src_table.columns:
                seq = raw_conn.execute(
                    text(
                        "SELECT pg_get_serial_sequence('%s.%s', '%s')"
                        % (to_database, src_table.name, col.name)
                    )
                ).scalar()
                if seq:
                    raw_conn.execute(
                        text(
                            "SELECT setval('%s', COALESCE("
                            '(SELECT MAX("%s") FROM "%s"."%s"), 1))'
                            % (seq, col.name, to_database, src_table.name)
                        )
                    )
    elif raw_conn.engine.dialect.name == "mysql":
        create_database(raw_conn, to_database)
        for row in raw_conn.execute(text("SHOW TABLES in %s;" % from_database)):
            raw_conn.execute(
                text(
                    "CREATE TABLE %s.%s LIKE %s.%s"
                    % (to_database, row[0], from_database, row[0])
                )
            )
            raw_conn.execute(
                text("ALTER TABLE %s.%s DISABLE KEYS" % (to_database, row[0]))
            )
            raw_conn.execute(
                text(
                    "INSERT INTO %s.%s SELECT * FROM %s.%s"
                    % (to_database, row[0], from_database, row[0])
                )
            )
            raw_conn.execute(
                text("ALTER TABLE %s.%s ENABLE KEYS" % (to_database, row[0]))
            )
    else:
        raise NotSupportedDatabase()


def database_exists(raw_conn, database):
    logger.debug("database_exists(%r)", database)
    if raw_conn.engine.dialect.name == "postgresql":
        result = raw_conn.execute(
            text("SELECT 1 FROM pg_namespace WHERE nspname = :name"), {"name": database}
        ).first()
        return result is not None
    elif raw_conn.engine.dialect.name == "mysql":
        import sqlalchemy_utils

        return sqlalchemy_utils.functions.database_exists(
            get_engine_url(raw_conn, database)
        )
    else:
        raise NotSupportedDatabase()


def remove_database(raw_conn, database):
    logger.debug("remove_database(%r)", database)
    if raw_conn.engine.dialect.name == "postgresql":
        raw_conn.execute(text('DROP SCHEMA IF EXISTS "%s" CASCADE' % database))
    elif raw_conn.engine.dialect.name == "mysql":
        import sqlalchemy_utils

        terminate_database_connections(raw_conn, database)
        sqlalchemy_utils.functions.drop_database(get_engine_url(raw_conn, database))
    else:
        raise NotSupportedDatabase()


def rename_database(raw_conn, from_database, to_database):
    logger.debug("rename_database(%r, %r)", from_database, to_database)
    if raw_conn.engine.dialect.name == "postgresql":
        raw_conn.execute(
            text('ALTER SCHEMA "%s" RENAME TO "%s"' % (from_database, to_database))
        )
    elif raw_conn.engine.dialect.name == "mysql":
        create_database(raw_conn, to_database)
        for row in raw_conn.execute(text("SHOW TABLES in %s;" % from_database)):
            raw_conn.execute(
                text(
                    "RENAME TABLE %s.%s TO %s.%s;"
                    % (from_database, row[0], to_database, row[0])
                )
            )
        remove_database(raw_conn, from_database)
    else:
        raise NotSupportedDatabase()


def list_of_databases(raw_conn):
    logger.debug("list_of_databases()")
    if raw_conn.engine.dialect.name == "postgresql":
        return [
            row[0]
            for row in raw_conn.execute(
                text(
                    "SELECT nspname FROM pg_namespace "
                    "WHERE nspname NOT IN "
                    "('information_schema', 'pg_catalog', 'pg_toast', 'public') "
                    "AND nspname NOT LIKE 'pg_%%'"
                )
            )
        ]
    elif raw_conn.engine.dialect.name == "mysql":
        return [row[0] for row in raw_conn.execute(text("SHOW DATABASES"))]
    else:
        raise NotSupportedDatabase()


def list_of_cluster_databases(raw_conn):
    logger.debug("list_of_cluster_databases()")
    if raw_conn.engine.dialect.name != "postgresql":
        return list_of_databases(raw_conn)
    return [
        row[0]
        for row in raw_conn.execute(
            text(
                "SELECT datname FROM pg_database "
                "WHERE datistemplate = false "
                "ORDER BY datname"
            )
        )
    ]


def cluster_database_exists(raw_conn, database):
    logger.debug("cluster_database_exists(%r)", database)
    if raw_conn.engine.dialect.name != "postgresql":
        return database_exists(raw_conn, database)
    result = raw_conn.execute(
        text("SELECT 1 FROM pg_database WHERE datname = :name"),
        {"name": database},
    ).first()
    return result is not None


def create_cluster_database(raw_conn, database, template=None):
    logger.debug("create_cluster_database(%r, template=%r)", database, template)
    if raw_conn.engine.dialect.name != "postgresql":
        return create_database(raw_conn, database)
    if cluster_database_exists(raw_conn, database):
        return
    if template:
        raw_conn.execute(
            text(
                "CREATE DATABASE %s TEMPLATE %s"
                % (_quote_ident(database), _quote_ident(template))
            )
        )
    else:
        raw_conn.execute(text("CREATE DATABASE %s" % _quote_ident(database)))


def copy_cluster_database(raw_conn, from_database, to_database):
    logger.debug("copy_cluster_database(%r, %r)", from_database, to_database)
    if raw_conn.engine.dialect.name != "postgresql":
        return copy_database(raw_conn, from_database, to_database)
    terminate_database_connections(raw_conn, from_database)
    terminate_database_connections(raw_conn, to_database)
    if cluster_database_exists(raw_conn, to_database):
        raw_conn.execute(text("DROP DATABASE %s" % _quote_ident(to_database)))
    raw_conn.execute(
        text(
            "CREATE DATABASE %s TEMPLATE %s"
            % (_quote_ident(to_database), _quote_ident(from_database))
        )
    )


def remove_cluster_database(raw_conn, database):
    logger.debug("remove_cluster_database(%r)", database)
    if raw_conn.engine.dialect.name != "postgresql":
        return remove_database(raw_conn, database)
    if not cluster_database_exists(raw_conn, database):
        return
    terminate_database_connections(raw_conn, database)
    raw_conn.execute(text("DROP DATABASE IF EXISTS %s" % _quote_ident(database)))


def rename_cluster_database(raw_conn, from_database, to_database):
    logger.debug("rename_cluster_database(%r, %r)", from_database, to_database)
    if raw_conn.engine.dialect.name != "postgresql":
        return rename_database(raw_conn, from_database, to_database)
    terminate_database_connections(raw_conn, from_database)
    terminate_database_connections(raw_conn, to_database)
    if cluster_database_exists(raw_conn, to_database):
        raw_conn.execute(text("DROP DATABASE %s" % _quote_ident(to_database)))
    raw_conn.execute(
        text(
            "CREATE DATABASE %s TEMPLATE %s"
            % (_quote_ident(to_database), _quote_ident(from_database))
        )
    )
    raw_conn.execute(text("DROP DATABASE %s" % _quote_ident(from_database)))
