# SpaceX Store → Discord product monitor

This checks the official SpaceX Store Shopify sitemap every five minutes. On the first run it records the current catalog without flooding the channel. After that, each newly published product URL is sent to Discord with its title, image, price and availability when those details can be read.

It monitors **new product pages only**. It does not purchase anything and it does not currently detect restocks of an existing product URL.

## Important: replace the exposed webhook

A Discord webhook URL is a credential. Delete the webhook URL that was pasted into chat, create a replacement in Discord, and use only the replacement in the secret described below. Never paste it into `monitor.py`, `state.json`, the workflow file, or a Git commit.

## Recommended setup: GitHub Actions

1. Create a new GitHub repository.
2. Upload this project, including the hidden `.github/workflows/monitor.yml` path.
3. Open the repository and go to **Settings → Secrets and variables → Actions**.
4. Select **New repository secret**.
5. Name it exactly `DISCORD_WEBHOOK_URL`.
6. Paste the newly created Discord webhook URL as the value.
7. Open the repository’s **Actions** tab and enable workflows if GitHub asks.
8. Open **SpaceX Store Monitor**, select **Run workflow**, and run it once.

The first successful run sends one initialization message and writes the existing products to `state.json`. Future new products generate Discord embeds automatically.

### Private or public repository?

A public repository is the practical free option for a five-minute schedule. The webhook remains encrypted as a GitHub Actions secret and is not included in the repository. A private repository consumes your included GitHub Actions minutes and a five-minute schedule can exceed a small monthly allowance.

## Change the interval

Edit `.github/workflows/monitor.yml`.

Every 10 minutes:

```yaml
- cron: "3-59/10 * * * *"
```

Every 15 minutes:

```yaml
- cron: "3-59/15 * * * *"
```

GitHub schedules can start late during busy periods, so this is a monitor rather than a guaranteed real-time feed.

## Optional Discord mention

By default, alerts do not ping anyone. To ping a role or user, add another Actions secret named `DISCORD_MENTION`, then add it to the workflow environment:

```yaml
env:
  DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
  DISCORD_MENTION: ${{ secrets.DISCORD_MENTION }}
```

Use a role mention such as `<@&ROLE_ID>` or a user mention such as `<@USER_ID>` as the secret value.

## Run on a normal Linux server instead

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DISCORD_WEBHOOK_URL='YOUR_NEW_WEBHOOK_URL'
python monitor.py
```

Then schedule it with cron:

```cron
*/5 * * * * cd /path/to/spacex-store-monitor && /path/to/spacex-store-monitor/.venv/bin/python monitor.py >> monitor.log 2>&1
```

Keep `state.json` in persistent storage. Do not run multiple copies against the same state file.

## Files

- `monitor.py` — sitemap checking, product detail extraction and Discord alerts
- `.github/workflows/monitor.yml` — five-minute GitHub Actions schedule
- `state.json` — persistent list of previously seen product URLs
- `requirements.txt` — Python dependencies
