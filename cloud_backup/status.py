"""Are the backups current? A positive answer, for monitoring.

Error tracking only reports a backup task that *fails*. A celery-beat that has stopped, a
worker that never picked the task up, or a task that silently wrote nothing raise no error
at all, so this module lists what is actually at each destination - the newest dump in
each tier, and the last recorded run of each task (models.BackupRun) - and grades it
against the CELERY_BEAT_SCHEDULE in settings, or against a plain maximum age when a
task is not scheduled here (a staging server, local development).

Used by views.BackupStatusView (json for a monitoring agent to read), the backup_status
management command, and anything else that wants ``collect()``. Each check is a row:

    {'metric': 'Latest database dump', 'now': 'Sat 29 Aug 09:50 (27 min ago), 285.8 MiB',
     'alert_at': 'older than the 09:50 run', 'status': 'OK', 'ok': True, ...raw values...}

Thresholds come from the config's ``status`` options (config.DEFAULT_STATUS). Times are
the host's local clock (naive when USE_TZ is off, aware otherwise); the dump names carry
the clock of the worker that wrote them, so ask the instance that runs the backups.
"""
import datetime
import logging
import re

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .config import DEFAULT_CONFIG, config_names, get_config
from .db_tiers import DAILY, MONTHLY, TIER_DIRS, hourly_dir, list_tier, parse_daily, parse_dump
from .models import BackupRun
from .runs import history_days, latest_run

logger = logging.getLogger(__name__)

CACHE_KEY = 'cloud_backup_status'
CACHE_TIMEOUT = 60 * 5      # a status read costs a few requests to the destination

TASKS = {BackupRun.BACKUP: 'cloud_backup.tasks.backup',
         BackupRun.PROMOTE: 'cloud_backup.tasks.promote_db_tiers',
         BackupRun.EXTEND_RETENTION: 'cloud_backup.tasks.extend_retention'}
RUN_METRICS = {BackupRun.BACKUP: 'Backup run', BackupRun.PROMOTE: 'Tier promotion run',
               BackupRun.EXTEND_RETENTION: 'Retention extension run'}

OK = 'OK'
STALE = 'STALE'
MISSING = 'MISSING'
FAILED = 'FAILED'
RUNNING = 'RUNNING'
UNKNOWN = 'UNKNOWN'

MONTHLY_RE = re.compile(r'_(?P<month>\d{4}-\d{2})\.[A-Za-z0-9.]+$')


def check(metric, now, alert_at, status, **detail):
    """One row: what was measured, the threshold, and the verdict."""
    return {'metric': metric, 'now': now, 'alert_at': alert_at, 'status': status,
            'ok': status == OK, **detail}


def unknown(metric, error):
    logger.exception('Backup status: %s could not be checked', metric)
    return check(metric, f'could not check - {type(error).__name__}: {error}', '', UNKNOWN)


# ----------------------------------------------------------------------------------------
# Clocks

def now_local():
    now = timezone.now()
    return timezone.localtime(now) if timezone.is_aware(now) else now


def local(when):
    """A datetime in the same flavour as now_local(): dump names and the legacy storage
    timestamps are naive local, BackupRun rows follow USE_TZ."""
    if when is None:
        return None
    if settings.USE_TZ:
        return timezone.make_aware(when) if timezone.is_naive(when) else timezone.localtime(when)
    return timezone.localtime(when).replace(tzinfo=None) if timezone.is_aware(when) else when


def at(day, time):
    return local(datetime.datetime.combine(day, time))


# ----------------------------------------------------------------------------------------
# The beat schedule

def default_config_name():
    """The config a beat entry without a config kwarg runs - get_config(None)'s choice."""
    names = config_names()
    if DEFAULT_CONFIG in names:
        return DEFAULT_CONFIG
    return names[0] if len(names) == 1 else None


class Schedule:
    """The times a beat crontab fires: sorted (hour, minute) slots plus the day
    constraints, so a mon-fri backup is not reported stale over the weekend. The day
    sets use celery's conventions - day_of_week 0 is Sunday, and day_of_week and
    day_of_month are ANDed, as crontab.is_due does; None means unrestricted."""

    def __init__(self, slots, days_of_week=None, days_of_month=None, months=None):
        self.slots = slots
        self.days_of_week = days_of_week
        self.days_of_month = days_of_month
        self.months = months

    def fires_on(self, day):
        return ((self.days_of_week is None or day.isoweekday() % 7 in self.days_of_week)
                and (self.days_of_month is None or day.day in self.days_of_month)
                and (self.months is None or day.month in self.months))


