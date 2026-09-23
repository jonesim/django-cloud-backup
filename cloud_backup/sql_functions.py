import re

from django.db import connection

SCHEMA_NAME_RE = re.compile(r'^[a-z_][a-z0-9_]*$')


def get_schemas():
    with connection.cursor() as cursor:
        # cursor.execute('SELECT schema.schema_name, Count(tables.table_name)'
        #                'FROM information_schema.schemata as schema '
        #        'Left Join information_schema.tables as tables on tables.table_schema = schema.schema_name '
        #                    'GROUP BY schema.schema_name')

        cursor.execute(
            'SELECT pg_catalog.pg_namespace.nspname, pg_size_pretty(SUM(pg_relation_size(pg_catalog.pg_class.oid))) '
            'FROM pg_catalog.pg_class '
            'LEFT JOIN pg_catalog.pg_namespace ON relnamespace = pg_catalog.pg_namespace.oid '
            'GROUP BY pg_catalog.pg_namespace.nspname'
        )
        return [schema for schema in cursor.fetchall() if not schema[0].startswith('pg_')
                and schema[0] not in ['information_schema']]


def get_schema_tables(schema):
    with connection.cursor() as cursor:
        cursor.execute(
            f" SELECT relname , pg_relation_size(relfilenode), reltuples FROM pg_catalog.pg_class "
            f"LEFT JOIN pg_catalog.pg_namespace ON relnamespace = pg_catalog.pg_namespace.oid "
            f"WHERE pg_catalog.pg_namespace.nspname = 'public' AND relkind='r' "
        )
        return cursor.fetchall()


def get_table_column_names(schema, table_name):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT column_name from INFORMATION_SCHEMA.COLUMNS WHERE "
                       f"table_name='{table_name}' and table_schema='{schema}'")
        return [c[0] for c in cursor.fetchall()]


def get_table_data(schema, table_name):
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT * from {schema}."{table_name}"')
        return cursor.fetchall()


def delete_table(schema, table_name):
    with connection.cursor() as cursor:
        cursor.execute(f'DELETE from {schema}.{table_name}')


def get_schema_extensions(schema):
    """Names of the extensions installed in schema. A DROP SCHEMA ... CASCADE removes them
    along with it, and a pg_dump -n of the schema does not recreate them, so a restore of
    that dump fails on anything that uses them (e.g. a gin_trgm_ops index)."""
    with connection.cursor() as cursor:
        cursor.execute('SELECT e.extname FROM pg_catalog.pg_extension e '
                       'JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace '
                       'WHERE n.nspname = %s ORDER BY e.extname', [schema])
        return [row[0] for row in cursor.fetchall()]


def quote_identifier(name):
    """name as a quoted SQL identifier - safe for any name, so names read back from a dump's
    stored metadata need no validating. Neither driver is a dependency, hence not sql.Identifier."""
    if not name or '\0' in name:
        raise ValueError(f'Invalid identifier: {name!r}')
    return '"' + name.replace('"', '""') + '"'


def create_extensions(schema, extensions):
    """Create each extension in schema unless it is already installed. Returns
    {extension: schema} for those installed in a different schema, which IF NOT EXISTS
    leaves where they are - a dump that refers to schema.<type> will still fail on them."""
    if not extensions:
        return {}
    quoted_schema = quote_identifier(schema)
    quoted = [quote_identifier(extension) for extension in extensions]
    with connection.cursor() as cursor:
        cursor.execute('SELECT e.extname, n.nspname FROM pg_catalog.pg_extension e '
                       'JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace '
                       'WHERE e.extname = ANY(%s)', [list(extensions)])
        elsewhere = {name: nspname for name, nspname in cursor.fetchall() if nspname != schema}
        # a fresh database may not have the schema yet - pg_restore then only warns that it exists
        cursor.execute(f'CREATE SCHEMA IF NOT EXISTS {quoted_schema}')
        for extension in quoted:
            cursor.execute(f'CREATE EXTENSION IF NOT EXISTS {extension} SCHEMA {quoted_schema}')
    return elsewhere
