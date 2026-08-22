# watchcron

A cron monitor for GitHub Actions that runs on your own minutes and alerts a Slack-compatible webhook when a scheduled workflow skips its window or fails.

## The problem

GitHub's scheduler silently skips and delays scheduled workflows, and it never tells you. If a public repo goes inactive for 60 days, GitHub auto-disables all of its schedules with almost no notice. And when a scheduled run does fire and fail, there is no notification unless someone is watching the Actions tab.

## How it works

You add one small watchdog workflow to your repo on its own cron schedule (every 30 minutes works well). Each run, watchcron reads the `on: schedule:` cron lines from every workflow file in the repo, checks each workflow's recent run history with your repo token, and sends an alert when:

- a scheduled workflow produced no run inside its expected window, or
- the latest run of a scheduled workflow failed.

There is no server, no hosted dashboard, no account anywhere but GitHub, and nothing to edit in your existing workflows. Jobwatch figures out the schedules by reading the files itself.

## Quick start

1. Create an incoming webhook in Slack (or any Slack-compatible service) and copy the URL.
2. Save that URL as a repository secret named `WEBHOOK_URL`.
3. Commit this file as `.github/workflows/watchcron.yml`:

```yaml
name: WatchCron
on:
  schedule:
    - cron: '*/30 * * * *'
  workflow_dispatch:
jobs:
  watch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: abhishekshree/watchcron@v1
        with:
          webhook: ${{ secrets.WEBHOOK_URL }}
```

That is the whole setup. Jobwatch discovers every other workflow's schedule on the next run.

## Configuration

### Action inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `webhook` | yes | | Incoming webhook URL. Slack format unless you set `discord`. |
| `discord` | no | `'false'` | Set `'true'` to send Discord's payload format instead of Slack's. |
| `grace-minutes` | no | `30` | How many minutes late a run can be before it counts as missed. GitHub scheduler jitter reaches 15 to 60 minutes, so don't set this below 15. |
| `lookback-hours` | no | `26` | How far back to scan run history. 26 covers daily jobs plus one hour of slack. |
| `on-failure` | no | `'true'` | Also alert when the latest run of a scheduled workflow failed. |

### Per-workflow overrides

For finer control, commit `.github/watchcron.yml` to the monitored repo. Keys are workflow filenames:

```yaml
defaults:
  grace_minutes: 45

nightly-backup.yml:
  grace_minutes: 120
old-migration-job.yml:
  ignore: true
```

## What the alerts look like

A missed schedule:

```
watchcron: missed schedule
nightly-backup.yml expected a run near 02:00 UTC.
No run found within 120 minutes of its window.
Last successful run: Aug 21, 02:03 UTC
```

A failed scheduled run:

```
watchcron: scheduled run failed
nightly-backup.yml failed at Aug 22, 02:00 UTC.
https://github.com/abhishekshree/watchcron/actions/runs/1234567890
```

## Limitations

- No memory between runs. While something stays broken, watchcron re-alerts on every check until it is fixed. That nagging is intentional; silence should mean everything passed.
- The cron matcher supports `*`, comma lists, ranges, and `*/n` steps only. Month/day names and `@syntax` are not parsed, so workflows using them are skipped.
- On public repos, GitHub disables schedules after 60 days without activity. Jobwatch reports this as a missed schedule, but only after it has already happened.
- The monitored repo must be checked out first (the quick-start workflow above already does this), since watchcron reads its workflow files from disk.

Ping-based monitors like Healthchecks.io, Cronitor, and CronSignal are good hosted products with monthly pricing, and they need a ping step edited into every job you want watched. Jobwatch trades their features for zero infrastructure and zero edits to existing workflows.

## License

MIT.
