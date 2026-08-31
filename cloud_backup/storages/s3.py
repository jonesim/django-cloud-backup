import concurrent.futures
import os
import threading
from datetime import datetime, timedelta, timezone
from io import BytesIO

import boto3
import requests
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

from ..db_tiers import DEFAULT_EXPIRE_DAYS, DEFAULT_PURGE_DAYS
from .base import BackupStorage, StorageFileNotFound


class B2AuthError(Exception):
    pass


def b2_s3_endpoint(key_id, application_key):
    """Backblaze B2 keys work with B2's S3-compatible API; the account's S3 endpoint
    is returned by b2_authorize_account so it does not need to be configured."""
    response = requests.get('https://api.backblazeb2.com/b2api/v2/b2_authorize_account',
                            auth=(key_id, application_key), timeout=30)
    if response.status_code != 200:
        raise B2AuthError(response.text)
    return response.json()['s3ApiUrl']


class S3Storage(BackupStorage):
    """
    Any S3-compatible destination: AWS S3, Backblaze B2 (pass b2=True to discover the
    endpoint from the key) or Cloudflare R2 (pass the account's endpoint_url and
    region='auto'). Folders are just key prefixes so they always "exist"; deletes are
    permanent unless the bucket itself has versioning or lifecycle rules.
    """

    # a versioned bucket's hidden previous versions are its trash: listed and restored
    # with the read/write key alone. Purging them is the lifecycle rule's job - the key
    # is deliberately unable to
    supports_trash = True
    supports_empty_trash = False
    # what separates the key from the version id in a hidden version's file id
    VERSION_SEPARATOR = '?versionId='
    metadata_workers = 8
    # CopyObject's ceiling - above it the copy has to be done in parts
    copy_object_limit = 5 * 1024 * 1024 * 1024

    def __init__(self, bucket, access_key_id=None, secret_key=None, endpoint_url=None,
                 region=None, b2=False, lock=None):
        if b2 and endpoint_url is None:
            endpoint_url = b2_s3_endpoint(access_key_id, secret_key)
            if region is None:
                # https://s3.<region>.backblazeb2.com
                region = endpoint_url.split('.')[1]
        self.s3 = boto3.client('s3', aws_access_key_id=access_key_id, aws_secret_access_key=secret_key,
                               endpoint_url=endpoint_url, region_name=region)
        self.bucket = bucket
        self.lock = lock or {}
        self.lock_mode = self.lock.get('mode', 'COMPLIANCE')
        self.transfer_config = TransferConfig(multipart_threshold=64 * 1024 * 1024)

    def _lock_args(self, lock_days, lock_mode=None):
        """Object-lock parameters for an upload/copy - the bucket must have Object Lock
        enabled or these requests are rejected. Retention is per object, so locking one
        tier does not stop anything else in the bucket being deleted."""
        if not lock_days:
            return {}
        return {'ObjectLockMode': lock_mode or self.lock_mode,
                'ObjectLockRetainUntilDate': datetime.now(timezone.utc) + timedelta(days=lock_days)}

    @staticmethod
    def _folder_handle(prefix):
        return {'id': prefix, 'name': prefix.rsplit('/', 1)[-1], 'web_link': None}

    def ensure_folder(self, path, parent=None):
        prefix = f"{parent['id']}/{path}" if parent else path
        return self._folder_handle(prefix.strip('/'))

    def get_folder(self, path, parent=None):
        return self.ensure_folder(path, parent)

    def normalise(self, key, size, etag, modified, metadata=None):
        etag = (etag or '').strip('"')
        if metadata is None and (self._is_multipart(etag)):
            metadata = self._head_metadata(key)
        metadata = metadata or {}
        file_hash = metadata.get('md5') if self._is_multipart(etag) else etag
        modified = self.to_local_naive(modified)
        return {'id': key,
                'name': key.rsplit('/', 1)[-1],
                'size': size,
                'hash': file_hash or None,
                'created': modified,  # S3 only records last modification
                'modified': modified,
                'metadata': metadata,
                'web_link': None}

    @staticmethod
    def _is_multipart(etag):
        return '-' in etag

    def _head_metadata(self, key):
        return self.s3.head_object(Bucket=self.bucket, Key=key).get('Metadata', {})

    def file_metadata(self, stored_file):
        """A listing carries no metadata on S3, so a file dict from one made without
        include_metadata has an empty dict: fetch it with a HEAD on first use and keep
        it on the dict. An object that genuinely has no metadata costs a HEAD each
        time - rare (nothing this package uploads is without it) and harmless."""
        if not stored_file.get('metadata'):
            stored_file['metadata'] = self._head_metadata(stored_file['id'])
        return stored_file['metadata']

    def _metadata_map(self, keys):
        """Metadata for several objects at once. S3 needs a HEAD per object and a
        listing can cover hundreds of them, so they go out in parallel."""
        if not keys:
            return {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.metadata_workers, len(keys))) as pool:
            return dict(zip(keys, pool.map(self._head_metadata, keys)))

    def list_files(self, folder, metadata_filter=None, deleted=False, include_metadata=False):
        if deleted:
            return self.hidden_versions(folder)
        prefix = folder['id'] + '/'
        objects = []
        for page in self.s3.get_paginator('list_objects_v2').paginate(Bucket=self.bucket,
                                                                      Prefix=prefix, Delimiter='/'):
            objects += [s3_object for s3_object in page.get('Contents', [])
                        if s3_object['Key'] != prefix]  # skip the zero-byte directory marker
        metadata = self._metadata_map([s3_object['Key'] for s3_object in objects]) \
            if include_metadata or metadata_filter else {}
        files = []
        for s3_object in objects:
            f = self.normalise(s3_object['Key'], s3_object['Size'], s3_object['ETag'],
                               s3_object['LastModified'], metadata=metadata.get(s3_object['Key']))
            if self.matches_metadata(f, metadata_filter):
                files.append(f)
        return files

    def hidden_versions(self, folder):
        """Every non-current version under a folder, recursively, as files whose id
        carries the version - what list_files(deleted=True) is on a versioned bucket. A
        version is hidden by whatever came after it on the same key, a delete marker or a
        newer version; that time is 'hidden', the start of the purge clock. Metadata is
        not fetched - that would be a HEAD per version."""
        by_key = {}
        for entry in self.list_versions(folder['id'] + '/'):
            by_key.setdefault(entry['key'], []).append(entry)
        files = []
        for key, entries in by_key.items():
            entries.sort(key=lambda e: e['modified'])
            for entry, successor in zip(entries, entries[1:]):
                if entry['marker'] or entry['is_latest']:
                    continue
                files.append({'id': f"{key}{self.VERSION_SEPARATOR}{entry['version_id']}",
                              'name': key.rsplit('/', 1)[-1],
                              'size': entry['size'],
                              'hash': None,
                              'created': entry['modified'],
                              'modified': entry['modified'],
                              'hidden': successor['modified'],
                              'metadata': {},
                              'web_link': None})
        return files

    def restore_deleted(self, file_id):
        """Make a hidden version current again with a server-side copy of it onto its
        own key - needs nothing beyond read and write, no delete: whatever hid it (a
        delete marker, an attacker's overwrite) just becomes a previous version. The
        metadata travels with it; a >5GB copy goes multipart, which inherits nothing, so
        it is always passed explicitly."""
        key, version_id = file_id.rsplit(self.VERSION_SEPARATOR, 1)
        head = self.s3.head_object(Bucket=self.bucket, Key=key, VersionId=version_id)
        source = {'Bucket': self.bucket, 'Key': key, 'VersionId': version_id}
        extra_args = {'Metadata': head.get('Metadata') or {}}
        if head.get('ContentType'):
            extra_args['ContentType'] = head['ContentType']
        if head['ContentLength'] <= self.copy_object_limit:
            self.s3.copy_object(Bucket=self.bucket, Key=key, CopySource=source,
                                MetadataDirective='REPLACE', **extra_args)
        else:
            self.s3.copy(source, self.bucket, key, ExtraArgs=extra_args, Config=self.transfer_config)
        restored = self.get_file(key)
        if restored['size'] != head['ContentLength']:
            raise IOError(f'Restored {key} is {restored["size"]} bytes, expected {head["ContentLength"]}')
        return restored

    def list_folders(self, folder):
        prefix = folder['id'] + '/'
        folders = []
        for page in self.s3.get_paginator('list_objects_v2').paginate(Bucket=self.bucket,
                                                                      Prefix=prefix, Delimiter='/'):
            for p in page.get('CommonPrefixes', []):
                folders.append(self._folder_handle(p['Prefix'].rstrip('/')))
        return folders

    def walk(self, folder, include_metadata=False):
        prefix = folder['id'] + '/'
        for page in self.s3.get_paginator('list_objects_v2').paginate(Bucket=self.bucket, Prefix=prefix):
            objects = [s3_object for s3_object in page.get('Contents', [])
                       if not s3_object['Key'].endswith('/')]
            metadata = self._metadata_map([s3_object['Key'] for s3_object in objects]) \
                if include_metadata else {}
            for s3_object in objects:
                relative = s3_object['Key'][len(prefix):]
                path = relative.rsplit('/', 1)[0] if '/' in relative else ''
                yield path, self.normalise(s3_object['Key'], s3_object['Size'], s3_object['ETag'],
                                           s3_object['LastModified'],
                                           metadata=metadata.get(s3_object['Key']))

    def get_file(self, file_id):
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=file_id)
        except ClientError as e:
            if e.response['Error']['Code'] in ('404', 'NoSuchKey', 'NotFound'):
                raise StorageFileNotFound(file_id)
            raise
        return self.normalise(file_id, head['ContentLength'], head['ETag'], head['LastModified'],
                              metadata=head.get('Metadata', {}))

    def find_file(self, folder, name):
        return self.get_file(f"{folder['id']}/{name}")

    def upload(self, folder, name, stream, metadata=None, lock_days=None):
        key = f"{folder['id']}/{name}"
        extra_args = {'Metadata': {k: str(v) for k, v in metadata.items()}} if metadata else {}
        extra_args.update(self._lock_args(lock_days))
        self.s3.upload_fileobj(stream, self.bucket, key, ExtraArgs=extra_args or None,
                               Config=self.transfer_config)
        return self.get_file(key)

    def keep_version(self, stored_file, lock_days=None):
        # server-side managed copy (handles > 5GB multipart) - no data transfer or
        # delete permission needed, and the copy gets its own object lock
        version_key = self.version_name(stored_file['id'])
        self.s3.copy({'Bucket': self.bucket, 'Key': stored_file['id']}, self.bucket, version_key,
                     ExtraArgs=self._lock_args(lock_days) or None, Config=self.transfer_config)
        return version_key

    def copy_stored_file(self, stored_file, folder, name, extra_metadata=None, lock_days=None,
                         lock_mode=None):
        key = f"{folder['id']}/{name}"
        head = self.s3.head_object(Bucket=self.bucket, Key=stored_file['id'])
        metadata = dict(head.get('Metadata') or {},
                        **{k: str(v) for k, v in (extra_metadata or {}).items()})
        source = {'Bucket': self.bucket, 'Key': stored_file['id']}
        extra_args = {'Metadata': metadata, **self._lock_args(lock_days, lock_mode)}
        if head.get('ContentType'):
            extra_args['ContentType'] = head['ContentType']
        if head['ContentLength'] <= self.copy_object_limit:
            self.s3.copy_object(Bucket=self.bucket, Key=key, CopySource=source,
                                MetadataDirective='REPLACE', **extra_args)
        else:
            # the managed copy switches to a multipart copy, which starts from
            # CreateMultipartUpload and so takes the metadata from ExtraArgs - there is
            # no MetadataDirective on that path and nothing is inherited from the source
            self.s3.copy(source, self.bucket, key, ExtraArgs=extra_args, Config=self.transfer_config)
        copied = self.get_file(key)
        if copied['size'] != head['ContentLength']:
            raise IOError(f'Copy of {stored_file["id"]} to {key} is {copied["size"]} bytes, '
                          f'expected {head["ContentLength"]}')
        return copied

    def extend_retention(self, folder, min_days, workers=8):
        """Ensure every object under folder keeps at least min_days of object-lock
        retention. Extending retention is always allowed; shortening never is, so this
        is safe to re-run. Costs 1-2 API calls per object - schedule it rather than
        running it with every backup."""
        target = datetime.now(timezone.utc) + timedelta(days=min_days)
        keys = []
        for page in self.s3.get_paginator('list_objects_v2').paginate(Bucket=self.bucket,
                                                                      Prefix=folder['id'] + '/'):
            keys += [s3_object['Key'] for s3_object in page.get('Contents', [])
                     if not s3_object['Key'].endswith('/')]
        stats = {'checked': 0, 'extended': 0, 'errors': []}
        stats_lock = threading.Lock()

        def extend(key):
            try:
                head = self.s3.head_object(Bucket=self.bucket, Key=key)
                retain_until = head.get('ObjectLockRetainUntilDate')
                if retain_until is None or retain_until < target:
                    self.s3.put_object_retention(
                        Bucket=self.bucket, Key=key,
                        Retention={'Mode': head.get('ObjectLockMode') or self.lock_mode,
                                   'RetainUntilDate': target})
                    with stats_lock:
                        stats['extended'] += 1
            except Exception as e:
                with stats_lock:
                    stats['errors'].append(f'{key}: {e}')
            with stats_lock:
                stats['checked'] += 1

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(extend, keys))
        return stats

    def verify_upload(self, stored_file, local_path):
        saved = self.get_file(stored_file['id'])
        if saved['size'] != os.path.getsize(local_path):
            return False
        # boto3 checksums every part in transit, so when no comparable md5 is available
        # (multipart upload with no md5 metadata) a size match is the best remaining check
        if saved['hash']:
            return saved['hash'] == self.md5sum(local_path)
        return True

    def download(self, stored_file, local_folder=None):
        if local_folder:
            self.s3.download_file(self.bucket, stored_file['id'],
                                  os.path.join(local_folder, stored_file['name']),
                                  Config=self.transfer_config)
            return stored_file['name']
        stream = BytesIO()
        self.s3.download_fileobj(self.bucket, stored_file['id'], stream, Config=self.transfer_config)
        stream.seek(0)
        return stream

    def delete(self, file_id):
        self.s3.delete_object(Bucket=self.bucket, Key=file_id)

    def storage_info(self, folder=None):
        name = f's3://{self.bucket}'
        if folder:
            name += f"/{folder['id']}"
        return {'name': name, 'web_link': None, 'used': None, 'limit': None}

    @staticmethod
    def _protection_unknown(label, error, capability=None):
        """:param capability: the B2 key capability this query needs, named in the row
        when the destination refuses it - 'AccessDenied' on its own sends people looking
        at bucket policy for what is really a missing capability on the key"""
        code = getattr(error, 'response', {}).get('Error', {}).get('Code') or type(error).__name__
        detail = f'could not query ({code})'
        if capability and code in ('AccessDenied', 'Forbidden', 'Unauthorized', '403'):
            detail += f' - a Backblaze key needs the {capability} capability for this'
        # not a fault in itself, but nothing below it can be trusted either
        return {'label': label, 'status': 'Unknown', 'action': 'warn', 'detail': detail}

    @staticmethod
    def _rule_prefix(rule):
        """A lifecycle rule's key prefix, across the current and legacy shapes. An empty
        prefix covers the whole bucket."""
        if 'Prefix' in rule:
            return rule['Prefix']
        rule_filter = rule.get('Filter') or {}
        if 'And' in rule_filter:
            return rule_filter['And'].get('Prefix', '')
        return rule_filter.get('Prefix', '')

    @staticmethod
    def _rule_days(rule):
        return (rule.get('Expiration') or {}).get('Days')

    @staticmethod
    def _rule_noncurrent_days(rule):
        return (rule.get('NoncurrentVersionExpiration') or {}).get('NoncurrentDays')

    @staticmethod
    def days(number):
        return f'{number} day' if number == 1 else f'{number} days'

    def object_retention(self, file_id):
        head = self.s3.head_object(Bucket=self.bucket, Key=file_id)
        retain_until = head.get('ObjectLockRetainUntilDate')
        if not retain_until:
            return None
        return {'mode': head.get('ObjectLockMode'), 'retain_until': self.to_local_naive(retain_until)}

    def lifecycle_rules(self):
        """The bucket's enabled lifecycle rules as plain dicts, one per key prefix - []
        when it has none configured. The caller has to expect a failure: not every
        S3-compatible service implements the call, and a backup credential may well not be
        allowed to read the bucket configuration."""
        try:
            rules = self.s3.get_bucket_lifecycle_configuration(Bucket=self.bucket).get('Rules', [])
        except ClientError as e:
            if 'NoSuchLifecycleConfiguration' in e.response.get('Error', {}).get('Code', ''):
                return []
            raise
        return self.merge_rules(
            {'id': rule.get('ID') or '', 'prefix': self._rule_prefix(rule),
             'days': self._rule_days(rule), 'noncurrent_days': self._rule_noncurrent_days(rule),
             'delete_markers': bool((rule.get('Expiration') or {}).get('ExpiredObjectDeleteMarker')),
             'abort_days': (rule.get('AbortIncompleteMultipartUpload') or {}).get('DaysAfterInitiation')}
            for rule in rules if rule.get('Status') == 'Enabled')

    @staticmethod
    def merge_rules(rules):
        """One entry per key prefix. A service can express one of its own rules as several
        S3 rules - Backblaze returns the delete-marker clean-up as a second rule on the
        same prefix - and two entries that each describe half of one rule read as
        duplicates, and turn into an overlapping pair of B2 rules that b2 bucket update
        rejects. The shortest wins for every day count: that is what actually happens to
        the object. Merging is by prefix alone, so rules that differ only by a tag or size
        filter collapse together - those filters are not represented here either."""
        merged = {}
        for rule in rules:
            target = merged.get(rule['prefix'])
            if target is None:
                merged[rule['prefix']] = dict(rule)
                continue
            target['id'] = target['id'] or rule['id']
            for field in ('days', 'noncurrent_days', 'abort_days'):
                days = [d for d in (target[field], rule[field]) if d]
                target[field] = min(days) if days else None
            target['delete_markers'] = target['delete_markers'] or rule['delete_markers']
        return list(merged.values())

    def list_versions(self, prefix):
        """Every object version and delete marker under a prefix - see BackupStorage. One
        request per 1000 entries; the db folder of a tiered config is a few hundred."""
        entries = []
        for page in self.s3.get_paginator('list_object_versions').paginate(Bucket=self.bucket, Prefix=prefix):
            for version in page.get('Versions', []):
                entries.append({'key': version['Key'], 'version_id': version.get('VersionId'),
                                'modified': self.to_local_naive(version['LastModified']),
                                'size': version.get('Size'), 'is_latest': version.get('IsLatest', False),
                                'marker': False})
            for marker in page.get('DeleteMarkers', []):
                entries.append({'key': marker['Key'], 'version_id': marker.get('VersionId'),
                                'modified': self.to_local_naive(marker['LastModified']),
                                'size': None, 'is_latest': marker.get('IsLatest', False), 'marker': True})
        return entries

    def versioned(self):
        """Whether the bucket keeps previous versions - B2 buckets seen through the S3
        API always report Enabled."""
        return (self.s3.get_bucket_versioning(Bucket=self.bucket).get('Status') or '') == 'Enabled'

    def rule_detail(self, rule, versioned=False):
        """What a rule does, in the order it happens. On a versioned bucket expiry only
        hides the current version, so the noncurrent clause is what actually deletes."""
        noncurrent = 'hidden versions deleted after' if versioned else 'previous versions after'
        parts = [f"expire after {self.days(rule['days'])}" if rule['days'] else None,
                 f"{noncurrent} {self.days(rule['noncurrent_days'])}"
                 if rule['noncurrent_days'] else None,
                 'delete markers removed' if rule['delete_markers'] else None,
                 f"abandoned uploads after {self.days(rule['abort_days'])}" if rule['abort_days'] else None]
        return ', '.join(part for part in parts if part) or 'nothing expires'

    @staticmethod
    def rule_name(rule):
        """A rule created without an ID still has to be nameable in a message."""
        return rule['id'] or rule['prefix'] or '(whole bucket)'

    @staticmethod
    def tier_expiry(rules, prefix):
        """The shortest expiry rule covering a tier prefix, or None. Matching is by
        prefix, so a rule on a parent folder covers the tier but a rule on a sibling
        (a per-schema tier folder) does not."""
        matched = [rule for rule in rules if rule['days'] and prefix.startswith(rule['prefix'])]
        return min(matched, key=lambda rule: rule['days']) if matched else None

    def lifecycle_info(self, tier_prefixes=None, versioned=False, expire_days=None, purge_days=None):
        """:param expire_days: {tier: days the tier should be kept, or None for
        indefinitely} - each tier's real rule is reported against what was asked for.
        Defaults to the standard tier policy rather than to "keep everything", or a
        correct rule on the hourly tier would be reported as a problem.
        :param purge_days: how long hidden versions should survive - a rule that purges
        them sooner shortens the undo window the config asked for"""
        expire_days = DEFAULT_EXPIRE_DAYS if expire_days is None else expire_days
        purge_days = DEFAULT_PURGE_DAYS if purge_days is None else purge_days
        try:
            rules = self.lifecycle_rules()
        except Exception as e:
            return [self._protection_unknown('Lifecycle rules', e, capability='readBucketLifecycleRules')]
        tier_rows = []
        # the folder a tier row has already described needs no second row of its own
        reported = set()
        for tier, prefix in (tier_prefixes or {}).items():
            rule = self.tier_expiry(rules, prefix)
            if rule is not None and rule['prefix'] == prefix:
                reported.add(prefix)
            tier_rows.append(self._tier_row(tier, prefix, rule, versioned, expire_days.get(tier), purge_days))
        # what is left is every rule no tier speaks for: a parent or whole-bucket rule, a
        # per-schema tier folder, or anything the site set up itself
        return [{'label': 'Lifecycle', 'folder': rule['prefix'] or '(whole bucket)',
                 'status': 'Enabled', 'detail': self.rule_detail(rule, versioned)}
                for rule in rules if rule['prefix'] not in reported] + tier_rows

    def _tier_row(self, tier, prefix, rule, versioned, wanted, purge_days=DEFAULT_PURGE_DAYS):
        """One tier's rule measured against what the config asked for. 'action' marks the
        rows that mean something has to be done - the rest of protection_info is context
        (Object Lock off is the normal state for a lifecycle-managed bucket, not a fault).
        The healthy details come from rule_detail, so this row says everything the rule
        itself would have said and the rule needs no row of its own."""
        row = {'label': f'Lifecycle {tier}', 'folder': prefix}
        if rule is None:
            if wanted is None:
                return dict(row, status='Enabled', detail='kept - no expiry rule, as intended')
            return dict(row, status='Disabled', action='fix',
                        detail=f'no lifecycle rule - {tier} dumps will accumulate forever instead of '
                               f'expiring after {self.days(wanted)}')
        if wanted is None:
            # something expires the tier that is meant to be kept - usually a catch-all
            # rule on the whole bucket, which is how an archive quietly disappears
            return dict(row, status='Disabled', action='fix',
                        detail=f"rule '{self.rule_name(rule)}' expires them after {self.days(rule['days'])}"
                               f' - {tier} dumps are meant to be kept')
        if rule['days'] < wanted:
            return dict(row, status='Disabled', action='fix',
                        detail=f"rule '{self.rule_name(rule)}' expires them after {self.days(rule['days'])}, "
                               f'sooner than the {self.days(wanted)} this config asks for')
        if versioned and not rule['noncurrent_days']:
            # B2 (and any versioned bucket) only hides the file on expiry - without a
            # noncurrent rule the hidden version stays and goes on being billed
            return dict(row, status='Disabled', action='fix',
                        detail=f"expire after {self.days(rule['days'])}, but the hidden versions are never "
                               f'deleted - they keep being charged for')
        if versioned and rule['noncurrent_days'] < purge_days:
            # the hidden-version window is the time an attacker's hide or overwrite can
            # still be undone, so a shorter one than asked for is a protection gap
            return dict(row, status='Disabled', action='fix',
                        detail=f"hidden versions purged after {self.days(rule['noncurrent_days'])}, sooner "
                               f'than the {self.days(purge_days)} this config asks for - that window is '
                               f'the time a deleted or overwritten dump can still be recovered')
        detail = self.rule_detail(rule, versioned)
        if rule['days'] > wanted:
            return dict(row, status='Suspended', action='warn',
                        detail=detail + f' - longer than the {self.days(wanted)} this config asks for')
        return dict(row, status='Enabled', detail=detail)

    def destination_status(self, root=None):
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except ClientError as e:
            code = e.response.get('Error', {}).get('Code', '')
            if code in ('404', 'NoSuchBucket', 'NotFound'):
                return {'state': 'missing', 'label': 'Bucket', 'status': 'Disabled',
                        'detail': f'{self.bucket} does not exist'}
            if code in ('403', 'AccessDenied', 'Forbidden'):
                # a bucket-restricted key answers 403 for every other name, including
                # names that do not exist, so this is never proof the bucket is there
                return {'state': 'denied', 'label': 'Bucket', 'status': 'Unknown',
                        'detail': f'these credentials cannot see {self.bucket} - it may not exist, or the '
                                  f'key may be restricted to another bucket'}
            return {'state': 'unknown', 'label': 'Bucket', 'status': 'Unknown',
                    'detail': f'could not check {self.bucket} ({code or type(e).__name__})'}
        except Exception as e:
            return {'state': 'unknown', 'label': 'Bucket', 'status': 'Unknown',
                    'detail': f'could not check {self.bucket} ({type(e).__name__})'}
        return {'state': 'ok', 'label': 'Bucket', 'status': 'Enabled', 'detail': f'{self.bucket} is accessible'}

    def protection_info(self, tier_prefixes=None, expire_days=None, purge_days=None):
        protection = []
        versioned = False
        try:
            status = self.s3.get_bucket_versioning(Bucket=self.bucket).get('Status') or 'Disabled'
            versioned = status == 'Enabled'
            protection.append({'label': 'Bucket versioning', 'status': status,
                               'detail': 'overwritten and deleted objects are kept as previous versions'
                                         if status == 'Enabled' else None})
        except ClientError as e:
            protection.append(self._protection_unknown('Bucket versioning', e, capability='readBuckets'))
        worm = 'Object Lock (WORM)'

        def worm_disabled_row():
            # versioning already keeps previous versions of overwritten and deleted
            # objects, so a missing lock is then a gap rather than an emergency
            if versioned:
                return {'label': worm, 'status': 'Disabled', 'badge': 'warning',
                        'detail': 'not enabled on this bucket - versioning still keeps previous versions'}
            return {'label': worm, 'status': 'Disabled', 'detail': 'not enabled on this bucket'}

        try:
            config = self.s3.get_object_lock_configuration(Bucket=self.bucket)
            config = config.get('ObjectLockConfiguration', {})
            enabled = config.get('ObjectLockEnabled') == 'Enabled'
            retention = config.get('Rule', {}).get('DefaultRetention', {})
            if not enabled:
                protection.append(worm_disabled_row())
            else:
                detail = 'objects may carry a retention date'
                if retention:
                    period = (f"{retention['Days']} days" if retention.get('Days')
                              else f"{retention.get('Years')} years")
                    detail = f"bucket default: {retention.get('Mode', '').lower()} retention {period}"
                protection.append({'label': worm, 'status': 'Enabled', 'detail': detail})
        except ClientError as e:
            if 'ObjectLockConfigurationNotFound' in e.response.get('Error', {}).get('Code', ''):
                protection.append(worm_disabled_row())
            else:
                protection.append(self._protection_unknown(worm, e, capability='readBucketRetentions'))
        if self.lock:
            days = [f"{kind} {self.lock[kind + '_days']} days"
                    for kind in ('db', 'file') if self.lock.get(kind + '_days')]
            protection.append({'label': 'Upload lock (BACKUP_STORAGE)', 'status': 'Enabled',
                               'detail': f"{self.lock_mode.lower()} retention: {', '.join(days)}"
                                         if days else f'{self.lock_mode.lower()} retention'})
        return protection + self.lifecycle_info(tier_prefixes, versioned=versioned, expire_days=expire_days,
                                                purge_days=purge_days)
