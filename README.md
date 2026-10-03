# Shopify New-Arrivals Tracker

Polls the public `products.json` endpoint of every store you follow and sends a Telegram message when a new product ID appears, or when a previously seen variant comes back in stock. No dependencies beyond Python 3 (standard library only).

## 1. Verify the store list

```bash
python3 tracker.py verify
```

This hits each domain in `stores.json` and tells you which ones respond. For any failures, open the store's page in the Shop app, tap through to its website, and correct the `domain` field. Then set `"verified": true` for every working store (or just run with `--all`).

Two stores need your eyes specifically:

- **Bloomr**: the name is ambiguous. Config currently points at bloomr.com (the UAE decor brand). If yours is a different Bloomr, fix the domain.
- **HINOYA**: their Shopify storefront may live on a different domain than hinoya.co.jp.
- **New Era / Culture Kings**: big brands sometimes run custom stacks; verify will tell you.

## 2. Set up Telegram (2 minutes)

1. Message **@BotFather** on Telegram → `/newbot` → pick a name. Copy the token.
2. Message **@userinfobot** → it replies with your numeric chat ID.
3. Send your new bot any message once (bots can't message you first).

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC..."
export TELEGRAM_CHAT_ID="123456789"
```

## 3. Test

```bash
python3 tracker.py run            # first run seeds state silently
python3 tracker.py run --dry-run  # later runs: prints what it would send
```

## 4. Filters (optional)

Edit `filters` in `stores.json`:

```json
"filters": {
  "include_keywords": ["fitted", "59fifty"],
  "exclude_keywords": ["youth", "toddler"],
  "notify_only_available": true
}
```

Empty `include_keywords` means notify on everything.

Stores that restock existing product pages (instead of creating new IDs) need
per-variant history, which is stored in `seen.json` under `_stock`. The first
run after an upgrade seeds that silently. To also poll a Shopify collection
such as Okayama Denim's new-restocks list:

```json
{
  "name": "Okayamadenim",
  "domain": "www.okayamadenim.com",
  "verified": true,
  "collections": ["new-restocks"]
}
```

That hits `/collections/<handle>/products.json` in addition to the store-wide
catalogue. Set `"catalog": false` to poll only the collection(s).

## 5. Schedule on the Mac Studio (launchd)

Save as `~/Library/LaunchAgents/com.tracker.shopify.plist`, fixing the two paths and credentials:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.tracker.shopify</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>/Users/YOURNAME/shopify-tracker/tracker.py</string>
    <string>run</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>TELEGRAM_BOT_TOKEN</key><string>123456:ABC...</string>
    <key>TELEGRAM_CHAT_ID</key><string>123456789</string>
  </dict>
  <key>StartInterval</key><integer>900</integer>
  <key>StandardOutPath</key><string>/tmp/shopify-tracker.log</string>
  <key>StandardErrorPath</key><string>/tmp/shopify-tracker.err</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.tracker.shopify.plist
```

900 seconds = every 15 minutes. Runs silently in the background, survives reboots.

## 6. Or run it free on GitHub Actions (works while the Mac sleeps)

Push this folder to a **private** repo, add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as repo secrets, and create `.github/workflows/track.yml`:

```yaml
name: track
on:
  schedule:
    - cron: "*/15 * * * *"
  workflow_dispatch:
permissions:
  contents: write
jobs:
  poll:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python3 tracker.py run
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
      - name: Persist seen state
        run: |
          git config user.name bot
          git config user.email bot@users.noreply.github.com
          git add seen.json && git commit -m "state" || true
          git push
```

Note: GitHub schedules aren't exact; expect 15 to 30 minute effective intervals.

## 7. Add or remove stores from Telegram

Send your bot a store link (share it from the browser or paste it) and the next
run validates it, adds it to stores.json, seeds it silently, and replies to
confirm. Send `remove domain.com` to drop a store. Only messages from your own
chat ID are honored. Requires the workflow to commit stores.json as well as
seen.json (see track.yml).

## 8. Quiet health notifications

Product-drop and restock alerts are unchanged. Health problems are grouped into
one message (at most 15 store names), rather than a failed/back pair per store:

- Individual fetch failures must last at least **1 hour and 3 failed polls**
- Empty catalogues lasting 24 hours and unverified stores join the same summary
- Ordinary health summaries repeat at most **once per 24 hours**, including when
  stores briefly recover or different stores start failing
- A majority of verified stores failing for **3 consecutive polling runs** can
  send one immediate widespread-outage escalation during that cooldown. This
  escalation is itself limited to once per 24 hours, so a normal alert followed
  by a new widespread outage can produce two messages in that window
- Recovery is silent and requires **3 successful non-empty polls**. Deferred
  stores do not count as failures or recoveries
- Cooldowns and incident history persist in `seen.json` under `_health`. Existing
  state migrates automatically; no reset or edits to `seen.json` are needed.
  Failed sends do not consume the cooldown, and dry runs do not send or consume
  the health-summary cooldown

Optional top-level `health` settings in `stores.json`:

```json
"health": {
  "fail_alerts_after": 3,
  "fail_alert_hours": 1,
  "recovery_polls": 3,
  "alert_cooldown_hours": 24,
  "empty_alert_hours": 24,
  "digest_every_days": 7,
  "quiet_flag_days": 14
}
```

The weekly quiet-store digest remains separate; set `digest_every_days` to 0 to
turn it off. Poll scheduling, retry behavior, filters and product notifications
are not affected by these settings. Health summaries reduce noise; they do not
resolve upstream HTTP 429 rate limiting. Run logs retain per-store errors.

Run offline regression tests without polling stores or sending Telegram messages:

```bash
python3 -m unittest -v
```
