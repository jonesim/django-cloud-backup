"""Azure blobs as a backup source: what a run costs when almost nothing has changed.

The destination is listed once, recursively, and the stored metadata - a HEAD per
object on S3 - is only fetched for the files the listing cannot settle. Skipped where
the azure SDK is not installed."""
import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from cloud_backup.storages.base import BackupStorage

try:
    from cloud_backup.backup_azure import BackupAzure
except ImportError:  # pragma: no cover - a host project without the azure extra
    BackupAzure = None

try:
    from cloud_backup.storages.s3 import S3Storage
except ImportError:  # pragma: no cover
    S3Storage = None


def md5(data):
    return hashlib.md5(data).hexdigest()


def blob(name, data, with_md5=True, etag='etag-1'):
    """A BlobProperties stand-in: Azure records Content-MD5 for single-request uploads
    and not for block-list ones (with_md5=False)."""
    return SimpleNamespace(name=name, size=len(data), etag=f'"{etag}"',
                           content_settings=SimpleNamespace(content_md5=bytes.fromhex(md5(data)) if with_md5 else None),
                           data=data)


class FakeContainer:

    def __init__(self, blobs):
        self.blobs = blobs

    def list_blobs(self, name_starts_with=''):
        return [b for b in self.blobs if b.name.startswith(name_starts_with)]

    def get_blob_client(self, name):
        data = next(b.data for b in self.blobs if b.name == name)
        client = MagicMock()
        client.download_blob.side_effect = lambda offset, length: MagicMock(readall=lambda: data[offset:offset + length])
        return client


class FakeStorage(BackupStorage):
    """An S3-like destination in memory: listings carry the hash of the stored bytes
    but no metadata, which costs a fetch per file (counted)."""

    def __init__(self, files=None):
        self.files = dict(files or {})     # key -> (data, metadata)
        self.walks = 0
        self.metadata_fetches = []
        self.uploads = []
        self.kept = []

    def lock_days(self, kind):
        return None

    def ensure_folder(self, path, parent=None):
        prefix = f"{parent['id']}/{path}" if parent else path
        return {'id': prefix.strip('/'), 'name': prefix.rsplit('/', 1)[-1], 'web_link': None}

    get_folder = ensure_folder

    def _file(self, key):
        data, _metadata = self.files[key]
        return {'id': key, 'name': key.rsplit('/', 1)[-1], 'size': len(data), 'hash': md5(data),
                'created': None, 'modified': None, 'metadata': {}, 'web_link': None}

    def walk(self, folder, include_metadata=False):
        self.walks += 1
        prefix = folder['id'] + '/'
        for key in sorted(self.files):
            if key.startswith(prefix):
                relative = key[len(prefix):]
                yield relative.rsplit('/', 1)[0] if '/' in relative else '', self._file(key)

    def file_metadata(self, stored_file):
        if not stored_file.get('metadata'):
            self.metadata_fetches.append(stored_file['id'])
            stored_file['metadata'] = dict(self.files[stored_file['id']][1])
        return stored_file['metadata']

    def upload(self, folder, name, stream, metadata=None, lock_days=None):
        key = f"{folder['id']}/{name}"
        data = stream.read()
        self.files[key] = (data, dict(metadata or {}))
        self.uploads.append(key)
        return self._file(key)

    def keep_version(self, stored_file, lock_days=None):
        self.kept.append(stored_file['id'])
        return stored_file['id'] + '.old'


