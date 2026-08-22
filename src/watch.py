"""jobwatch — alerts when scheduled GitHub Actions workflows miss or fail.

Stdlib + PyYAML only. Import-safe: all work happens in main().
"""

import glob
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

API = 'https://api.github.com'
HEADERS = {
    'Accept': 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
}


def parse_cron_field(field: str, lo: int, hi: int) -> set[int]:
    """One cron field -> set of allowed ints. Supports *, N, lists, a-b, */n, a-b/n."""
    out: set[int] = set()
    for part in field.split(','):
        base, _, step_s = part.partition('/')
        step = int(step_s) if step_s else 1
        if base == '*':
            rng: range = range(lo, hi + 1)
        elif '-' in base:
            a, b = base.split('-', 1)
            rng = range(int(a), int(b) + 1)
        elif step_s:
            rng = range(int(base), hi + 1)  # "5/15" == 5-59/15 per cron convention
        else:
            rng = range(int(base), int(base) + 1)
        out.update(v for v in rng[::step] if lo <= v <= hi)
    return out


def cron_matches(cron: str, dt: datetime) -> bool:
    """True if dt (UTC) matches the 5-field cron expression."""
    fields = cron.split()
    if len(fields) != 5:
        raise ValueError(f'unsupported cron expression: {cron!r}')
    minute, hour, dom, month, dow = fields
    # ponytail: hand-rolled cron match, swap in croniter if users hit unsupported expressions
    dom_ok = dt.day in parse_cron_field(dom, 1, 31)
    dow_ok = (dt.weekday() + 1) % 7 in parse_cron_field('0' if dow == '7' else dow, 0, 6)
    if dom != '*' and dow != '*':  # standard cron: both restricted -> OR
        day_ok = dom_ok or dow_ok
    else:
        day_ok = dom_ok and dow_ok
    return (
        dt.minute in parse_cron_field(minute, 0, 59)
        and dt.hour in parse_cron_field(hour, 0, 23)
        and dt.month in parse_cron_field(month, 1, 12)
        and day_ok
    )


def expected_fire(
    cron: str, now: datetime, lookback_minutes: int, grace_minutes: int
) -> datetime | None:
    """Latest fire time F in [now - lookback - grace, now] with now >= F + grace, else None."""
    start = (now - timedelta(minutes=lookback_minutes + grace_minutes)).replace(
        second=0, microsecond=0
    )
    t = start
    found: datetime | None = None
    while t <= now.replace(second=0, microsecond=0):
        if cron_matches(cron, t) and now >= t + timedelta(minutes=grace_minutes):
            found = t
        t += timedelta(minutes=1)
    return found


def load_config(path: Path) -> dict[str, Any]:
    """Parse .github/jobwatch.yml; missing or malformed -> {}."""
    try:
        data = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        return {}
    return data if isinstance(data, dict) else {}


def extract_schedules(workflow: dict[str, Any]) -> list[str]:
    """Cron strings under on.schedule. Handles the PyYAML `on:` -> True gotcha."""
    trigger = workflow.get('on')
    if not isinstance(trigger, dict):
        trigger = workflow.get(True)  # PyYAML parses bare `on:` as boolean True
    if not isinstance(trigger, dict):
        return []
    schedule = trigger.get('schedule')
    if not isinstance(schedule, list):
        return []
    return [item['cron'] for item in schedule if isinstance(item, dict) and 'cron' in item]


class RateLimited(Exception):
    pass


def api_get(url: str, token: str) -> Any:
    req = urllib.request.Request(url, headers={**HEADERS, 'Authorization': f'Bearer {token}'})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 403 and e.headers.get('X-RateLimit-Remaining') == '0':
            raise RateLimited from e
        raise


def notify(webhook: str, msg: str, discord: bool) -> None:
    body = {'content': msg} if discord else {'text': msg}
    req = urllib.request.Request(
        webhook,
        data=json.dumps(body).encode(),
        headers={'Content-Type': 'application/json'},
    )
    urllib.request.urlopen(req)


def main() -> None:
    repo = os.environ['GITHUB_REPOSITORY']
    token = os.environ.get('INPUT_TOKEN', '')
    webhook = os.environ.get('INPUT_WEBHOOK', '')
    discord = os.environ.get('INPUT_DISCORD', '').lower() == 'true'
    check_failures = os.environ.get('INPUT_ON_FAILURE', 'true').lower() == 'true'
    config = load_config(Path('.github/jobwatch.yml'))
    defaults = config.get('defaults', {})
    grace = int(defaults.get('grace_minutes', os.environ.get('INPUT_GRACE_MINUTES', '30')))
    lookback_hours = int(os.environ.get('INPUT_LOOKBACK_HOURS', '26'))
    lookback = lookback_hours * 60
    now = datetime.now(timezone.utc)

    files = sorted(glob.glob('.github/workflows/*.yml')) + sorted(
        glob.glob('.github/workflows/*.yaml')
    )
    alerts: list[str] = []
    try:
        for path in files:
            name = Path(path).name
            override = config.get(name, {})
            if isinstance(override, dict) and override.get('ignore'):
                print(f'SKIPPED {name} (ignore)')
                continue
            try:
                data = yaml.safe_load(Path(path).read_text()) or {}
            except yaml.YAMLError as e:
                print(f'WARNING: skipping {name}: bad YAML ({e})')
                continue
            display = data.get('name') if isinstance(data.get('name'), str) else name
            crons = extract_schedules(data)
            file_grace = int(override.get('grace_minutes', grace)) if isinstance(override, dict) else grace
            for cron in crons:
                fired = expected_fire(cron, now, lookback, file_grace)
                if fired is None:
                    print(f'NO-FIRE-IN-WINDOW {display} ({name}) cron={cron!r}')
                    continue
                iso = fired.strftime('%Y-%m-%dT%H:%M:%SZ')
                q = urllib.parse.quote(f'>={iso}', safe='')
                url = (
                    f'{API}/repos/{repo}/actions/workflows/{name}'
                    f'/runs?created={q}&per_page=1'
                )
                runs = api_get(url, token)
                if runs.get('total_count', 0) == 0:
                    msg = (
                        f':rotating_light: jobwatch MISSED run: *{display}* (`{name}`)\n'
                        f'cron `{cron}` expected fire at `{iso}` '
                        f'(grace {file_grace}m) but no run was created.'
                    )
                    alerts.append(msg)
                    print(f'MISSED {display} ({name}) cron={cron!r} expected={iso}')
                elif check_failures:
                    latest = api_get(
                        f'{API}/repos/{repo}/actions/workflows/{name}/runs?per_page=1', token
                    )
                    for run in latest.get('workflow_runs', []):
                        if run.get('conclusion') == 'failure':
                            msg = (
                                f':x: jobwatch FAILED run: *{display}* (`{name}`)\n'
                                f'{run.get("html_url")}'
                            )
                            alerts.append(msg)
                            print(f'FAILED {display} ({name}) {run.get("html_url")}')
                        else:
                            print(f'OK {display} ({name}) cron={cron!r}')
                        break
                else:
                    print(f'OK {display} ({name}) cron={cron!r}')
    except RateLimited:
        print('WARNING: GitHub API rate limit hit; skipping remaining checks')

    # ponytail: no state/dedup — alerts repeat every run while broken; add a store if noisy.
    for msg in alerts:
        if webhook:
            notify(webhook, msg, discord)
        else:
            print(msg)


if __name__ == '__main__':
    main()
