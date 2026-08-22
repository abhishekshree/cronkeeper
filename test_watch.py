"""Plain-assert self-check for watchcron. Run: python3 test_watch.py (no pytest, no network)."""

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'src'))

import yaml

from watch import cron_matches, expected_fire, extract_schedules, load_config, parse_cron_field

UTC = timezone.utc
passed = 0


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except ValueError:
        return True


def check(label: str, cond: bool) -> None:
    global passed
    assert cond, f'FAIL: {label}'
    passed += 1


# --- parse_cron_field ---
check('field *', parse_cron_field('*', 0, 59) == set(range(60)))
check('field */5', parse_cron_field('*/5', 0, 59) == set(range(0, 60, 5)))
check('field list', parse_cron_field('9,18', 0, 23) == {9, 18})
check('field range', parse_cron_field('9-17', 0, 23) == set(range(9, 18)))
check('field step-range', parse_cron_field('*/15', 0, 59) == {0, 15, 30, 45})
check('field range-step 8-20/4', parse_cron_field('8-20/4', 0, 23) == {8, 12, 16, 20})

# --- cron_matches ---
t = datetime(2026, 8, 22, 6, 0, tzinfo=UTC)  # Saturday
check('match exact', cron_matches('0 6 * * *', t))
check('no match hour', not cron_matches('0 7 * * *', t))
check('match dow sat=6', cron_matches('0 6 * * 6', t))
check('no match dow sun=0', not cron_matches('0 6 * * 0', t))
check('match sunday 0/7', cron_matches('0 6 * * 0', datetime(2026, 8, 23, 6, 0, tzinfo=UTC)))
check('match sunday 7', cron_matches('0 6 * * 7', datetime(2026, 8, 23, 6, 0, tzinfo=UTC)))
check('match month', cron_matches('0 6 * 8 *', t))
check('no match month', not cron_matches('0 6 * 9 *', t))
check('dom/dow OR rule', cron_matches('0 6 22 * 1', t))  # Sat the 22nd OR any Monday
check('dom/dow AND when one is *', not cron_matches('0 6 22 * *', datetime(2026, 8, 23, 6, 0, tzinfo=UTC)))
check('bad cron raises', _raises(lambda: cron_matches('* * * *', t)))

# --- PyYAML on-gotcha ---
inline = 'name: x\non:\n  schedule:\n    - cron: "0 6 * * *"\njobs: {}\n'
data = yaml.safe_load(inline)
check('on parsed as True key', 'on' not in data and True in data)
check('extract via gotcha', extract_schedules(data) == ['0 6 * * *'])
check('on: [push] -> []', extract_schedules(yaml.safe_load('on: [push]\njobs: {}')) == [])
check('no on -> []', extract_schedules({'name': 'x'}) == [])
check(
    'multiple crons',
    extract_schedules(
        yaml.safe_load('on:\n  schedule:\n    - cron: "0 6 * * *"\n    - cron: "*/30 * * * *"\n')
    )
    == ['0 6 * * *', '*/30 * * * *'],
)

# --- load_config ---
with tempfile.TemporaryDirectory() as d:
    cfg_path = Path(d) / 'watchcron.yml'
    check('missing config -> {}', load_config(cfg_path) == {})
    cfg_path.write_text('defaults:\n  grace_minutes: 45\nscrape.yml:\n  ignore: true\nbogus_key: 1\n')
    cfg = load_config(cfg_path)
    check('defaults grace', cfg['defaults']['grace_minutes'] == 45)
    check('per-file ignore', cfg['scrape.yml']['ignore'] is True)
    check('unknown keys preserved (ignored by consumer)', 'bogus_key' in cfg)

# --- expected_fire over synthetic grid (no network) ---
now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
f = expected_fire('*/5 * * * *', now, lookback_minutes=26 * 60, grace_minutes=30)
check('*/5 latest eligible fire', f == datetime(2026, 8, 22, 11, 30, tzinfo=UTC))

f = expected_fire('0 6 * * *', now, lookback_minutes=26 * 60, grace_minutes=30)
check('daily 06:00 today', f == datetime(2026, 8, 22, 6, 0, tzinfo=UTC))

# recent fire still inside grace -> fall back to previous fire
now2 = datetime(2026, 8, 22, 12, 3, tzinfo=UTC)
f = expected_fire('*/5 * * * *', now2, lookback_minutes=26 * 60, grace_minutes=10)
check('grace fallback to earlier fire', f == datetime(2026, 8, 22, 11, 50, tzinfo=UTC))

# nothing in window
f = expected_fire('0 6 * * *', now, lookback_minutes=60, grace_minutes=30)
check('no fire in window -> None', f is None)

# notify(): real HTTP POST against a localhost server; asserts payload shapes
from http.server import BaseHTTPRequestHandler, HTTPServer
import json as _json
import threading

import watch

captured = {}

class _Capture(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        captured['body'] = _json.loads(body)
        captured['ctype'] = self.headers['Content-Type']
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass

_srv = HTTPServer(('127.0.0.1', 0), _Capture)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
_url = f'http://127.0.0.1:{_srv.server_port}/hook'

watch.notify(_url, 'slack msg', discord=False)
check('slack payload text key', captured['body'] == {'text': 'slack msg'})
check('json content-type', captured['ctype'] == 'application/json')

watch.notify(_url, 'discord msg', discord=True)
check('discord payload content key', captured['body'] == {'content': 'discord msg'})

_srv.shutdown()
print(f'All {passed} checks passed.')