import datetime
import hashlib


class StorageFileNotFound(Exception):
    pass


class BackupStorage:
    """
    Interface for a backup destination. Implementations normalise files to plain dicts:

        {'id': str,          # backend reference - gdrive file id, s3 key, azure blob name
         'name': str,        # filename without any path
         'size': int,
         'hash': str,        # md5 hex digest where the backend can supply one, else None
         'created': datetime,   # naive local time, to match PruneBackups comparisons
         'modified': datetime,  # naive local time
         'metadata': dict,   # custom key/value pairs stored with the file
         'web_link': str}    # browser URL if the backend has one, else None

    Folders are opaque dict handles produced by ensure_folder/get_folder and must only
    be passed back to the storage that created them. Path arguments use '/' separators.
    """

    # list_files(deleted=True) and restore_deleted work: a real trash, or on a versioned
    # bucket the hidden previous versions, which are the same thing from the outside
    supports_trash = False
    # empty_trash purges it - only where the credential is meant to be able to delete
    supports_empty_trash = False
    lock = {}  # object-lock config where the backend supports it (currently only s3)

    def lock_days(self, kind):
        """Object-lock retention days configured for 'db' or 'file' uploads, or None
        when the backend has no object lock configured."""
        return self.lock.get(kind + '_days')

    def ensure_folder(self, path, parent=None):
        """Return a folder handle for path (relative to parent), creating it if needed."""
        raise NotImplementedError

    def get_folder(self, path, parent=None):
        """Return a folder handle or None if the folder does not exist."""
        raise NotImplementedError

    def list_files(self, folder, metadata_filter=None, deleted=False, include_metadata=False):
        """
        List files directly inside a folder as normalised dicts.
        :param folder: folder handle
        :param metadata_filter: dict of metadata key/values every returned file must match
        :param deleted: list trashed files instead - or on a versioned bucket the hidden
                        previous versions under the folder, recursively (backends without
                        either return [])
        :param include_metadata: guarantee 'metadata' is populated (may cost extra
                                 requests on backends that can't list metadata)
        """
        raise NotImplementedError

    def list_folders(self, folder):
        """List the sub-folders directly inside a folder as folder handles
        (each usable with list_files/list_folders)."""
        raise NotImplementedError

    def walk(self, folder, include_metadata=False):
        """Yield (relative_path, file_dict) for every file under folder, recursively.
        relative_path is '' for direct children, otherwise 'sub/folders'.
        include_metadata guarantees 'metadata' is populated (may cost extra requests
        on backends that can't list metadata)."""
        raise NotImplementedError

    def file_metadata(self, stored_file):
        """The metadata of a file dict from a listing made without include_metadata.
        Backends whose listings cannot carry metadata (S3: a HEAD per object) fetch it
        on first use and keep it on the dict, so a caller can list cheaply and pay for
        metadata only on the files it cannot judge from the listing alone."""
        return stored_file.get('metadata') or {}

    def get_file(self, file_id):
        """Return the normalised dict for a file reference, or raise StorageFileNotFound."""
        raise NotImplementedError

    def find_file(self, folder, name):
        """Return the normalised dict for a named file in a folder, or raise StorageFileNotFound."""
        raise NotImplementedError

    def upload(self, folder, name, stream, metadata=None, lock_days=None):
        """Upload a binary stream and return the normalised dict of the stored file.
        lock_days applies object-lock retention where the backend supports it and is
        ignored otherwise."""
        raise NotImplementedError

    def keep_version(self, stored_file, lock_days=None):
        """Preserve the current contents of a stored file so that an upload under the
        same name cannot destroy it. Backends use their cheapest native mechanism
        (server-side copy, snapshot or rename) - no data is re-transferred and no
        delete permission is needed. Returns a reference to the preserved copy where
        the backend has one."""
        raise NotImplementedError('This storage backend cannot keep file versions')

    def copy_stored_file(self, stored_file, folder, name, extra_metadata=None, lock_days=None,
                         lock_mode=None):
        """Server-side copy of an already stored file to another name/folder in the same
        destination - no download and no re-upload, so no egress. The file's metadata is
        preserved with extra_metadata merged over it. lock_days/lock_mode apply object-lock
        retention to the copy where the backend supports it. Returns the normalised dict of
        the new file."""
        raise NotImplementedError('This storage backend cannot copy stored files server-side')

    @staticmethod
    def version_name(name):
        return f'{name}.{datetime.datetime.today().strftime("%Y_%m_%d_%H_%M")}'

    def extend_retention(self, folder, min_days, workers=8):
        """Ensure every file under folder keeps at least min_days of object-lock
        retention. No-op (returns None) on backends without object lock."""
        return None

    def verify_upload(self, stored_file, local_path):
        """Check a completed upload against the local source file."""
        raise NotImplementedError

    def download(self, stored_file, local_folder=None):
        """Download a file. Returns the filename written when local_folder is given,
        otherwise a BytesIO of the contents."""
        raise NotImplementedError

    def delete(self, file_id):
        """Remove a file - to trash where supported, otherwise permanently."""
        raise NotImplementedError

    def restore_deleted(self, file_id):
        """Bring a file listed by list_files(deleted=True) back, by the id that listing
        gave it."""
        raise NotImplementedError('This storage backend does not support undelete')

    def empty_trash(self):
        """Permanently remove trashed files. No-op where there is no trash, or where
        purging it is the bucket's job (supports_empty_trash)."""

    def storage_info(self, folder=None):
        """
        Describe the destination for display.
        :return: {'name': location name, 'web_link': browser URL or None,
                  'used': bytes used or None, 'limit': bytes available or None}
        """
        raise NotImplementedError

    def destination_status(self, root=None):
        """
        Read-only check that the bucket/container/folder this storage points at exists
        and is visible to these credentials. Creates nothing.
        :param root: the backup root path, for backends whose destination is a folder
        :return: {'state': 'ok'|'missing'|'denied'|'unknown', plus the label/status/detail
                  of a protection_info row so it renders the same way}
        """
        return {'state': 'unknown', 'label': 'Destination', 'status': 'Unknown',
                'detail': 'this backend cannot check whether the destination exists'}

    def object_retention(self, file_id):
        """The object-lock retention on one stored file as
        {'mode': str, 'retain_until': datetime}, or None where it has none or the backend
        has no object lock."""
        return None

    def lifecycle_rules(self):
        """Expiry rules the destination applies by itself, as plain dicts
        {'id', 'prefix', 'days', 'noncurrent_days', 'delete_markers', 'abort_days'}, one
        per key prefix, or None where the backend has no such concept. Raises where they
        exist but cannot be read."""
        return None

    def list_versions(self, prefix):
        """Every object version and delete marker under a key prefix, as
        {'key', 'version_id', 'modified' (naive local), 'size', 'is_latest', 'marker'} -
        what the status check's version audit reads. None where the backend has no
        versions; raises where they exist but cannot be listed."""
        return None

    def versioned(self):
        """Whether the destination keeps previous versions of overwritten and deleted
        objects, meaning a plain delete only hides the current one. False where the
        backend has no such concept; raises where it cannot be read."""
        return False

    def protection_info(self, tier_prefixes=None, expire_days=None, purge_days=None):
        """
        Describe the destination's data-protection configuration (soft delete,
        versioning, WORM/immutability) for display. Costs a few extra API requests,
        so only call it for info pages, not during backups.
        :param tier_prefixes: {tier: key prefix} when the config uses lifecycle-managed db
                              tiers, so backends that have lifecycle rules can report on
                              each tier - with expire_days, both from BackupDb.tier_policy()
        :param expire_days: {tier: days it should be kept, None for indefinitely} to
                            report each tier's real rule against what was asked for
        :param purge_days: how long a hidden version should survive before the rule purges
                           it - a tier rule that purges sooner is reported
        :return: list of {'label': str,
                          'status': 'Enabled'|'Disabled'|'Suspended'|'Unknown',
                          'detail': str or None,
                          'folder': the key prefix the row is about, where it has one -
                                    rendered as its own column,
                          'action': 'fix'|'warn' on the rows that mean something has to be
                                    done, which is what sets the setup page's panel state.
                                    Absent on rows that are context,
                          'badge': optional bootstrap colour overriding the one the status
                                   text implies - a Disabled that is only a warning here}
        """
        return []

    @staticmethod
    def md5sum(filename, block_size=65536):
        file_hash = hashlib.md5()
        with open(filename, 'rb') as f:
            for block in iter(lambda: f.read(block_size), b''):
                file_hash.update(block)
        return file_hash.hexdigest()

    @staticmethod
    def to_local_naive(dt):
        """Convert an aware datetime to naive local time, matching how Google Drive
        timestamps have always been normalised (PruneBackups compares them with
        datetime.today())."""
        if dt is None:
            return None
        if dt.tzinfo is not None:
            dt = dt.astimezone(tz=None).replace(tzinfo=None)
        return dt

    @staticmethod
    def matches_metadata(file_dict, metadata_filter):
        if not metadata_filter:
            return True
        metadata = file_dict.get('metadata') or {}
        return all(metadata.get(k) == v for k, v in metadata_filter.items())