def schedule_slots(kind, config_name):
    """The Schedule the beat schedule fires the task on for this config, or None when
    it is not scheduled here."""
    task = TASKS[kind]
    for entry in (getattr(settings, 'CELERY_BEAT_SCHEDULE', None) or {}).values():
        if entry.get('task') != task:
            continue
        if ((entry.get('kwargs') or {}).get('config') or default_config_name()) != config_name:
            continue
        crontab = entry.get('schedule')
        hours, minutes = getattr(crontab, 'hour', None), getattr(crontab, 'minute', None)
        if hours is None or minutes is None:
            return None     # an interval schedule - graded by age instead
        return Schedule(sorted((h, m) for h in hours for m in minutes),
                        crontab.day_of_week, crontab.day_of_month, crontab.month_of_year)
    return None


# far enough back to find the last firing of even an annual crontab
MAX_LOOKBACK_DAYS = 366 + 31


def latest_due_slot(now, schedule, grace):
    """The most recent scheduled time whose run should have finished by now, or None
    when no slot has come due yet. Days the crontab does not fire on are skipped, so
    the last due slot of a mon-fri schedule stays Friday's until Monday morning."""
    for days_back in range(MAX_LOOKBACK_DAYS):
        day = now.date() - datetime.timedelta(days=days_back)
        if not schedule.fires_on(day):
            continue
        due = [at(day, datetime.time(hour, minute)) for hour, minute in schedule.slots
               if at(day, datetime.time(hour, minute)) + grace <= now]
        if due:
            return max(due)
    return None


def firing_days(today, schedule, count):
    """The most recent `count` days the schedule fires on, today first - or simply the
    last `count` days when there is no schedule. Bounds the listings and expectations
    that would otherwise assume a backup every day: on a mon-fri schedule the newest
    dump is Friday's all weekend, and so is the newest daily copy."""
    days = []
    for days_back in range(MAX_LOOKBACK_DAYS):
        day = today - datetime.timedelta(days=days_back)
        if schedule is None or schedule.fires_on(day):
            days.append(day)
            if len(days) == count:
                break
    return days


def grade_time(now, when, schedule, options):
    """(status, alert_at text) for something that should be as new as the last scheduled
    run - or, with no schedule, no older than max_age_hours."""
    if schedule:
        due = latest_due_slot(now, schedule, datetime.timedelta(minutes=options['grace_minutes']))
        day = '' if due is None or due.date() == now.date() else f'{due:%a} '
        alert_at = f'older than the {day}{due:%H:%M} run' if due else 'no run due yet'
        return (OK if due is None or (when is not None and when >= due) else STALE), alert_at
    max_age = datetime.timedelta(hours=options['max_age_hours'])
    alert_at = f'older than {options["max_age_hours"]} h (no beat schedule here)'
    return (OK if when is not None and now - when <= max_age else STALE), alert_at


# ----------------------------------------------------------------------------------------
# Formatting

def fmt(when):
    return when.strftime('%a %d %b %H:%M') if when else '-'


