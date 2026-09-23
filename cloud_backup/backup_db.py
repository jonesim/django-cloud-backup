import datetime
import os
import subprocess
import urllib.parse
from tempfile import NamedTemporaryFile, TemporaryFile

import requests

from .base_backup import BaseBackup
from .compression import decompress
from .db_tiers import (DAILY, DB_FILE_EXTENSIONS, DELETE_APP, DUMP_EXTENSION, HOURLY,  # noqa: F401 re-export
                       LEGACY_STAMP, MONTHLY, TIER_DIRS, backup_time, hourly_dir, hourly_name, list_tier, tier_of,
                       tier_prefixes)
from .encryption import decrypt_in_place, encrypt_file
from .prune_backups import PruneBackups
from .sql_functions import create_extensions, delete_table, get_schema_extensions

EXTENSIONS_KEY = 'extensions'
# Google Drive's limit on one appProperty, key plus value - the smallest of the backends
MAX_METADATA_ITEM_BYTES = 124


def get_ip_address():
    try:
        ip_response = requests.get('https://api.ipify.org/')
        if ip_response.status_code == 200 and len(ip_response.text) < 16:
            ip_address = ip_response.text.replace('.', '_')
        else:
            ip_address = 'fail'
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.HTTPError):
        ip_address = 'exception'
    return ip_address


class DatabaseUploadError(Exception):
    pass


class DatabaseBackupError(Exception):
    pass


