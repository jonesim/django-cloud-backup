# django-cloud-backup

[![PyPI version](https://badge.fury.io/py/django-cloud-backup.svg)](https://badge.fury.io/py/django-cloud-backup)

**Django backups that survive your credentials being stolen.**

Backs up PostgreSQL databases, local folders, S3 buckets and Azure containers to Google
Drive, S3-compatible storage (AWS, Backblaze B2, Cloudflare R2) or Azure Blob Storage -
and then holds them down so that whoever gets into your server cannot destroy them.

An attacker who reaches your settings has your backup credentials too. The usual
arrangement - scheduled dumps to a bucket the application can delete from - fails at
exactly the moment it is needed: the backups are wiped, or quietly overwritten with
encrypted copies on the next scheduled run. This package is built for that threat model.

## What it does that a scheduled dump does not

- **Object Lock retention stamped at upload.** Every dump is immutable for a fixed window,
  topped up by a scheduled task; in `COMPLIANCE` mode not even the bucket owner can shorten
  it. See [Ransomware protection](#ransomware-protection).
- **An application that never deletes.** With `BACKUP_DB_TIERS` dumps are promoted between
  hourly/daily/monthly prefixes by server-side copy and the bucket's own lifecycle rules do
  all the expiry, so the backup credential needs no delete permission at all. See
  [Lifecycle-managed database backups](#lifecycle-managed-database-backups-s3b2r2).
- **Changed source files cannot silently overwrite good backups.**
  `BACKUP_CHANGED_FILES = 'protect'` refuses the overwrite and fails the run; `'history'`
  keeps the previous version first. See [Ransomware protection](#ransomware-protection).
- **Read-only destinations.** A config marked `restore_only` refuses every write and
  generates a read-only key - for the staging box that shares an encrypted settings file
  with live. See [Live, staging and local machines](#live-staging-and-local-machines).
- **It verifies the bucket is actually configured that way.** The management page queries
  the destination and reports versioning, soft delete, Object Lock and lifecycle rules, and
  flags any hidden version or overwrite the retention policy cannot explain. See
  [Setting up the bucket](#setting-up-the-bucket).
- **It tells you when the backups stop being current.** Every run is recorded and graded
  against your `CELERY_BEAT_SCHEDULE`, browsable in a Run Log page and served as json at
  `backup/status/` for a monitoring agent. See
  [Checking that the backups are current](#checking-that-the-backups-are-current).
- **Optional client-side AES-256-GCM encryption**, so the destination never holds
  plaintext. See [Client-side encryption](#client-side-encryption).

Alongside that: a Django management UI for browsing, verifying and restoring backups,
Celery tasks for scheduling, MD5/ETag deduplication, per-schema database backups, and
several named destinations in one project.

A schema dump (`pg_dump -n`) does not include the extensions installed in that schema, so
each schema dump records their names in its metadata (`extensions`, e.g. `pg_trgm`), and
restoring it creates any that are missing first. Only extensions installed *in* that
schema are recorded: one in `public` used by a tenant schema needs `public` restored
first, and a restore warns when a recorded extension is installed in a different schema.
The **Drop Restore** button, which runs `DROP SCHEMA ... CASCADE` and so removes the
extensions too, notes them before dropping and recreates them afterwards in the same
transaction, which also covers dumps made before this was recorded.

## Install

    pip install django-cloud-backup                # Google Drive destination
    pip install django-cloud-backup[s3]            # AWS S3, Backblaze B2, Cloudflare R2
    pip install django-cloud-backup[azure]         # Azure Blob Storage
    pip install django-cloud-backup[encryption]    # client-side encryption

settings.py:

    INSTALLED_APPS = ['cloud_backup', ...]
    BACKUP_ROOT = 'django_backup'

Google Drive is the default destination and needs a service account - the setup walkthrough
starts below. To back up to a bucket instead, set `BACKUP_STORAGE`; see
[Choosing a backup destination](#choosing-a-backup-destination).

Already running `django-gdrive-backup`? See
[Migrating from django-gdrive-backup](#migrating-from-django-gdrive-backup) at the end.

## Setup

The walkthrough below configures the default Google Drive destination.
For a bucket destination, skip to [Choosing a backup destination](#choosing-a-backup-destination).

## encrypted-credentials

This package uses encrypted-credentials and the instructions there could be useful. Adding the following lines to **settings.py** will initialise the package

    from encrypted_credentials.django_credentials import add_encrypted_settings
    
    add_encrypted_settings(globals())


## Create service account

Requires a Google service account with the Google Drive API enabled

https://console.cloud.google.com/apis/credentials/serviceaccountkey

## Add to cloud_backup installed apps

settings.py

    INSTALLED_APPS = [ ..
            'cloud_backup',
        ]

## Store service account key

By default *encrypted-credentials* is used to store the key. Create a directory off the django projects BASE_DIR called credentials and save the json key. 

settings.py

    CREDENTIAL_FOLDER = os.path.join(BASE_DIR, 'credentials')
    CREDENTIAL_FILES = {
        'drive': 'service-account.json',
    }
  

## Create Google Drive folder and share with service account

With a Google Drive account create a folder and share with the email address of the service account.


## Ensure psql is available to python subprocess

For docker containers you may need to something similar to the following line in the Dockerfile dependent on the version of Postgres.

    RUN apt-get -y install postgresql-client-11

## Configure database backup

settings.py

    BACKUP_ROOT = 'django_backup'

## Choosing a backup destination

Google Drive is the default destination and needs no extra settings beyond those above.
Backups can instead be stored on any S3-compatible service or Azure Blob Storage by adding
a `BACKUP_STORAGE` dict to settings.py. The optional `root` key replaces `BACKUP_ROOT`
as the top-level folder/prefix.

AWS S3:

    BACKUP_STORAGE = {
        'backend': 's3',
        'bucket': 'my-backups',
        'access_key_id': '...',
        'secret_key': '...',
        'root': 'django_backup',
    }

Backblaze B2 (the S3 endpoint is discovered from the key automatically):

    BACKUP_STORAGE = {
        'backend': 's3',
        'b2': True,
        'bucket': 'my-backups',
        'access_key_id': '...',       # B2 keyID
        'secret_key': '...',          # B2 applicationKey
    }

Cloudflare R2:

    BACKUP_STORAGE = {
        'backend': 's3',
        'bucket': 'my-backups',
        'endpoint_url': 'https://<account-id>.r2.cloudflarestorage.com',
        'region': 'auto',
        'access_key_id': '...',
        'secret_key': '...',
    }

Azure Blob Storage:

    BACKUP_STORAGE = {
        'backend': 'azure',
        'container': 'backups',
        'connection_string': '...',   # or account_url + credential
    }

S3 backends require `pip install django-cloud-backup[s3]` and Azure
`pip install django-cloud-backup[azure]`.

Note that unlike Google Drive, S3 and Azure destinations have no trash - pruned
database backups are deleted permanently, so consider enabling bucket versioning
(S3/B2/R2 lifecycle rules) or soft delete (Azure) if you want a safety net. The
backup page queries the destination and shows whether versioning, soft delete and
Object Lock/immutability (WORM) are actually enabled, so a missing safety net is
visible at a glance.

## Ransomware protection

If backups re-sync whenever a source file changes, an attacker encrypting your files
would overwrite the good backups on the next scheduled run. Protection is layered:

*Changed-file handling* - settings.py:

    BACKUP_CHANGED_FILES = 'overwrite'   # default: re-upload changed files
    BACKUP_CHANGED_FILES = 'protect'     # never touch the existing backup: skip the
                                         # file, log a warning and fail the backup run
                                         # (raises ChangedFilesError after everything
                                         # else has completed, so monitoring alerts)
    BACKUP_CHANGED_FILES = 'history'     # keep the previous version (S3 server-side
                                         # copy named <file>.<timestamp>, Azure
                                         # snapshot, Google Drive rename), then upload
                                         # the new version; warns but succeeds

Use `'protect'` when your file store is immutable (e.g. UUID-named uploads that are
never edited) - any change is corruption or an attacker. Use `'history'` when changes
can be legitimate but you still want every previous version recoverable.

*Object Lock retention (S3 and B2)* - create the bucket with Object Lock enabled and
add a `lock` section to `BACKUP_STORAGE`:

    BACKUP_STORAGE = {
        'backend': 's3', ...,
        'lock': {
            'mode': 'COMPLIANCE',   # not even the bucket owner can shorten or delete
            'db_days': 35,          # each database dump is locked for 35 days at upload
            'file_days': 7,         # each file backup is locked for 7 days at upload
            'min_days': 7,          # extend_retention keeps every file locked >= 7 days ahead
        },
    }

Locking at upload costs nothing (extra headers on the existing request). To keep
long-lived file backups permanently locked, schedule the top-up task daily - it
extends any object whose remaining lock is below `min_days`:

    CELERY_BEAT_SCHEDULE = {
        'extend_retention': {
            'task': 'cloud_backup.tasks.extend_retention',
            'schedule': crontab(hour=3, minute=0),
        },
    }

or run `python manage.py backup_website --extend_retention`. The top-up costs 1-2 API
calls per file (roughly a minute per 10,000 files), which is why it is a scheduled
task rather than part of every backup. Backups only become deletable `min_days` after
the top-up task stops running.

Pruning still works on a locked bucket: Object Lock buckets are versioned, so deleting
an old dump just writes a delete marker (allowed even while versions are locked) and
the locked versions physically remain. Add a bucket lifecycle rule such as "expire
noncurrent versions after 40 days" to clean them up once their lock has passed. The
same versioning means even `'overwrite'` mode cannot physically destroy data on a
locked bucket - the prior locked version survives underneath.

*Credential and bucket hardening* (outside this package):

- **AWS S3**: give the backup IAM user no `s3:DeleteObject`/`s3:PutBucketLifecycle`;
  prune via lifecycle rules instead of `BACKUP_DB_RETENTION`
- **Backblaze B2**: use an application key without the `deleteFiles` capability
- **Azure**: enable blob soft delete or a container immutability policy
- **Google Drive**: deletes go to trash and are recoverable, but the service account
  can empty the trash - treat the credential file accordingly

With delete-less credentials, pruning logs a warning instead of failing the backup;
leave `BACKUP_DB_RETENTION` unset and let bucket lifecycle rules do the pruning - the
tiered layout below is the supported way to do that.

## Lifecycle-managed database backups (S3/B2/R2)

`BACKUP_DB_RETENTION` prunes from the application: it lists the dumps and deletes the
ones it does not want to keep. `BACKUP_DB_TIERS` is the alternative - the application
only ever writes, and the bucket's own lifecycle rules do all the deleting:

    BACKUP_DB_TIERS = True          # requires the 's3' backend
    BACKUP_DB_RETENTION = []        # tiering replaces client-side pruning

Every dump then goes to a unique, date-partitioned key under `hourly/`, and a
scheduled job promotes one dump per day into `daily/` and one per month into
`monthly/` using server-side copies (no download, no re-upload, no egress):

    <root>/db/hourly/2026/07/30/db_2026_07_30_14_05_37.dump    expire after ~15 days
    <root>/db/daily/20_117_165_6/db_2026-07-30.dump            expire after ~91 days
    <root>/db/monthly/20_117_165_6/db_2026-07.dump             no rule - kept forever

The sub-folder is the server the dump came from (the `ip_address` metadata every dump
carries), and each server is promoted on its own: its last dump of the day becomes its
daily copy, its last daily copy of the month its monthly one. Several installations can
share one db folder - a staging server that restores from production's dumps, or a
developer's machine - and each keeps its own archive rather than whichever of them
dumped last overwriting the day's single copy. That is the same guarantee the
`BACKUP_DB_RETENTION` layout gives by pruning per server.

The retention you get is the lifecycle rules you create; this package never writes
them, so the backup credential needs neither `DeleteObject` nor
`PutBucketLifecycleConfiguration`. On AWS, two rules with `Expiration.Days` of 15 and
91 filtered on the `hourly/` and `daily/` prefixes. On B2, rules are
`fileNamePrefix` + `daysFromUploadingToHiding` + `daysFromHidingToDeleting`, and B2
buckets always keep versions, so **both** numbers are needed or "expired" files are
merely hidden and go on being billed:

    {"fileNamePrefix": "django_backup/db/hourly/", "daysFromUploadingToHiding": 15,
     "daysFromHidingToDeleting": 1}
    {"fileNamePrefix": "django_backup/db/daily/",  "daysFromUploadingToHiding": 91,
     "daysFromHidingToDeleting": 1}

B2 rejects overlapping rules, so an existing bucket-wide rule (empty prefix) has to go
before these can be added. The backup page reads the bucket's lifecycle configuration
and shows a row per tier, in red when `hourly/` has no rule (dumps accumulate forever)
or when a catch-all rule would expire the `monthly/` archive.

**Promotion runs at the end of every database backup**, so there is no second job that
has to be working for the backups to survive - the hourly tier expires by itself, and a
promotion step that quietly stopped would lose everything while the backups carried on
looking healthy. In that in-backup pass it only looks at days after the newest one
already promoted, which is normally two listings and no copies at all; after an outage it
catches every missed day up on the next backup.

It is still worth scheduling as well, so promotion happens on a day when no backup runs:

    CELERY_BEAT_SCHEDULE = {
        'promote_db_tiers': {
            'task': 'cloud_backup.tasks.promote_db_tiers',
            'schedule': crontab(hour=1, minute=30),
        },
    }

or `python manage.py backup_website --promote_tiers`. That form does a full 14-day scan,
warns about days that had no dumps at all, and takes `--as_of YYYY-MM-DD` and `--days N`
to backfill further. It needs no database connection and re-running it is a no-op.

The setup page shows a **Tier promotion** row with the newest daily dump, red when
nothing has reached the daily tier for more than two days - the one failure that would
otherwise be invisible until the hourly rule caught up with it.

Notes:

- Tiers are created inside each database folder, so a schema backed up separately gets
  `<root>/db/<schema>/hourly/...` and needs its own pair of lifecycle rules.
- Copies at the root of `daily/` and `monthly/` are from before promotion was per
  server. They are left as they are, expire under the same rules, and count as that
  day's (or month's) copy, so upgrading creates no duplicates.
- Not compatible with Object Lock (`lock` in `BACKUP_STORAGE`): a retention lock stops
  lifecycle expiry, so locked hourly dumps would accumulate forever. Pick one.
- Dumps written before tiering was turned on stay listed and restorable where they
  are; nothing is migrated or deleted.
- The web UI lists every tier, which means a metadata request per dump. Set
  `BACKUP_DB_TIERS = {'hourly_days': 3}` to list only the last few days of the hourly
  tier - it is a display window, not a retention setting.

**How long each tier is kept** defaults to 15 days hourly, 91 days daily and the monthly
archive indefinitely. Change it per config, and the setup page generates the matching
rules and checks the real ones against them:

    BACKUP_DB_TIERS = {'expire_days': {'monthly': 2557}}     # end of month for 7 years
    BACKUP_DB_TIERS = {'expire_days': {'hourly': 30, 'daily': 180, 'monthly': None}}

`None` means no rule at all, so that tier is kept until someone deletes it. By default the
application never applies these numbers - the destination's own rules are what actually
delete, and the page reads them back: a rule that expires a tier *sooner* than the config
asks for is a red row, and one that keeps it *longer* an amber one.

Expiry on a versioned bucket only *hides* a dump; the rule's second clause purges the
hidden version `purge_days` later (default 1). That clause applies to every hidden
version however it got hidden - including dumps an attacker holding the backup
credential has deleted or overwritten - so it is also the window in which such an attack
can still be undone. Trading a few days of hourly retention for a longer window costs
about the same storage and buys a week to notice:

    BACKUP_DB_TIERS = {'expire_days': {'hourly': 7}, 'purge_days': 7}   # 7 visible + 7 hidden

The setup page generates the rules with that clause and flags a real rule that purges
sooner.

A deleted dump is loud - the status check sees it missing. An *overwritten* one is not:
the name, date and size still look right, and the real version sits hidden underneath
until the purge. So for tiered configs the status check also audits the bucket's
versions: dumps get unique names and are only ever hidden by expiry once they are
`expire_days` old, so a dump hidden younger than that, anything hidden under `monthly/`,
or a key holding two real versions is someone else's doing. The `Version audit` row
names them, says whether they are `hidden` (still recoverable), `overwritten` or already
`purged`, and gives the deadline the purge clock sets - run `backup_status` more often
than `purge_days`.

### Explicit deletes with a locked archive

Lifecycle rules are only as durable as the bucket configuration - anyone with
`writeBuckets` can shorten them, and they delete silently. `delete: 'app'` moves the
deleting into the backup run, where it is logged and testable, and `lock_days` puts an
Object Lock retention on the tier that then has nothing else protecting it:

    BACKUP_DB_TIERS = {
        'delete': 'app',                                  # default: 'lifecycle'
        'expire_days': {'hourly': 15, 'daily': 91, 'monthly': None},
        'lock_days': {'monthly': 2557},                   # 7 years, per object
        'lock_mode': 'COMPLIANCE',                        # or 'GOVERNANCE'
    }

In this mode no lifecycle rules are generated or expected; the backup run deletes hourly
dumps older than `expire_days['hourly']` and daily ones older than `expire_days['daily']`,
straight after promoting - so a dump is only ever deleted once its daily copy exists.

Object Lock in B2 is **per object**, not per bucket: enabling it on the bucket only
permits objects to carry a retention date, and only the tiers in `lock_days` get one. So
the monthly archive becomes immutable while hourly and daily stay freely deletable in the
same bucket. `COMPLIANCE` cannot be shortened by anyone, including the account owner -
which is the point, and also means you are committed to paying for those objects for the
full period, and cannot delete the bucket while any remain. `GOVERNANCE` allows a key
with `bypassGovernance` to delete early.

The bucket needs Object Lock enabling, which `b2 bucket update --file-lock-enabled
<bucket>` does on an existing bucket - it cannot be turned off again. The key needs
`deleteFiles` and the file-retention capabilities; the setup page generates both, and adds
an **Archive lock** row that HEADs the newest monthly dump to prove it really carries a
retention date rather than trusting the settings.

The trade-off is explicit: the backup credential can now delete, which is what an attacker
would use it for. What survives that is exactly the tiers in `lock_days`.

### Setting up the bucket

Since the package never creates a bucket or writes a lifecycle rule, the *Storage Setup*
tab on the backup page (`/backup/setup/`) works out what is missing and shows the
commands to fix it. It has one panel per `BACKUP_CONFIGS` entry - every destination at
once, rather than the one the other pages are on - and is strictly read-only: it checks
whether the bucket exists and reads the rules already on it, and changes nothing.

For a Backblaze destination it generates commands for the
[b2 command-line tool v4](https://b2-command-line-tool.readthedocs.io/):

    b2 account authorize <accountKeyID> <applicationKey>
    b2 bucket create --lifecycle-rule '{"fileNamePrefix": "django_backup/db/hourly/", "daysFromUploadingToHiding": 15, "daysFromHidingToDeleting": 1}' \
                     --lifecycle-rule '{"fileNamePrefix": "django_backup/db/daily/", "daysFromUploadingToHiding": 91, "daysFromHidingToDeleting": 1}' \
                     my-backups allPrivate
    b2 key create --bucket my-backups --name-prefix django_backup/ django-cloud-backup-default-my-backups listBuckets,listFiles,readFiles,writeFiles
    b2 bucket list

Points the page makes, and the reasons behind them:

- The first command needs an **account-level key** with `writeBuckets`,
  `readBucketEncryption`, `writeBucketEncryption` and `writeBucketRetentions`. That is not
  the key the application should use - the restricted key from `b2 key create` is, and the
  page works its capabilities out from what the config actually does:

  | capability | why | when |
  |---|---|---|
  | `listBuckets` | find the bucket; read its versioning, object-lock and lifecycle configuration (the checks on this page) | always |
  | `listFiles` | list backups for the UI, dedup and pruning | always |
  | `readFiles` | download for restore, and read metadata (md5, schema, ip, extensions) | always |
  | `writeFiles` | upload backups, and server-side copies (tier promotion, `changed_files='history'`) | always |
  | `readBuckets` | `Get Bucket Versioning` | always - read-only, for this page |
  | `readBucketRetentions` | `Get Object Lock Configuration` | always - read-only, for this page |
  | `readBucketLifecycleRules` | `Get Lifecycle Configuration` | always - read-only, for this page |
  | `deleteFiles` | `BACKUP_DB_RETENTION` pruning deletes from the application | only when the config prunes; **never** with `db_tiers`, where the bucket does the deleting |
  | `readFileRetentions`, `writeFileRetentions` | stamp Object Lock retention at upload and top it up with `extend_retention` | only when `lock` is set in `BACKUP_STORAGE` |

  Everything else - `writeBuckets`, `deleteBuckets`, `writeKeys`, `bypassGovernance` - is
  deliberately absent: a key that can undo the protection is not protection.
  `--name-prefix` scopes the key to the backup root so it cannot touch anything else in
  the bucket.

  A `restore_only` config gets a different key entirely -
  `listBuckets,listFiles,readFiles,readBuckets`, enough to find and download another
  machine's backups and nothing more - and no bucket commands at all, since the bucket and
  its lifecycle rules belong to the machine that writes them.

  B2 gates each bucket read behind its own capability, and the three `readBucket*` ones
  above exist only in the S3-compatible API - there is no B2 native equivalent, so they
  are easy to miss. Without them the queries fail with `AccessDenied` and the page shows
  `Unknown` rather than "not configured", which is why it names the missing capability in
  the row. **A B2 key cannot be edited after creation** - to add a capability, create a
  new key and update `BACKUP_STORAGE`.
- `b2 bucket update` **replaces the whole rule set**, so when the bucket already exists
  the generated command carries the rules already on it as well. Rules that overlap a
  tier prefix cannot be kept (B2 rejects overlapping rules) and are listed as removed.
  If the existing rules could not be read, the page says so rather than handing over a
  command that would quietly wipe them.
- `--file-lock-enabled` is never generated: a retention lock stops lifecycle expiry.
- `daysFromHidingToDeleting` is always set. B2 buckets keep versions, so expiry only
  *hides* a file - without it the hidden version is kept and charged for forever. The
  per-tier status row flags an existing rule that gets this wrong.
- A schema backed up separately needs its own pair of rules; the page adds them for the
  schema folders it finds.
- Only b2 CLI v4 is supported. v3 (`create-bucket`, `--lifecycleRules`) is deliberately
  not generated. AWS, R2, Azure and Drive destinations get a panel describing what to
  create rather than commands.

A lifecycle rule is JSON, and every shell mangles it differently, so the panel has
**bash / PowerShell / cmd.exe** buttons that re-quote the commands - bash gets
`'{"fileNamePrefix": ...}'`, the Windows shells get `"{\"fileNamePrefix\": ...}"`, and
the PowerShell form carries the `--%` stop-parsing token because neither PowerShell 5.1
nor 7 passes a JSON argument to a native command unaltered. Copy the wrong one and b2
rejects the argument. The page starts on the shell your **browser's** platform suggests -
not the server's, since the page is usually served from a Linux container to a Windows
workstation and it is the workstation that runs b2. The management command takes
`--shell bash|powershell|cmd`, defaulting to the platform it runs on.

Each command has a clipboard button, plus a *Copy all* for the whole block. Copying uses
the browser's clipboard API, which only works over https or on localhost.

`python manage.py storage_setup [--config <name>]` prints the same thing on a server
with no web UI.

## Client-side encryption

By default backups are stored as the provider receives them - anyone with access to
the Drive folder or bucket can read a full database dump. Setting `BACKUP_ENCRYPTION`
encrypts every backup (database dumps, local folder files and S3-source files) on the
client before upload, using chunked AES-256-GCM, so the provider only ever holds
ciphertext:

    BACKUP_ENCRYPTION = True     # reuse the encrypted-credentials SETTINGS_KEY
    BACKUP_ENCRYPTION = '...'    # or a dedicated urlsafe-base64 32-byte key

`True` derives backup keys from the same `SETTINGS_KEY` that already protects the
encrypted credentials - nothing new to manage, and the key never sits in the
repository. The trade-off is coupling: today `SETTINGS_KEY` can be rotated cheaply by
re-encrypting the `.enc` settings files, but once backups are encrypted with it, old
backups need the old key forever. A dedicated key avoids that; generate one with:

    python -c "from encrypted_credentials.encrypted_file import random_key; print(random_key())"

and keep it in the encrypted private settings, not plain settings.py.

Notes:

- Restores are transparent - encrypted and older unencrypted backups are detected by
  content and both restore normally, including `restore_db --local_file`. A wrong or
  missing key fails cleanly before anything reaches `pg_restore`.
- **Losing the key means losing every backup encrypted with it.** Keep a copy of the
  key somewhere that does not depend on the server or the backups themselves.
- File deduplication keeps working: the plaintext md5 is recorded in each file's
  metadata at upload and compared on later runs. If you turn encryption off again,
  already-encrypted file backups re-upload once (their metadata is no longer fetched).
- Database backups briefly need twice the dump size in `BACKUP_LOCAL_DB_DIR` while the
  ciphertext copy is written; folder and S3-source backups encrypt in-stream with no
  extra disk.
- Requires the `cryptography` package (`pip install django-cloud-backup[encryption]`) -
  already present in practice, as encrypted-credentials depends on it.
- `BackupAzureToS3` is a separate rclone-compatible mirror and is not encrypted.
- With multiple backup configurations (below), encryption is set per config rather
  than globally.

## Multiple backup configurations

`BACKUP_CONFIGS` lets one project back up to several destinations with different
behaviour per destination - the classic case being a hardened offsite backup plus an
unencrypted database copy that a staging server restores from:

    BACKUP_CONFIGS = {
        'default': {
            'storage': {'backend': 's3', 'bucket': 'offsite-backups', ..., 'lock': {...}},
            'encryption': True,
            'changed_files': 'protect',
        },
        'staging': {
            'storage': {'backend': 's3', 'bucket': 'staging-transfer', ...},
            'encryption': False,
            'dirs': [],                              # database only, no folder backups
            'retention': [{'days': 1, 'number': 2}], # keep just the latest couple of dumps
            'changed_files': 'overwrite',
        },
    }

Config keys: `storage` (a `BACKUP_STORAGE`-style dict), `encryption`, `db` (include
the database, default True), `db_dir`, `dirs` (as `BACKUP_DIRS`), `azure_dirs` and
`azure_source` (as `AZURE_BACKUP_DIRS` / `AZURE_BACKUP_SOURCE`), `s3_dirs` (as
`S3_BACKUP_DIRS`), `retention`, `changed_files`, `db_tiers`, `restore_only` (read this
destination, never write to it - see *Live, staging and local machines* below).
**A key absent from a config
inherits the corresponding legacy global setting** (`BACKUP_STORAGE`,
`BACKUP_ENCRYPTION`, `BACKUP_DIRS`, ...), so shared values can stay in the globals -
but note that means a config without `'dirs': []` backs up the global `BACKUP_DIRS`.
Without `BACKUP_CONFIGS` the globals simply are the default config, so existing
installations are unaffected.

Running a config:

    python manage.py backup_website --config staging
    python manage.py restore_db --config staging       # e.g. on the staging server

    CELERY_BEAT_SCHEDULE = {
        'backup': {'task': 'cloud_backup.tasks.backup',
                   'schedule': crontab(hour='8-19', minute=10)},
        'backup_staging': {'task': 'cloud_backup.tasks.backup',
                           'schedule': crontab(hour=6, minute=0),
                           'kwargs': {'config': 'staging'}},
    }

The enhanced web UI shows a tab per destination - plus a *Storage Setup* tab, which is
how that page is reached - and everything on the page (the listings, the backup and
restore buttons, verify and empty trash) works on the selected destination. It opens on
the first config that includes the database, since a files-only destination has no
database page: that config's tab goes straight to its file browser instead. A destination whose settings do not resolve gets a warning tab
pointing at *Storage Setup*, which is the page that explains it. The selection is in
the url as `?config=<name>`, so a tab is bookmarkable. Un-parameterised tasks and the
basic (non-enhanced) UI still use the config named `'default'` - or the only entry, if
there is exactly one. Config names may contain any characters; the UI never puts the
name in a modal url.

Note that the file browser's directory index is per config: `/backup/files/0/` is the
first entry in *that* config's `dirs`.

For the staging pattern, the staging server's own
settings point a config at the same transfer bucket and `restore_db` pulls from it -
production never shares its offsite credentials or encryption key with staging. If
the transfer bucket holds an unencrypted production dump, treat the bucket itself as
production-sensitive, or give that config a dedicated key the staging server also
has.

Backups made by one config restore with that config's key: restoring an encrypted
backup through a config with a different key (or none) fails cleanly.


### Live, staging and local machines

The usual three-role setup: one server produces the backups, and the others restore them
to get a copy of live data to work with.

| Role | Backs up | Restores | Backup root |
|---|---|---|---|
| Live | yes, on a schedule | no - web restore off | `backup/live` |
| Staging | occasionally, its own data | from live's dumps | `backup/staging` |
| Local machine | occasionally, its own data | from live's dumps | `backup/local` |

Staging never touches the live server: it reads live's dumps out of the bucket live backs
up to. That destination is marked **`'restore_only': True`**, which is what stops staging
writing into another machine's backups - every write refuses
(`RestoreOnlyConfig`), the web UI offers no backup, empty-trash or undelete action for it,
and *Storage Setup* generates a read-only key for it instead of the usual one. It is a
config setting rather than a matter of credentials because machines commonly share one
encrypted settings file, so the same credential is on all of them and only the config can
tell the roles apart.

    # live/settings.py
    BACKUP_ALLOW_RESTORE = False              # also the default once DEBUG is off
    BACKUP_ENCRYPTION = True
    BACKUP_STORAGE = {'backend': 's3', 'b2': True, 'bucket': 'my-backups',
                      'access_key_id': access_key_id, 'secret_key': secret_key,
                      'root': 'backup/live'}
    BACKUP_DIRS = [(MEDIA_ROOT, 'media')]

    # staging/settings.py - the local machine is the same with root 'backup/local'
    BACKUP_ALLOW_RESTORE = True               # restore from the management page
    BACKUP_ENCRYPTION = True                  # the shared settings key, so live's dumps decrypt
    BACKUP_STORAGE = {...as above...}
    BACKUP_CONFIGS = {
        'default': {'storage': dict(BACKUP_STORAGE, root='backup/staging')},
        'live': {'storage': dict(BACKUP_STORAGE, root='backup/live'),
                 'restore_only': True,
                 'dirs': [], 's3_dirs': []},   # the database is what gets restored
    }

Restoring live's latest dump on staging:

    python manage.py restore_db --config live

or from the management page: pick the *live* tab and use **Drop Restore** on a dump.

Points worth getting right:

- **Give each role its own `root`.** Pruning is scoped to the machine's own public IP so
  servers do not delete each other's dumps, but `db_tiers` promotion has no such filter -
  a staging dump written into live's prefix would be promoted into live's monthly archive.
  Separate roots keep each machine's retention entirely its own.
- **`BACKUP_ALLOW_RESTORE` off on live**, which is what it already is once `DEBUG` is off.
  It is enforced server-side on every web restore endpoint. `manage.py restore_db` is not
  affected by it, so disaster recovery on live stays possible from the command line.
- **Never schedule `cloud_backup.tasks.backup` with `kwargs={'config': 'live'}`** anywhere
  but live. It would refuse anyway, but a beat schedule that raises every night is noise.
- The same encrypted settings file on every machine means `BACKUP_ENCRYPTION = True`
  derives the same key everywhere, so staging can decrypt live's dumps with nothing extra
  to configure. If the machines do **not** share settings, either give the restore_only
  config live's key explicitly, or have live write a second, unencrypted copy to a
  transfer bucket that staging reads (the *Multiple backup configurations* example above).
- Restoring writes to `DATABASES['default']` of the machine doing the restoring - the
  config picks which dumps to read, never where they land.

If a machine only ever restores and backs nothing up at all, `BACKUP_RESTORE_ONLY = True`
sets the flag globally without needing `BACKUP_CONFIGS`.


## Management commands

    python manage.py backup_website
    python manage.py restore_db
    python manage.py storage_setup

## Management page

urls.py

    urlpatterns = [
                    path('backup/', include('cloud_backup.urls')),
                    ....


All URL names live under the `cloud_backup` namespace (e.g.
`reverse('cloud_backup:backup-info')`). Previously the basic management page
used un-namespaced names such as `backup-info`; add the `cloud_backup:` prefix
if you reverse them yourself.

An enhanced version of the management page will be shown if the following django apps are installed

    'django_modals', 'django_datatables', 'django_menus', 'ajax_helpers'

from the following PyPi packages

    django-nested-modals, django-filtered-datatables, django-tab-menus, django-ajax-helpers

### Branding the management page

The enhanced page views build the whole UI (menus, storage info and tables) into a
single HTML string, `{{ backup_content }}`, so it can be dropped into your own
template. Subclass the base views and set `template_name`:

    from cloud_backup.enhanced_views import (BackupBaseView, BackupFilesBaseView,
                                             SchemaTableBaseView, StorageSetupBaseView)

    class MyBackupView(BackupBaseView):
        template_name = 'myapp/backup.html'

    class MySchemaTableView(SchemaTableBaseView):
        template_name = 'myapp/backup.html'

    class MyBackupFilesView(BackupFilesBaseView):
        template_name = 'myapp/backup.html'

    class MyStorageSetupView(StorageSetupBaseView):
        template_name = 'myapp/backup.html'

The template must include the ajax_helpers/datatables/modals libraries and the
page script, then place the content wherever it fits your layout:

    {% load ajax_helpers %}
    {% lib_include 'ajax_helpers' 'Bootstrap' 'FontAwesome' module='ajax_helpers.includes' %}
    {% lib_include 'datatable' module='django_datatables.includes' %}
    {% lib_include 'Modals' module='django_modals.includes' %}
    {{ ajax_helpers_script }}
    ...
    {{ backup_content }}

Register the subclasses with `backup_urlpatterns` so the menu links and modals
(which reverse the standard `cloud_backup:` URL names) point at your views:

    from cloud_backup.urls import backup_urlpatterns

    urlpatterns = [
        path('backup/', include((backup_urlpatterns(
            backup_view=MyBackupView, schema_table_view=MySchemaTableView,
            files_view=MyBackupFilesView, setup_view=MyStorageSetupView), 'cloud_backup'))),
    ]

The unbranded standard page remains the default when using
`include('cloud_backup.urls')`.

### Restoring from the management page

Restore and drop-schema actions on the enhanced management page require

    BACKUP_ALLOW_RESTORE = True

which defaults to the value of `DEBUG`. This is enforced server-side on every
restore endpoint (not just by hiding the buttons), so a production server with
`DEBUG = False` and no `BACKUP_ALLOW_RESTORE` setting cannot be restored from
the web UI even by a superuser. Set `BACKUP_ALLOW_RESTORE = True` on staging and
development machines where restoring is wanted. The `manage.py restore_db`
command is not affected by this setting, so disaster recovery on a live server
remains possible from the command line.

## Browsing and verifying folder backups

When `BACKUP_DIRS`, `AZURE_BACKUP_DIRS` (or `S3_BACKUP_DIRS`) is configured, the
enhanced management page shows a `Backup Files` button that backs up all configured
folders without touching the database, and a single `Files` button opening a file
browser. Its root level lists each configured backup directory as a folder (a cloud
icon marks an Azure source); clicking through navigates the backed-up tree one level
at a time (with breadcrumbs back up), and files show their size, backup date and
checksum - the plaintext md5 recorded with the file at upload time (so it stays
comparable when client-side encryption is enabled).

The `Verify` button on each row re-hashes the file on the server's local disk
(or, for an Azure source, reads the blob's current md5/etag) and compares it with
what was recorded at backup time, reporting:

- `Match` - the source file is identical to its backup
- `Changed` - the source file no longer matches its backup
- `Missing locally` / `Missing from Azure` - the source file has been deleted since
  it was backed up
- `No stored checksum` - the backup has no comparable checksum (e.g. a large
  multipart S3 upload made without md5 metadata)

`Verify All Files` runs the same comparison over the whole directory as a
celery task (the worker must be running) and reports a summary.

The browser carries the same backup buttons as the management page, so a
destination that has no database page - a config with `db` off, whose tab opens
the browser - can still be backed up from the web UI. Its root listing has
`Backup Files`, and inside one directory there is a `Backup <directory>` button
that backs up just that folder source (the whole directory, not the sub-folder
being browsed). The equivalent on the command line is
`backup_website --folders_only --backup_dir <index>`, where the index counts
`BACKUP_DIRS` entries first and then `AZURE_BACKUP_DIRS`.

The browser and verification are read-only and require the same `access_admin`
permission as the rest of the management page; the backup buttons, like every
other backup button, require a superuser. Note that on an S3-compatible
destination with client-side encryption enabled, listing the checksums costs one
metadata request per file, so the page can be slow to load for very large trees.

## Configure Azure folder backups

Media that django-storages keeps in Azure Blob Storage has no local directory for
`BACKUP_DIRS` to back up. `AZURE_BACKUP_DIRS` names prefixes ("folders") in the
container instead, and they are backed up through exactly the same pipeline as a local
directory: the same destination folders (so `media` below appears in the file browser
just as a local `media` would), client-side encryption, `BACKUP_CHANGED_FILES`
protection, object lock and per-file verification. Requires
`pip install django-cloud-backup[azure]`.

settings.py

    # (blob prefix, destination folder). The prefix has no trailing slash and is
    # relative to the container - include django-storages' `location` if one is set.
    # '' is the whole container.
    AZURE_BACKUP_DIRS = [('media', 'media')]

When the project's default file storage is django-storages' `AzureStorage`, its
container and credentials (the `STORAGES['default']['OPTIONS']` or `AZURE_*`
settings) are used and nothing else is needed. To back up some other container, or
when the default storage is not Azure, say which:

    AZURE_BACKUP_SOURCE = {'container': 'media', 'connection_string': connection_string}
    # or {'container': 'media', 'account_url': 'https://<account>.blob.core.windows.net',
    #     'credential': account_key_or_sas_token}

Both are available per config as `azure_dirs` / `azure_source`. Nothing is downloaded
to decide what needs backing up: a blob is skipped when its md5 (which Azure records
for single-request uploads - django-storages' normal case) or otherwise its etag
matches what was stored with the backup, so an unchanged blob costs one listing entry
and a changed one is re-uploaded. The destination is listed once, recursively, rather
than folder by folder, and its stored metadata - a request per object on S3-compatible
storage - is only read for the files the listing cannot settle by hash (blobs Azure holds
no md5 for, encrypted copies, and anything that has actually changed), so a nightly run
over a large, mostly unchanged container is a couple of listings and a few requests, not
one per file. The `backup_azure_s3.BackupAzureToS3` class is
different: a standalone rclone-compatible mirror that bypasses this pipeline.

## Configure S3 folder backups

settings.py

            AWS_ACCESS_KEY_ID = id
            AWS_SECRET_ACCESS_KEY = key
            AWS_PRIVATE_STORAGE_BUCKET_NAME = bucket
            
            S3_BACKUP_DIRS = [('S3-source-folder1', 'google-drive-folder1'),
                              ('S3-source-folder2', 'google-drive-folder2')
            ]
            
## Configure cleaning of old datatabase backups

settings.py

    BACKUP_DB_RETENTION = [{'hours': 1, 'number': 4}, 
                           {'hours': 2, 'number': 10},
                           {'days': 1, 'number': 10},
                           {'months': 1, 'number': 36},
                           ]

Each entry keeps the newest dump in each period for that many periods, and everything
not kept by some entry is deleted after each backup. On an S3-compatible destination
`BACKUP_DB_TIERS` (above) is the alternative: the bucket's lifecycle rules do the
deleting instead, and the application never needs delete permission.

             
## Schedule backup with celery beat

    CELERY_BEAT_SCHEDULE = {
        'backup': {
            'task': 'cloud_backup.tasks.backup',
            'schedule': crontab(hour='8-19', minute=10, day_of_week='mon-fri')
        }
    }
            

## Checking that the backups are current

Error tracking only tells you about a backup that ran and *failed*. A celery beat that has
stopped, or a worker that never picks the task up, raises nothing anywhere - so the package
answers the positive question itself. Every run of `backup`, `promote_db_tiers` and
`extend_retention` is recorded in the database (`BackupRun`; rows are pruned after
`BACKUP_RUN_HISTORY_DAYS`, default 90), and the status check lists what is actually at each
destination and grades it against your beat schedule:

    python manage.py backup_status              # every config; exit code 1 if anything is not OK
    python manage.py backup_status --json

    OK       database: Latest database dump   Sat 29 Aug 09:50 (27 min ago), 285.8 MiB  [older than the 09:50 run]
    OK       database: Dump size vs previous  285.8 MiB vs 285.8 MiB (100%)  [< 80% of previous]
    OK       database: Latest daily copy      Fri 28 Aug, 285.8 MiB  [before Fri 28 Aug]
    OK       database: Latest monthly copy    Jul 2026, 280.1 MiB  [before Jul 2026]
    OK       database: Version audit          nothing unexplained - 9 hidden by expiry, 214 entries listed  [any hidden version or overwrite the retention policy does not explain]
    OK       database: Backup run             succeeded Sat 29 Aug 09:51 (26 min ago)  [older than the 09:50 run]
    OK       database: Tier promotion run     succeeded Sat 29 Aug 06:00 (4 h 17 min ago)  [older than the 06:00 run]
    OK       files: Destination reachable     unity-backup is accessible  [not accessible]
    OK       files: Backup run                succeeded Sat 29 Aug 01:31 (8 h 46 min ago)  [older than the 01:30 run]

The same rows are served as json at `backup/status/` (`?config=<name>` to limit it,
`?refresh=1` to bypass the five-minute cache) for a monitoring agent to read. The view
only admits a superuser or the backup permission; subclass `cloud_backup.views
.BackupStatusView`, override `has_permission(request)` to accept your monitor's credential,
and pass it as `backup_urlpatterns(status_view=...)`.

Rows are graded against the `crontab` entries in `CELERY_BEAT_SCHEDULE` for the config
(a run is late once `grace_minutes` past its slot, counting only the days the crontab
fires - a `mon-fri` schedule is not stale over the weekend), or by plain age
(`max_age_hours`) on a server where the task is not scheduled.

The run history behind those rows is browsable too: the enhanced UI has a Run Log page
at `backup/runs/` (a button on each destination's page), and `BackupRun` is registered
read-only in the Django admin for projects on the basic UI. A dump smaller than `size_drop` of the one before it
is flagged, and with `BACKUP_DB_TIERS` the daily and monthly copies must be there by
`promotion_deadline`. The defaults are in `cloud_backup.config.DEFAULT_STATUS`; override
them for every config with `BACKUP_STATUS = {...}` or per config with a `'status': {...}` key.

## Migrating from django-gdrive-backup

This package was previously published as `django-gdrive-backup`. The rename is a breaking
release; upgrading requires the following changes in your project:

- Install `django-cloud-backup` (and its extras, e.g. `django-cloud-backup[s3]`) instead of
  `django-gdrive-backup`
- `INSTALLED_APPS`: `'gdrive_backup'` → `'cloud_backup'`
- urls.py: `include('gdrive_backup.urls')` → `include('cloud_backup.urls')`, and any
  reverses/`{% url %}` tags use the `cloud_backup:` namespace instead of `gdrive_backup:`
- Celery beat schedules: task names are now `cloud_backup.tasks.*`
  (e.g. `cloud_backup.tasks.backup`)
- Settings renamed: `BACKUP_GDRIVE_DIR` → `BACKUP_ROOT`, `BACKUP_GDRIVE_DB` → `BACKUP_DB_DIR`
  (they apply to every destination backend, not just Google Drive). `BACKUP_TEAM_DRIVE` is
  unchanged.
- Removed legacy method aliases `backup_db_gdrive`, `restore_gdrive_db` and
  `restore_gdrive_folder` - use `backup_db_to_storage`, `restore_db_from_storage` and
  `restore_folder`
- Client-side encryption: the file format constants changed with the rename, so files
  encrypted by pre-release versions of the encryption feature cannot be read. No published
  release included encryption, so this affects no production backups.

Backups already in your storage destination are unaffected - folder layout and metadata
are unchanged, and existing backups restore as before.
