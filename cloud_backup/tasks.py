import logging

from celery import shared_task
from django.db import connection, transaction

from .backup import Backup
from .config import config_at
from .sql_functions import SCHEMA_NAME_RE, create_extensions, get_schema_extensions
from .utils import allowed_to_restore, RESTORE_BLOCKED_MESSAGE

logger = logging.getLogger(__name__)


@shared_task
def backup(config=None):
    """Beat schedules can run several configs, e.g. kwargs={'config': 'staging'}"""
    Backup(config=config).backup_db_and_folders()


@shared_task
def backup_all_schemas(config=None):
    Backup(config=config).backup_db_and_folders(all_schemas=True)


@shared_task
def empty_trash(config=None):
    Backup(config=config).empty_trash()


@shared_task
def promote_db_tiers(config=None):
    """Copy the newest hourly database dump of each finished day into the daily tier, and
    the last daily dump of each finished month into the monthly tier. Schedule this daily
    whenever a config uses db_tiers - without it the hourly tier expires with nothing
    behind it."""
    Backup(config=config).promote_db_tiers()


@shared_task
def extend_retention(config=None):
    """Top up object-lock retention so every file backup keeps at least the configured
    min_days of protection. Schedule daily via beat (interval must be shorter than
    min_days)."""
    Backup(config=config).extend_file_retention()


class StateLogger:

    def __init__(self, task):
        self.task = task

    def _log(self, log, text):
        log(text)
        self.task.update_state(state='PROGRESS', meta={'message': text})

    def info(self, text):
        self._log(logger.info, text)

    def warning(self, text):
        self._log(logger.warning, text)

    def error(self, text):
        self._log(logger.error, text)


try:
    from ajax_helpers.utils import ajax_command

    # none of these three may gain a config parameter: django-modals runs a task whose
    # signature has one synchronously, just to read the modal's display config. The web UI
    # puts the config in the slug instead, as its index in config_names()

    @shared_task(bind=True)
    def ajax_backup(self, **kwargs):
        task_kwargs = dict(kwargs['slug'] if 'slug' in kwargs else kwargs)
        config = config_at(task_kwargs.pop('config', None))
        # slug values are strings, so boolean kwargs (include_db-False, all_schemas-True)
        # must be converted before reaching backup_db_and_folders
        task_kwargs = {k: v == 'True' if v in ('True', 'False') else v for k, v in task_kwargs.items()}
        if 'backup_dir' in task_kwargs:
            task_kwargs['backup_dir'] = int(task_kwargs['backup_dir'])
        Backup(StateLogger(self), config=config).backup_db_and_folders(**task_kwargs)
        return {'commands': [ajax_command('message', text='Backup Complete'), ajax_command('reload')]}

    @shared_task(bind=True)
    def ajax_restore(self, *, slug, **_kwargs):
        if not allowed_to_restore():
            return {'commands': [ajax_command('message', text=RESTORE_BLOCKED_MESSAGE)]}
        drop_schema = slug.get('drop_schema')
        if drop_schema:
            if not SCHEMA_NAME_RE.match(drop_schema):
                raise ValueError(f'Invalid schema name for drop_schema: {drop_schema!r}')
            # the CASCADE drops the schema's extensions too, and dumps made before they were
            # recorded in the metadata cannot put them back. One transaction, so an extension
            # that cannot be created again rolls the drop back rather than leaving the schema
            # empty - pg_restore runs on its own connection, after the commit
            with transaction.atomic():
                extensions = get_schema_extensions(drop_schema)
                with connection.cursor() as cursor:
                    cursor.execute(f'DROP SCHEMA "{drop_schema}" CASCADE')
                    cursor.execute(f'CREATE SCHEMA "{drop_schema}"')
                create_extensions(drop_schema, extensions)
        backup = Backup(StateLogger(self), config=config_at(slug.get('config')))
        backup.get_backup_db().restore_db_from_storage(file_id=slug['pk'])
        return {'commands': [ajax_command('message', text='Restore Complete'), ajax_command('reload')]}

    @shared_task(bind=True)
    def ajax_verify_files(self, *, slug, **_kwargs):
        backup = Backup(StateLogger(self), config=config_at(slug.get('config')))
        # the index is into this config's file_sources, not the default config's
        source = backup.config.file_sources[int(slug['backup_dir'])]
        results = backup.get_file_backup(source.kind).verify_folder(source.source, source.dest_name)
        summary = (f"{results['matched']} matched, {len(results['changed'])} changed, "
                   f"{len(results['missing'])} missing from the source")
        if results['no_checksum']:
            summary += f", {len(results['no_checksum'])} without a stored checksum"
        problems = results['changed'] + results['missing']
        if problems:
            listed = problems[:10]
            summary += ': ' + ', '.join(listed)
            if len(problems) > len(listed):
                summary += f' and {len(problems) - len(listed)} more'
        return {'commands': [ajax_command('message', text=f'Verify complete - {summary}')]}

except ModuleNotFoundError:
    pass
