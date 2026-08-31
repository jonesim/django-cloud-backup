"""Azure Blob Storage as a backup *source*.

A project whose media lives in an Azure container (django-storages' AzureStorage) has
nothing on local disk for BACKUP_DIRS to back up. AZURE_BACKUP_DIRS / the azure_dirs
config key names prefixes in that container instead, and BackupAzure streams the blobs
through the normal BackupStorage pipeline - the same destination folders, encryption,
changed-file protection, object lock and file browser as a local directory, so an Azure
prefix and a local directory are interchangeable entries in BackupConfig.file_sources.

Deduplication uses the fingerprint Azure already holds, so nothing is downloaded to
decide whether a blob needs backing up: the plaintext md5 when Azure recorded one
(Content-MD5 is set automatically for single-request uploads, which is what
django-storages does below its block-size threshold) and otherwise the blob's ETag,
which changes on every write. Both are recorded in the stored file's metadata; the md5
also feeds the file browser's checksum column and content_hash()-based dedup, exactly as
a local file's does.

This is the counterpart of BackupS3 (an S3 bucket as a source), not of
backup_azure_s3.BackupAzureToS3, which is a standalone rclone-compatible mirror that
bypasses the pipeline.
"""
import io

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import ContainerClient

from .base_backup import BaseBackup, CHANGED_HISTORY, CHANGED_PROTECT
from .encryption import EncryptingReader


def container_client(source):
    """source is an azure_source dict: container plus connection_string, or account_url
    and credential - the same keys as the azure destination backend."""
    if source.get('connection_string'):
        return ContainerClient.from_connection_string(source['connection_string'], source['container'])
    return ContainerClient(source['account_url'], source['container'], credential=source.get('credential'))


def blob_fingerprint(blob):
    """(md5 hex or None, etag) for a BlobProperties. Azure only holds a Content-MD5 when
    the upload supplied one or was a single Put Blob request; the etag is always there."""
    content_md5 = getattr(blob.content_settings, 'content_md5', None)
    md5 = bytes(content_md5).hex() if content_md5 else None
    return md5, (blob.etag or '').strip('"')


class AzureBlobFile(io.RawIOBase):
    """Seekable read-only stream over a blob, fetching byte ranges on demand - the
    destination backends' uploaders (boto3, googleapiclient) probe the size with a seek
    to the end and then read in chunks, so the blob is never held in memory."""

    def __init__(self, blob_client, size):
        self.blob_client = blob_client
        self.size = size
        self.position = 0

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self.position = offset
        elif whence == io.SEEK_CUR:
            self.position += offset
        elif whence == io.SEEK_END:
            self.position = self.size + offset
        else:
            raise ValueError(f'invalid whence ({whence!r}, should be {io.SEEK_SET}, {io.SEEK_CUR}, {io.SEEK_END})')
        return self.position

    def seekable(self):
        return True

    def readable(self):
        return True

    def read(self, size=-1):
        if self.position >= self.size:
            return b''
        if size == -1 or self.position + size > self.size:
            size = self.size - self.position
        data = self.blob_client.download_blob(offset=self.position, length=size).readall()
        self.position += len(data)
        return data