class BackupDb(BaseBackup):

    def __init__(self, storage, backup_dir, database, local_backup_dir, logger, schema=None,
                 table=None, exclude_tables=None, exclude_table_data=None, config=None):
        super().__init__(storage, backup_dir, logger, config=config)
        self.postgres_backup = PostgresBackup(database, self.logger, schema, table,
                                              exclude_tables=exclude_tables,
                                              exclude_table_data=exclude_table_data,
                                              encryption_key=self.encryption_key)
        self.local_backup_dir = local_backup_dir

    def backup_db_to_storage(self):
        metadata = {'ip_address': get_ip_address()}
        if self.postgres_backup.table:
            metadata['schema'] = self.postgres_backup.schema
            metadata['table'] = self.postgres_backup.table
            base_name = f'table_{self.postgres_backup.table}'
        elif self.postgres_backup.schema:
            metadata['schema'] = self.postgres_backup.schema
            metadata.update(self.extensions_metadata(self.postgres_backup.schema))
            base_name = f'schema_{self.postgres_backup.schema}'
        else:
            base_name = 'db'
        # one clock for both the file name and the date partition it goes in
        now = datetime.datetime.today()
        if self.config.db_tiers:
            upload_folder = self.storage.ensure_folder(hourly_dir(now), parent=self.base_backup_dir)
            filename = hourly_name(base_name, now)
        else:
            upload_folder = self.base_backup_dir
            filename = f'{base_name}_{now.strftime(LEGACY_STAMP)}.{DUMP_EXTENSION}'
        # only the unique name is wanted - backup_db writes to <name>.dump, so the placeholder
        # file itself is closed and removed rather than left open for the life of the process
        backup_stream = NamedTemporaryFile(delete=False)
        backup_stream.close()
        os.remove(backup_stream.name)
        backup_filename = None
        upload_filename = None
        try:
            backup_filename = self.postgres_backup.backup_db('', backup_stream.name)
            if self.encryption_key is not None:
                # encrypt to a second temp file so check_upload can verify the storage
                # backend's hash against the ciphertext it actually received
                upload_filename = backup_filename + '.enc-tmp'
                metadata['md5'] = encrypt_file(backup_filename, upload_filename, self.encryption_key)
                metadata['encrypted'] = '1'
                os.remove(backup_filename)
            else:
                upload_filename = backup_filename
                # stored alongside the file so destinations without a server-side md5
                # (multipart S3, Azure) can still deduplicate and verify
                metadata['md5'] = self.md5sum(backup_filename)
            self.logger.info('Copying backup to storage')
            with open(upload_filename, 'rb') as compressed_file:
                stored_file = self.storage.upload(upload_folder, filename, compressed_file,
                                                  metadata=metadata,
                                                  lock_days=self.storage.lock_days('db'))
            if not self.check_upload(stored_file, upload_filename):
                raise DatabaseUploadError
        finally:
            for temp_file in (backup_filename, upload_filename):
                if temp_file and os.path.exists(temp_file):
                    os.remove(temp_file)

    def restore_db_from_storage(self, file_id=None, file_name=None):
        if file_id:
            file_info = self.storage.get_file(file_id)
        else:
            file_info = self.storage.find_file(self.base_backup_dir, file_name)
        local_name = self.storage.download(file_info, local_folder=self.local_backup_dir)
        metadata = file_info['metadata']
        if metadata.get('table'):
            delete_table(metadata['schema'], metadata['table'])
        elif metadata.get('schema') and metadata.get(EXTENSIONS_KEY):
            # a schema dump does not recreate its extensions, which a dropped schema or a
            # fresh database will not have
            self.warn_extensions_elsewhere(
                metadata['schema'], create_extensions(metadata['schema'], metadata[EXTENSIONS_KEY].split(',')))
        self.postgres_backup.restore_db(os.path.join(self.local_backup_dir, local_name))

    def extensions_metadata(self, schema):
        """The schema's extensions for the dump's metadata, so a restore can create them
        first - pg_dump -n leaves them out of the dump."""
        extensions = ','.join(get_schema_extensions(schema))
        if not extensions:
            return {}
        if len(EXTENSIONS_KEY) + len(extensions.encode()) > MAX_METADATA_ITEM_BYTES:
            self.logger.warning(f'Extensions of schema {schema} not recorded with the dump, too long for the '
                                f'metadata: {extensions}')
            return {}
        return {EXTENSIONS_KEY: extensions}

    def warn_extensions_elsewhere(self, schema, elsewhere):
        for extension, other_schema in elsewhere.items():
            self.logger.warning(f'Extension {extension} is installed in schema {other_schema}, not {schema} - '
                                f'objects in the dump that use it are likely to fail to restore')

    def tier_prefixes(self):
        """{tier: key prefix} for lifecycle-rule reporting, or None when this config does
        not use the lifecycle-managed tiers."""
        if not self.config.db_tiers:
            return None
        return tier_prefixes(self.base_backup_dir['id'])

    def tier_policy(self):
        """The lifecycle-rule arguments for storage.protection_info() - where each tier
        lives and how long it is meant to be kept. In app-delete mode the tiers are meant
        to have no lifecycle rules, so measuring them against the rules would report every
        tier as unprotected - no tier prefixes are passed, the same choice storage_setup
        makes, and protection_rows() adds a Deletion row saying what does the deleting."""
        if not self.config.db_tiers or self.config.db_tier_delete == DELETE_APP:
            return {'tier_prefixes': None, 'expire_days': None, 'purge_days': None}
        return {'tier_prefixes': self.tier_prefixes(), 'expire_days': self.config.db_tier_expire_days,
                'purge_days': self.config.db_tier_purge_days}

    def protection_rows(self):
        """The backup page's protection table: storage.protection_info plus the rows
        explained by the config rather than the destination."""
        from .storage_setup import deletion_row
        rows = self.storage.protection_info(**self.tier_policy())
        row = deletion_row(self.config, self.storage)
        if row:
            rows.append(row)
        return rows

    def get_db_backup_files(self, deleted=False, metadata_filter=None):
        # the flat listing is delimited, so it picks up dumps written before tiering was
        # turned on without ever seeing the tier folders
        files = self.storage.list_files(self.base_backup_dir, metadata_filter=metadata_filter,
                                        deleted=deleted, include_metadata=True)
        if self.config.db_tiers and not deleted:
            files += self.get_tiered_backup_files(metadata_filter)
            for f in files:
                self.set_tier_info(f)
        return sorted((f for f in files if f['name'].endswith(DB_FILE_EXTENSIONS)),
                      key=lambda f: f['created'], reverse=True)

    def get_tiered_backup_files(self, metadata_filter=None):
        files = []
        for tier in (DAILY, MONTHLY):
            # a sub-folder per server, plus any copies at the root from before that
            files += [f for _server, f in list_tier(self.storage, self.base_backup_dir, tier, include_metadata=True)
                      if self.storage.matches_metadata(f, metadata_filter)]
        if self.config.db_tier_hourly_days:
            # listing the day folders in the window is cheaper than walking the whole
            # hourly tier, which would read metadata for days nobody is going to see
            today = datetime.date.today()
            for offset in range(self.config.db_tier_hourly_days + 1):
                day_folder = self.storage.ensure_folder(hourly_dir(today - datetime.timedelta(days=offset)),
                                                        parent=self.base_backup_dir)
                files += self.storage.list_files(day_folder, metadata_filter=metadata_filter,
                                                 include_metadata=True)
        else:
            hourly_folder = self.storage.ensure_folder(HOURLY, parent=self.base_backup_dir)
            files += [f for _path, f in self.storage.walk(hourly_folder, include_metadata=True)
                      if self.storage.matches_metadata(f, metadata_filter)]
        return files

    def set_tier_info(self, f):
        """Label the tier from the key and make 'created' the time the dump was taken -
        for a promoted copy the storage timestamp is when the copy ran, a day or a month
        after the dump itself."""
        f['metadata'] = f.get('metadata') or {}
        f['metadata']['tier'] = tier_of(f['id'], self.base_backup_dir['id']) or ''
        taken = None
        stamp = f['metadata'].get('backup_time')
        if stamp:
            try:
                taken = datetime.datetime.fromisoformat(stamp)
            except ValueError:
                pass
        if taken is None:
            taken = backup_time(f['name'])
        if taken is not None:
            f['created'] = taken

    def get_latest_db_backup(self):
        files = self.get_db_backup_files()
        if len(files) > 0:
            return files[0]

    def prune_old_backups(self, recipe):
        backups = self.get_db_backup_files(metadata_filter={'ip_address': get_ip_address()})
        backup_dict = {b['created']: b for b in backups}
        pb = PruneBackups(backup_dict)
        removal = pb.backups_to_remove(recipe)
        for k in removal:
            try:
                self.storage.delete(removal[k]['id'])
            except Exception as e:
                # deletes may be intentionally blocked (object lock, delete-less
                # credentials) - the backup itself has already succeeded
                self.logger.warning(f'Could not delete old backup {removal[k]["name"]}: {e}')