@unittest.skipIf(BackupAzure is None, 'azure-storage-blob is not installed')
class BackupAzureRunCostTest(SimpleTestCase):

    SMALL = b'a small pdf'
    BIG = b'x' * 200          # "uploaded in blocks": Azure holds no md5 for it
    ROOT = 'backup/live'

    def backup(self, storage, blobs, changed_files='protect', encryption_key=None):
        config = SimpleNamespace(changed_files=changed_files, encryption_key=encryption_key)
        with patch('cloud_backup.backup_azure.container_client', return_value=FakeContainer(blobs)):
            return BackupAzure({'container': 'attachments'}, storage, self.ROOT, MagicMock(), config=config)

    def stored(self, *entries):
        """Files already in the backup, as (relative path, data, metadata)."""
        return {f'{self.ROOT}/files/{path}': (data, metadata) for path, data, metadata in entries}

    def test_unchanged_run_lists_once_and_fetches_no_metadata(self):
        storage = FakeStorage(self.stored(
            ('stock/a.pdf', self.SMALL, {'etag': 'e-a', 'md5': md5(self.SMALL)}),
            ('stock/deep/b.pdf', self.SMALL, {'etag': 'e-b', 'md5': md5(self.SMALL)}),
            ('contact/c.pdf', self.SMALL, {'etag': 'e-c', 'md5': md5(self.SMALL)})))
        blobs = [blob('files/stock/a.pdf', self.SMALL), blob('files/stock/deep/b.pdf', self.SMALL),
                 blob('files/contact/c.pdf', self.SMALL)]
        self.backup(storage, blobs).backup_folder('files', 'files')
        self.assertEqual(storage.walks, 1)            # three folders, one listing
        self.assertEqual(storage.metadata_fetches, [])  # settled by the listing's hashes
        self.assertEqual(storage.uploads, [])

    def test_blob_without_md5_needs_the_stored_etag(self):
        storage = FakeStorage(self.stored(('stock/big.bin', self.BIG, {'etag': 'e-big'})))
        blobs = [blob('files/stock/big.bin', self.BIG, with_md5=False, etag='e-big')]
        self.backup(storage, blobs).backup_folder('files', 'files')
        self.assertEqual(storage.metadata_fetches, [f'{self.ROOT}/files/stock/big.bin'])
        self.assertEqual(storage.uploads, [])

    def test_blob_without_md5_re_uploaded_to_azure_is_changed(self):
        # a new etag with no md5 to fall back on: protect mode refuses the overwrite
        storage = FakeStorage(self.stored(('stock/big.bin', self.BIG, {'etag': 'e-old'})))
        backup = self.backup(storage, [blob('files/stock/big.bin', self.BIG, with_md5=False, etag='e-new')])
        backup.backup_folder('files', 'files')
        self.assertEqual(storage.uploads, [])
        self.assertEqual(backup.changed_files, ['files/stock/big.bin'])

    def test_encrypted_copy_is_settled_by_recorded_md5(self):
        # the stored bytes are ciphertext, so the listing's hash cannot match: one fetch
        ciphertext = b'not the plaintext'
        storage = FakeStorage(self.stored(('stock/a.pdf', ciphertext,
                                           {'etag': 'e-a', 'md5': md5(self.SMALL), 'encrypted': '1'})))
        self.backup(storage, [blob('files/stock/a.pdf', self.SMALL)]).backup_folder('files', 'files')
        self.assertEqual(storage.metadata_fetches, [f'{self.ROOT}/files/stock/a.pdf'])
        self.assertEqual(storage.uploads, [])

    def test_new_blob_is_uploaded_with_its_fingerprint(self):
        storage = FakeStorage(self.stored(('stock/a.pdf', self.SMALL, {'etag': 'e-a', 'md5': md5(self.SMALL)})))
        new = b'new attachment'
        blobs = [blob('files/stock/a.pdf', self.SMALL), blob('files/orders/SO_1/new.pdf', new, etag='e-new')]
        self.backup(storage, blobs).backup_folder('files', 'files')
        key = f'{self.ROOT}/files/orders/SO_1/new.pdf'
        self.assertEqual(storage.uploads, [key])
        self.assertEqual(storage.files[key], (new, {'etag': 'e-new', 'md5': md5(new)}))
        self.assertEqual(storage.metadata_fetches, [])

    def test_changed_blob_protect_skips_and_history_keeps_version(self):
        changed = b'edited in place'
        for mode, uploads, kept in (('protect', [], []), ('history', [f'{self.ROOT}/files/stock/a.pdf'],
                                                             [f'{self.ROOT}/files/stock/a.pdf'])):
            with self.subTest(mode=mode):
                storage = FakeStorage(self.stored(('stock/a.pdf', self.SMALL, {'etag': 'e-a', 'md5': md5(self.SMALL)})))
                backup = self.backup(storage, [blob('files/stock/a.pdf', changed, etag='e-a2')], changed_files=mode)
                backup.backup_folder('files', 'files')
                # the hashes differ, so the recorded md5 had to be consulted to be sure
                self.assertEqual(storage.metadata_fetches, [f'{self.ROOT}/files/stock/a.pdf'])
                self.assertEqual(storage.uploads, uploads)
                self.assertEqual(storage.kept, kept)
                self.assertEqual(backup.changed_files, ['files/stock/a.pdf'])

    def test_deleted_stored_file_compares_as_no_checksum(self):
        # the file browser's per-row verify passes {} when the stored file has gone since
        # the page was listed: no metadata fetch, and on S3 no KeyError on the missing id
        storage = FakeStorage()
        current = blob('files/stock/a.pdf', self.SMALL)
        self.assertEqual(self.backup(storage, [current]).compare({}, current), 'no_checksum')
        self.assertEqual(storage.metadata_fetches, [])

    def test_verify_fetches_metadata_only_where_needed(self):
        storage = FakeStorage(self.stored(
            ('stock/a.pdf', self.SMALL, {'etag': 'e-a', 'md5': md5(self.SMALL)}),
            ('stock/big.bin', self.BIG, {'etag': 'e-big'}),
            ('stock/gone.pdf', self.SMALL, {'etag': 'e-gone', 'md5': md5(self.SMALL)})))
        blobs = [blob('files/stock/a.pdf', self.SMALL), blob('files/stock/big.bin', self.BIG, with_md5=False, etag='e-big')]
        results = self.backup(storage, blobs).verify_folder('files', 'files')
        self.assertEqual(results, {'matched': 2, 'changed': [], 'missing': ['stock/gone.pdf'], 'no_checksum': []})
        self.assertEqual(storage.metadata_fetches, [f'{self.ROOT}/files/stock/big.bin'])