def fmt_age(now, when):
    if when is None:
        return '-'
    minutes = int((now - when).total_seconds() // 60)
    if minutes < 60:
        return f'{minutes} min ago'
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f'{hours} h {minutes:02d} min ago'
    return f'{hours // 24} days ago'


def fmt_size(size):
    if size is None:
        return '-'
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if size < 1024 or unit == 'GiB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024


# ----------------------------------------------------------------------------------------
# What is at the destination

def newest_dumps(backup, today, schedule=None):
    """Database dumps newest first, as (time taken, size, key). Tiered layouts list the
    hourly folders of today and the two most recent days the schedule fires on - enough
    for the newest dump and the one before it, even for the first run of the day, and
    over a weekend the schedule skips; the flat legacy layout is listed whole."""
    storage = backup.storage
    db = backup.get_backup_db()
    dumps = []
    if backup.config.db_tiers:
        # today is always listed - a manual run lands there whatever the schedule says
        days = dict.fromkeys([today] + firing_days(today, schedule, 2))
        for day in days:
            folder = storage.ensure_folder(hourly_dir(day), parent=db.base_backup_dir)
            for f in storage.list_files(folder):
                parsed = parse_dump(f['name'])
                if parsed:
                    dumps.append((local(parsed[1]), f['size'], f['id']))
    else:
        for f in db.get_db_backup_files():
            parsed = parse_dump(f['name'])
            dumps.append((local(parsed[1] if parsed else f['created']), f['size'], f['id']))
    return sorted(dumps, reverse=True)


def promoted_copies(backup, tier):
    """Every copy in the daily or monthly tier, from every server, as (period, size, key),
    oldest first - a date for daily, the first of the month for monthly."""
    storage = backup.storage
    found = []
    for _server, f in list_tier(storage, storage.ensure_folder(backup.config.db_dir), tier):
        parsed = parse_daily(f['name'])
        if parsed:
            found.append((parsed[1], f['size'], f['id']))
            continue
        match = MONTHLY_RE.search(f['name'])
        if match:
            found.append((datetime.date.fromisoformat(match.group('month') + '-01'), f['size'], f['id']))
    return sorted(found)


def previous_month(day):
    return (day.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)


def next_month(day):
    return (day.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)


def promotion_deadline(options):
    return datetime.time.fromisoformat(options['promotion_deadline'])


# ----------------------------------------------------------------------------------------
# The version audit - is anything hidden that the retention policy did not hide?

def tier_in_key(key):
    for tier in TIER_DIRS:
        if f'/{tier}/' in key:
            return tier
    return None


def name_time(key):
    """When the dump a key names was taken, from its name - what dates a delete marker
    whose version has already been purged."""
    name = key.rsplit('/', 1)[-1]
    parsed = parse_dump(name)
    if parsed:
        return parsed[1]
    parsed = parse_daily(name)
    if parsed:
        return datetime.datetime.combine(parsed[1], datetime.time())
    match = MONTHLY_RE.search(name)
    return datetime.datetime.fromisoformat(match.group('month') + '-01') if match else None


def audit_versions(entries, expire_days):
    """(unexplained, explained): what the retention policy does not account for among a
    db folder's versions and delete markers. Dumps get unique names and are only ever
    hidden by expiry - the bucket's rule or the app's prune - once they are expire_days
    old, so a dump hidden younger than that, anything hidden in a tier that never
    expires, and a key holding two real versions (an overwrite) are someone else's
    doing. Each finding is {'key', 'kind': 'hidden'|'overwritten'|'purged', 'at', 'age'}:
    'purged' is a young dump whose hidden version is already gone - evidence, no longer
    recoverable."""
    by_key = {}
    for entry in entries:
        group = by_key.setdefault(entry['key'], {'versions': [], 'markers': []})
        group['markers' if entry['marker'] else 'versions'].append(entry)
    unexplained, explained = [], 0
    for key, group in by_key.items():
        versions = sorted(group['versions'], key=lambda v: v['modified'])
        if len(versions) > 1:
            unexplained.append({'key': key, 'kind': 'overwritten', 'at': versions[-1]['modified'],
                                'age': versions[-1]['modified'] - versions[0]['modified']})
        tier = tier_in_key(key)
        limit = expire_days.get(tier) if tier else None
        for marker in group['markers']:
            hidden = [v for v in versions if v['modified'] <= marker['modified']]
            uploaded = hidden[-1]['modified'] if hidden else name_time(key)
            if uploaded is None:
                continue
            age = marker['modified'] - uploaded
            # prune works in whole days, so a legitimate hide can be a day early
            if limit is not None and age >= datetime.timedelta(days=limit - 1):
                explained += 1
            else:
                unexplained.append({'key': key, 'kind': 'hidden' if hidden else 'purged',
                                    'at': marker['modified'], 'age': age})
    return unexplained, explained


def fmt_span(delta):
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f'{minutes} min old'
    if minutes < 48 * 60:
        return f'{minutes // 60} h old'
    return f'{minutes // (24 * 60)} days old'


def versions_check(backup, now):
    """The Version audit row, or None where the storage has no versions. Costs one
    listing of the db folder including its hidden versions."""
    config = backup.config
    entries = backup.storage.list_versions(backup.get_backup_db().base_backup_dir['id'] + '/')
    if entries is None:
        return None
    metric, alert_at = 'Version audit', 'any hidden version or overwrite the retention policy does not explain'
    unexplained, explained = audit_versions(entries, config.db_tier_expire_days)
    if not unexplained:
        return check(metric, f'nothing unexplained - {explained} hidden by expiry, {len(entries)} entries listed',
                     alert_at, OK, hidden_by_expiry=explained, entries=len(entries))
    recoverable = [u for u in unexplained if u['kind'] != 'purged']
    # the purge clock started when the dump was hidden; the earliest one sets the deadline
    deadline = (local(min(u['at'] for u in recoverable)) + datetime.timedelta(days=config.db_tier_purge_days)
                if recoverable else None)
    listed = ', '.join(f"{u['key'].rsplit('/', 1)[-1]} ({u['kind']}, {fmt_span(u['age'])})" for u in unexplained[:4])
    if len(unexplained) > 4:
        listed += f', ... {len(unexplained) - 4} more'
    return check(metric,
                 f'{len(unexplained)} dump(s) hidden or overwritten that the retention policy does not explain'
                 + (f' - recoverable until {fmt(deadline)}' if deadline else ' - already purged') + f': {listed}',
                 alert_at, FAILED, recover_by=deadline.isoformat() if deadline else None,
                 unexplained=[dict(u, at=local(u['at']).isoformat(), age=fmt_span(u['age'])) for u in unexplained],
                 hidden_by_expiry=explained)


# ----------------------------------------------------------------------------------------
# The checks

def dump_checks(now, dumps, schedule, options):
    if not dumps:
        return [check('Latest database dump', 'none found', 'missing', MISSING)]
    taken, size, key = dumps[0]
    status, alert_at = grade_time(now, taken, schedule, options)
    checks = [check('Latest database dump', f'{fmt(taken)} ({fmt_age(now, taken)}), {fmt_size(size)}',
                    alert_at, status, taken=taken.isoformat(), size=size, key=key)]
    if len(dumps) > 1:
        previous_size = dumps[1][1]
        ratio = size / previous_size if previous_size else None
        checks.append(check('Dump size vs previous', f'{fmt_size(size)} vs {fmt_size(previous_size)}'
                            + (f' ({ratio:.0%})' if ratio is not None else ''),
                            f'< {options["size_drop"]:.0%} of previous',
                            OK if ratio is None or ratio >= options['size_drop'] else FAILED,
                            previous_size=previous_size, previous_key=dumps[1][2]))
    return checks


def daily_check(now, newest, options, schedule=None):
    if newest is None:
        return check('Latest daily copy', 'none', 'missing', MISSING)
    day, size, key = newest
    # a day's copy appears the morning after, once promotion has run; the copy that must
    # be there is the last day the backups ran before that, so a mon-fri schedule's
    # Friday copy is current until Tuesday morning
    cutoff = now.date() - datetime.timedelta(days=1 if now.time() >= promotion_deadline(options) else 2)
    expected = (firing_days(cutoff, schedule, 1) or [cutoff])[0]
    return check('Latest daily copy', f'{day:%a %d %b}, {fmt_size(size)}', f'before {expected:%a %d %b}',
                 OK if day >= expected else STALE, day=day.isoformat(), size=size, key=key)


def monthly_check(now, newest, options, oldest_daily=None):
    """oldest_daily: the day the daily tier starts, so a destination too young to have
    seen a month end is not reported as missing its monthly copy."""
    expected = previous_month(now.date())
    if now.date().day == 1 and now.time() < promotion_deadline(options):
        expected = previous_month(expected)
    if newest is None:
        if oldest_daily and oldest_daily >= next_month(expected):
            return check('Latest monthly copy', f'none yet - daily copies start {oldest_daily:%d %b %Y}',
                         f'first due 1 {next_month(oldest_daily):%b %Y}', OK)
        return check('Latest monthly copy', 'none', 'missing', MISSING)
    month, size, key = newest
    return check('Latest monthly copy', f'{month:%b %Y}, {fmt_size(size)}', f'before {expected:%b %Y}',
                 OK if month >= expected else STALE, month=month.strftime('%Y-%m'), size=size, key=key)


def destination_check(backup):
    state = backup.storage.destination_status(backup.config.root)
    return check('Destination reachable', state.get('detail') or state.get('status', ''), 'not accessible',
                 OK if state.get('state') == 'ok' else FAILED)


def run_check(now, kind, run, schedule, options):
    metric = RUN_METRICS[kind]
    if run is None:
        return check(metric, f'no run recorded in the last {options.get("history_days", history_days())} days',
                     'missing', MISSING)
    detail = {'run_id': run.pk, 'run_status': run.status, 'started': local(run.started).isoformat(),
              'finished': local(run.finished).isoformat() if run.finished else None, 'detail': run.detail}
    started, finished = local(run.started), local(run.finished)
    if run.status == BackupRun.FAILURE:
        return check(metric, f'FAILED {fmt(finished)} ({fmt_age(now, finished)}): {run.error[:200]}',
                     'any failure', FAILED, **detail)
    if run.status == BackupRun.RUNNING:
        stuck = now - started > datetime.timedelta(hours=options['stuck_hours'])
        return check(metric, f'running since {fmt(started)} ({fmt_age(now, started)})',
                     f'running for more than {options["stuck_hours"]} h', FAILED if stuck else RUNNING, **detail)
    status, alert_at = grade_time(now, finished, schedule, options)
    return check(metric, f'succeeded {fmt(finished)} ({fmt_age(now, finished)})', alert_at, status, **detail)


def config_checks(backup, now):
    """Every check for one destination. A collector that blows up contributes an UNKNOWN
    row rather than taking the others with it - the caller must still get an answer when
    the destination is unreachable."""
    config = backup.config
    options = config.status
    checks = []
    if config.include_db:
        try:
            schedule = schedule_slots(BackupRun.BACKUP, config.name)
            checks += dump_checks(now, newest_dumps(backup, now.date(), schedule), schedule, options)
            if config.db_tiers:
                daily = promoted_copies(backup, DAILY)
                monthly = promoted_copies(backup, MONTHLY)
                checks.append(daily_check(now, daily[-1] if daily else None, options, schedule))
                checks.append(monthly_check(now, monthly[-1] if monthly else None, options,
                                            daily[0][0] if daily else None))
        except Exception as e:  # noqa: BLE001
            checks.append(unknown('Database dumps', e))
        if config.db_tiers:
            # only for tiered configs: their retention is exact enough to say what a
            # legitimate hide looks like, which BACKUP_DB_RETENTION's hourly pruning is not
            try:
                row = versions_check(backup, now)
                if row:
                    checks.append(row)
            except Exception as e:  # noqa: BLE001
                checks.append(unknown('Version audit', e))
    if config.file_sources or config.s3_dirs or not config.include_db:
        # a listing above already proved a database destination reachable
        try:
            checks.append(destination_check(backup))
        except Exception as e:  # noqa: BLE001
            checks.append(unknown('Destination reachable', e))
    if config.restore_only:
        return checks    # nothing runs against a restore-only destination from here
    kinds = [BackupRun.BACKUP]
    if config.db_tiers:
        kinds.append(BackupRun.PROMOTE)
    if (config.storage_settings.get('lock') or {}).get('min_days'):
        kinds.append(BackupRun.EXTEND_RETENTION)
    for kind in kinds:
        try:
            checks.append(run_check(now, kind, latest_run(config.name, kind), schedule_slots(kind, config.name),
                                    options))
        except Exception as e:  # noqa: BLE001
            checks.append(unknown(RUN_METRICS[kind], e))
    return checks


def backup_for(name):
    from .backup import Backup
    return Backup(config=name)


def collect(names=None, now=None):
    """Every check for every configured destination (or just `names`), ready to
    serialise. `checks` is the flat list a monitor reads, metric names prefixed with the
    config name when there is more than one; `configs` has the same rows per destination."""
    now = now or now_local()
    names = list(names) if names else config_names()
    configs, checks, buckets = {}, [], {}
    for name in names:
        try:
            backup = backup_for(name)
            storage = backup.config.storage_settings
            buckets[name] = storage.get('bucket') or storage.get('container') or storage.get('backend', '')
            rows = config_checks(backup, now)
        except Exception as e:  # noqa: BLE001 - a misconfigured destination is a row, not a crash
            rows = [unknown(f'{name} configuration', e)]
        configs[name] = {'destination': buckets.get(name, ''), 'checks': rows,
                         'ok': all(r['ok'] for r in rows)}
        prefix = f'{name}: ' if len(names) > 1 else ''
        checks += [dict(r, metric=prefix + r['metric']) for r in rows]
    problems = [c['metric'] for c in checks if not c['ok']]
    return {'generated': now.replace(microsecond=0).isoformat(),
            'ok': not problems,
            'problems': problems,
            'buckets': buckets,
            'configs': configs,
            'checks': checks}


def get_backup_status(names=None, refresh=False):
    """collect(), cached for CACHE_TIMEOUT so a monitor polling the endpoint does not
    list the destination every time."""
    key = f'{CACHE_KEY}:{",".join(names) if names else "*"}'
    if not refresh:
        status = cache.get(key)
        if status is not None:
            return status
    status = collect(names)
    cache.set(key, status, CACHE_TIMEOUT)
    return status