class PostgresBackup:

    def __init__(self, database, logger, schema=None, table=None, exclude_tables=None, exclude_table_data=None,
                 encryption_key=None):
        self.logger = logger
        self.schema = schema
        self.table = table
        self.encryption_key = encryption_key
        self.exclude_tables = exclude_tables or []
        self.exclude_table_data = exclude_table_data or []
        self.connection_string = (f'postgresql://{database["USER"]}:{urllib.parse.quote(database["PASSWORD"])}'
                                  f'@{database["HOST"]}/{database["NAME"]}')

    def psql(self, commands):
        return subprocess.call(['psql', '-d',  self.connection_string] + commands)

    def restore_db(self, backup_file):
        decrypt_in_place(backup_file, self.encryption_key)
        if backup_file.endswith('.' + DUMP_EXTENSION):
            return_code = subprocess.call(['pg_restore', '-d', self.connection_string, '--clean', '--if-exists',
                                           backup_file])
            command = 'pg_restore'
            os.remove(backup_file)
        else:
            decompressed_name = decompress(backup_file)
            return_code = self.psql(['-f', decompressed_name])
            command = 'psql'
            os.remove(decompressed_name)
        if return_code != 0:
            # not fatal - pg_restore exits non-zero for ignorable warnings under --clean --if-exists,
            # but the restore may equally have failed outright, so make it visible
            self.logger.warning(f'{command} exited with code {return_code}. Check the output above to confirm the '
                                f'restore completed as expected.')
        return return_code

    def backup_db(self, backup_local_db_dir, filename):
        self.logger.info('Creating backup file ' + filename)
        if backup_local_db_dir:
            if not os.path.exists(backup_local_db_dir):
                os.makedirs(backup_local_db_dir)
            backup_path = backup_local_db_dir + '/' + filename
        else:
            backup_path = filename
        commands = ['pg_dump', '-Fc', '-d', self.connection_string]
        if self.table:
            self.logger.info(f'Backing up table {self.schema}.{self.table}')
            commands += ['-a', '-t', f'{self.schema}.{self.table}']
        elif self.schema:
            self.logger.info(f'Backing up schema {self.schema}')
            commands += ['-n', self.schema]
        else:
            self.logger.info(f'Backing up database')
        for t in self.exclude_tables:
            commands += [f'--exclude-table={t}']
        for t in self.exclude_table_data:
            commands += [f'--exclude-table-data={t}']
        dump_path = backup_path + '.' + DUMP_EXTENSION
        try:
            # stderr goes to a temp file rather than a pipe so a noisy dump cannot fill the pipe
            # buffer and deadlock while we are still reading stdout
            with TemporaryFile() as error_file:
                with open(dump_path, 'wb') as output:
                    dump_process = subprocess.Popen(commands, stdout=subprocess.PIPE, stderr=error_file)
                    for chunk in iter(lambda: dump_process.stdout.read(1024 * 1024), b''):
                        output.write(chunk)
                    dump_process.wait()
                error_file.seek(0)
                errors = error_file.read().decode(errors='replace').strip()
            if dump_process.returncode != 0:
                if errors:
                    self.logger.error(errors)
                raise DatabaseBackupError(f'pg_dump failed with exit code {dump_process.returncode}')
            if errors:
                self.logger.warning(errors)
        except BaseException:
            # never leave a partial dump behind for the caller to upload as if it were valid
            if os.path.exists(dump_path):
                os.remove(dump_path)
            raise
        return dump_path