@unittest.skipIf(S3Storage is None, 'boto3 is not installed')
class S3FileMetadataTest(SimpleTestCase):

    def storage(self):
        storage = S3Storage.__new__(S3Storage)
        storage.bucket = 'b'
        storage.s3 = MagicMock()
        storage.s3.head_object.return_value = {'Metadata': {'md5': 'abc', 'etag': 'e'}}
        return storage

    def test_listing_carries_no_metadata_and_it_is_fetched_once(self):
        storage = self.storage()
        f = storage.normalise('backup/live/files/a.pdf', 3, '"0123"', None)
        self.assertEqual((f['hash'], f['metadata']), ('0123', {}))
        self.assertEqual(storage.file_metadata(f), {'md5': 'abc', 'etag': 'e'})
        self.assertEqual(storage.file_metadata(f), {'md5': 'abc', 'etag': 'e'})
        storage.s3.head_object.assert_called_once_with(Bucket='b', Key='backup/live/files/a.pdf')

    def test_multipart_object_is_headed_at_listing_time(self):
        # its etag is not an md5, so the recorded one is needed to give the file a hash
        storage = self.storage()
        f = storage.normalise('backup/live/files/big.bin', 3, '"0123-4"', None)
        self.assertEqual((f['hash'], f['metadata']), ('abc', {'md5': 'abc', 'etag': 'e'}))
        self.assertEqual(storage.file_metadata(f), {'md5': 'abc', 'etag': 'e'})
        storage.s3.head_object.assert_called_once()
