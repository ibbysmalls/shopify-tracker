# Shopify New-Arrivals Tracker

Polls the public `products.json` endpoint of every store you follow and sends a Telegram message when something new appears. No dependencies beyond Python 3 (standard library only).

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
