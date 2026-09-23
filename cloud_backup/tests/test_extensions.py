"""A schema dump (pg_dump -n) leaves out the schema's extensions and DROP SCHEMA ... CASCADE
removes them, so a drop-restore used to fail on anything built on one (a gin_trgm_ops index).
Run from a host project: ``python manage.py test cloud_backup``. Needs the database, and the
hstore contrib extension, which the postgres docker images ship."""
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import SimpleTestCase, TestCase

from cloud_backup.backup_db import BackupDb, EXTENSIONS_KEY
from cloud_backup.config import BackupConfig
from cloud_backup.sql_functions import create_extensions, get_schema_extensions, quote_identifier
from cloud_backup.tasks import ajax_restore

SCHEMA = 'cloud_backup_ext_test'


def make_backup_db(file_info=None):
    storage = MagicMock()
    storage.ensure_folder.return_value = {'id': 'db', 'name': 'db'}
    storage.get_file.return_value = file_info
    storage.download.return_value = 'dump.dump'
    backup_db = BackupDb(storage, 'db', {'USER': 'u', 'PASSWORD': 'p', 'HOST': 'h', 'NAME': 'n'}, '/tmp',
                         MagicMock(), config=BackupConfig())
    backup_db.postgres_backup.restore_db = MagicMock()
    return backup_db


class SchemaExtensionsTests(TestCase):

    def setUp(self):
        with connection.cursor() as cursor:
            # an extension is installed once per database, so one already in the host
            # project's database cannot be put in the test schema
            cursor.execute("SELECT 1 FROM pg_catalog.pg_extension WHERE extname = 'hstore'")
            if cursor.fetchone():
                self.skipTest('hstore is already installed in this database')
            cursor.execute(f'CREATE SCHEMA "{SCHEMA}"')
            cursor.execute(f'CREATE EXTENSION hstore SCHEMA "{SCHEMA}"')

    def drop_schema(self):
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA "{SCHEMA}" CASCADE')

    def test_lists_only_the_schemas_extensions(self):
        self.assertEqual(get_schema_extensions(SCHEMA), ['hstore'])
        self.assertNotIn('hstore', get_schema_extensions('public'))

    def test_create_after_drop(self):
        self.drop_schema()
        self.assertEqual(get_schema_extensions(SCHEMA), [])
        # the schema is created too - a fresh database will not have it
        create_extensions(SCHEMA, ['hstore'])
        self.assertEqual(get_schema_extensions(SCHEMA), ['hstore'])

    def test_create_when_already_installed(self):
        create_extensions(SCHEMA, ['hstore'])
        self.assertEqual(get_schema_extensions(SCHEMA), ['hstore'])

    def test_ajax_restore_puts_back_what_the_drop_removed(self):
        with patch('cloud_backup.tasks.allowed_to_restore', return_value=True), \
                patch('cloud_backup.tasks.Backup') as backup:
            ajax_restore.apply(kwargs={'slug': {'pk': 'file-id', 'drop_schema': SCHEMA}}).get()
        self.assertEqual(get_schema_extensions(SCHEMA), ['hstore'])
        backup.return_value.get_backup_db.return_value.restore_db_from_storage.assert_called_once_with(
            file_id='file-id')

    def test_installed_in_another_schema_reported(self):
        self.assertEqual(create_extensions('public', ['hstore']), {'hstore': SCHEMA})
        self.assertEqual(get_schema_extensions(SCHEMA), ['hstore'])

    def test_restore_warns_about_extension_elsewhere(self):
        backup_db = make_backup_db({'metadata': {'schema': 'public', EXTENSIONS_KEY: 'hstore'}})
        backup_db.restore_db_from_storage(file_id='file-id')
        backup_db.logger.warning.assert_called_once()
        self.assertIn('hstore', backup_db.logger.warning.call_args[0][0])

    def test_failed_recreate_rolls_the_drop_back(self):
        with patch('cloud_backup.tasks.allowed_to_restore', return_value=True),                 patch('cloud_backup.tasks.Backup') as backup,                 patch('cloud_backup.tasks.create_extensions', side_effect=RuntimeError('not trusted')):
            with self.assertRaises(RuntimeError):
                ajax_restore.apply(kwargs={'slug': {'pk': 'file-id', 'drop_schema': SCHEMA}}).get()
        self.assertEqual(get_schema_extensions(SCHEMA), ['hstore'])
        backup.return_value.get_backup_db.return_value.restore_db_from_storage.assert_not_called()

    def test_backup_records_extensions(self):
        self.assertEqual(make_backup_db().extensions_metadata(SCHEMA), {EXTENSIONS_KEY: 'hstore'})
        self.assertEqual(make_backup_db().extensions_metadata('no_such_schema'), {})


class ExtensionsMetadataTests(SimpleTestCase):

    def test_quote_identifier(self):
        self.assertEqual(quote_identifier('uuid-ossp'), '"uuid-ossp"')
        self.assertEqual(quote_identifier('Tenant1'), '"Tenant1"')
        self.assertEqual(quote_identifier('pg_trgm"; DROP TABLE x; --'), '"pg_trgm""; DROP TABLE x; --"')
        for name in ('', 'a\0b'):
            with self.assertRaises(ValueError):
                quote_identifier(name)

    @patch('cloud_backup.backup_db.get_schema_extensions', return_value=['pg_trgm', 'uuid-ossp'])
    def test_joined_with_commas(self, _get):
        self.assertEqual(make_backup_db().extensions_metadata('public'), {EXTENSIONS_KEY: 'pg_trgm,uuid-ossp'})

    @patch('cloud_backup.backup_db.get_schema_extensions', return_value=[f'extension_{i}' for i in range(20)])
    def test_too_long_for_drive_is_left_out(self, _get):
        backup_db = make_backup_db()
        self.assertEqual(backup_db.extensions_metadata('public'), {})
        backup_db.logger.warning.assert_called_once()

    @patch('cloud_backup.backup_db.create_extensions', return_value={})
    def test_restore_creates_extensions_first(self, create):
        backup_db = make_backup_db({'metadata': {'schema': 'public', EXTENSIONS_KEY: 'pg_trgm,unaccent'}})
        backup_db.postgres_backup.restore_db.side_effect = lambda _f: create.assert_called_once_with(
            'public', ['pg_trgm', 'unaccent'])
        backup_db.restore_db_from_storage(file_id='file-id')
        backup_db.postgres_backup.restore_db.assert_called_once()

    @patch('cloud_backup.backup_db.create_extensions')
    def test_restore_without_extensions_metadata(self, create):
        # dumps made before the key existed, and whole-database dumps, which include the extensions
        make_backup_db({'metadata': {'schema': 'public'}}).restore_db_from_storage(file_id='file-id')
        make_backup_db({'metadata': {}}).restore_db_from_storage(file_id='file-id')
        create.assert_not_called()

    @patch('cloud_backup.backup_db.delete_table')
    @patch('cloud_backup.backup_db.create_extensions')
    def test_table_restore_does_not_create_extensions(self, create, _delete):
        make_backup_db({'metadata': {'schema': 'public', 'table': 't', EXTENSIONS_KEY: 'pg_trgm'}}
                       ).restore_db_from_storage(file_id='file-id')
        create.assert_not_called()