class BackupAzure(BaseBackup):
    """Copies the blobs under a prefix of an Azure container to the backup storage,
    keeping the container's folder structure. Interchangeable with BackupLocal:
    backup_folder / verify_folder take the source (here a blob prefix rather than a
    directory) and the destination folder name."""

    def __init__(self, source, storage, backup_dir, logger, config=None):
        super().__init__(storage, backup_dir, logger, config=config)
        self.source = source
        self.container = container_client(source)
        self.changed_files = []

    def describe(self, prefix):
        return f"azure://{self.source['container']}/{prefix}"

    @staticmethod
    def blob_name(prefix, rel_path):
        return f'{prefix}/{rel_path}' if prefix else rel_path

    def blobs(self, prefix):
        """(path relative to the prefix, BlobProperties) for every blob under it, from
        one paginated listing. An empty prefix is the whole container."""
        start = prefix + '/' if prefix else ''
        for blob in self.container.list_blobs(name_starts_with=start):
            rel_path = blob.name[len(start):]
            if not rel_path or rel_path.endswith('/'):
                continue  # a directory placeholder blob
            yield rel_path, blob

    def get_blob(self, prefix, rel_path):
        """Current properties of one blob, or None when it no longer exists."""
        try:
            return self.container.get_blob_client(self.blob_name(prefix, rel_path)).get_blob_properties()
        except ResourceNotFoundError:
            return None

    def compare(self, stored_file, blob):
        """'match', 'changed', 'missing' or 'no_checksum' for a stored file against the
        blob it was backed up from.

        Decided from the listing alone wherever possible: the backend's hash of the
        stored bytes is the plaintext md5 for an unencrypted single-part copy (a
        multipart copy's hash is already the recorded md5), so equal hashes settle the
        common case - an unchanged blob - with no further request. Only when that does
        not settle it is the stored metadata consulted, fetched on demand where the
        listing could not carry it (a HEAD per object on S3): the plaintext md5 recorded
        at upload - an unchanged file re-uploaded to Azure gets a new etag but the same
        md5, and the md5 survives client-side encryption - and otherwise the etag."""
        if blob is None:
            return 'missing'
        if not stored_file:
            # the file browser's verify passes {} for a stored file deleted since the
            # page was listed: nothing to fetch metadata for
            return 'no_checksum'
        md5, etag = blob_fingerprint(blob)
        if md5 and stored_file.get('hash') == md5:
            return 'match'
        metadata = self.storage.file_metadata(stored_file)
        if md5 and metadata.get('md5'):
            return 'match' if md5 == metadata['md5'] else 'changed'
        if metadata.get('etag'):
            return 'match' if etag == metadata['etag'] else 'changed'
        if md5 and not metadata.get('encrypted') and stored_file.get('hash'):
            # a stored copy with no metadata at all: the backend's hash of the stored
            # bytes is the plaintext md5 unless the upload was multipart
            return 'match' if md5 == stored_file['hash'] else 'changed'
        return 'no_checksum'

    def stored_files(self, backup_dir):
        """{relative path: {name: [file dicts]}} for everything already under
        backup_dir - one recursive listing of the destination rather than one per
        folder, and without metadata: compare() asks for it only where the listing
        cannot decide."""
        base = self.storage.ensure_folder(backup_dir, parent=self.base_backup_dir)
        stored = {}
        for path, stored_file in self.storage.walk(base):
            stored.setdefault(path, {}).setdefault(stored_file['name'], []).append(stored_file)
        self.logger.info(f'{sum(len(files) for names in stored.values() for files in names.values()):,} '
                         f'files already in {backup_dir}')
        return stored

    def backup_folder(self, prefix, backup_dir):
        self.logger.info(f'Backing up {self.describe(prefix)} to {backup_dir}')
        mode = self.config.changed_files
        lock_days = self.storage.lock_days('file')
        stored = self.stored_files(backup_dir)
        handles = {}

        def folder(path):
            if path not in handles:
                name = f'{backup_dir}/{path}' if path else backup_dir
                handles[path] = self.storage.ensure_folder(name, parent=self.base_backup_dir)
            return handles[path]

        for rel_path, blob in self.blobs(prefix):
            path, _, name = rel_path.rpartition('/')
            handle = folder(path)
            existing = stored.get(path, {}).get(name, [])
            if any(self.compare(e, blob) == 'match' for e in existing):
                self.logger.info('    Exists - ' + rel_path)
                continue
            if existing:
                if mode == CHANGED_PROTECT:
                    self.changed_files.append(blob.name)
                    self.logger.warning(f'NOT backing up {blob.name} - it no longer matches its existing backup')
                    continue
                if mode == CHANGED_HISTORY:
                    self.changed_files.append(blob.name)
                    self.logger.warning(f'{blob.name} changed - keeping the previous backup version')
                    self.storage.keep_version(existing[0], lock_days=lock_days)
            self.logger.info('Backup - ' + rel_path)
            md5, etag = blob_fingerprint(blob)
            # the fingerprints are of the source blob, so dedup is unaffected by the
            # stored bytes being encrypted
            metadata = {'etag': etag}
            if md5:
                metadata['md5'] = md5
            stream = AzureBlobFile(self.container.get_blob_client(blob.name), blob.size)
            if self.encryption_key is not None:
                stream = EncryptingReader(stream, blob.size, self.encryption_key, logger=self.logger)
                metadata['encrypted'] = '1'
            self.storage.upload(handle, name, stream, metadata=metadata, lock_days=lock_days)

    def verify_folder(self, prefix, backup_dir):
        """Compare every stored file under backup_dir against the blob it was backed up
        from. Same result dict as BackupLocal.verify_folder; 'missing' means the blob is
        no longer in the container."""
        self.logger.info(f'Verifying {backup_dir} against {self.describe(prefix)}')
        results = {'matched': 0, 'changed': [], 'missing': [], 'no_checksum': []}
        folder = self.storage.get_folder(backup_dir, parent=self.base_backup_dir)
        if folder is None:
            self.logger.warning(f'No backup folder found for {backup_dir}')
            return results
        # one listing of the source rather than a properties request per file, and the
        # stored metadata only for files compare() cannot settle from the listing
        blobs = dict(self.blobs(prefix))
        for path, stored_file in self.storage.walk(folder):
            rel_path = f"{path}/{stored_file['name']}" if path else stored_file['name']
            status = self.compare(stored_file, blobs.get(rel_path))
            if status == 'match':
                results['matched'] += 1
                self.logger.info(f'    Match - {rel_path}')
            elif status == 'missing':
                results['missing'].append(rel_path)
                self.logger.warning(f'    Missing from the source - {rel_path}')
            elif status == 'no_checksum':
                results['no_checksum'].append(rel_path)
                self.logger.warning(f'    No stored checksum - {rel_path}')
            else:
                results['changed'].append(rel_path)
                self.logger.warning(f'    CHANGED - {rel_path}')
        return results
