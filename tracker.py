#!/usr/bin/env python3
"""
Shopify new-product tracker.

Usage:
  python3 tracker.py verify          # check which stores expose products.json
  python3 tracker.py run             # poll all verified stores once, notify on new items
  python3 tracker.py run --dry-run   # poll but print instead of sending Telegram messages

Environment variables (required for notifications):
  TELEGRAM_BOT_TOKEN   from @BotFather
  TELEGRAM_CHAT_ID     your numeric chat id (message @userinfobot to get it)

State is kept in seen.json next to this script. The first run seeds state
silently (no notification flood); subsequent runs notify only on new product IDs.

Designed to be run on a schedule: launchd/cron on a Mac, or GitHub Actions.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "stores.json")
STATE_PATH = os.path.join(BASE, "seen.json")

UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json",
}


def http_get_json(url, timeout=20, retries=3):
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 503) and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))  # back off: 5s, 10s
                continue
            raise
    raise last_err


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def products_url(domain, limit):
    return f"https://{domain}/products.json?limit={limit}"


def fetch_products(domain, limit):
    data = http_get_json(products_url(domain, limit))
    return data.get("products", [])


# ---------------------------------------------------------------- verify ----

def cmd_verify(cfg):
    ok, failed = [], []
    for s in cfg["stores"]:
        domain = s["domain"]
        try:
            products = fetch_products(domain, 1)
            if isinstance(products, list):
                ok.append((s["name"], domain, len(products)))
                print(f"  OK    {s['name']:<24} https://{domain}/products.json")
            else:
                failed.append((s["name"], domain, "unexpected shape"))
                print(f"  ????  {s['name']:<24} responded but not a product list")
        except Exception as e:
            failed.append((s["name"], domain, str(e)))
            print(f"  FAIL  {s['name']:<24} {e}")
        time.sleep(0.5)

    print(f"\n{len(ok)} working, {len(failed)} failed.")
    if failed:
        print("Failed stores (check the domain via shop.app or the store site,")
        print("then correct 'domain' in stores.json):")
        for name, domain, err in failed:
            print(f"  - {name}: {domain}  ({err})")
    print("\nTip: set \"verified\": true in stores.json for every OK store so")
    print("'run' includes it, or pass --all to poll everything regardless.")


# ----------------------------------------------------- telegram commands ----

import re

URL_RE = re.compile(r"(?:https?://)?((?:[\w-]+\.)+[a-z]{2,})(?:/\S*)?", re.I)


def telegram_api(method, params):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}?" + urllib.parse.urlencode(params)
    try:
        return http_get_json(url)
    except Exception as e:
        print(f"[warn] telegram {method} failed: {e}", file=sys.stderr)
        return None


def derive_name(domain):
    core = domain.replace("www.", "").split(".")[0]
    return core.replace("-", " ").title()


def process_telegram_commands(cfg, state):
    """Read messages sent to the bot; add/remove stores accordingly.

    Send the bot a store URL (or bare domain) to add it.
    Send 'remove <domain>' to remove one.
    Only messages from TELEGRAM_CHAT_ID are honored.
    """
    my_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not my_chat:
        return False

    offset = state.get("_tg_offset", 0)
    resp = telegram_api("getUpdates", {"offset": offset + 1, "timeout": 0})
    if not resp or not resp.get("ok"):
        return False

    changed = False
    for upd in resp.get("result", []):
        state["_tg_offset"] = max(state.get("_tg_offset", 0), upd["update_id"])
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(my_chat):
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue

        existing = {s["domain"].replace("www.", ""): s for s in cfg["stores"]}

        if text.lower().startswith("remove"):
            m = URL_RE.search(text)
            target = m.group(1).replace("www.", "") if m else None
            if target and target in existing:
                cfg["stores"] = [s for s in cfg["stores"]
                                 if s["domain"].replace("www.", "") != target]
                changed = True
                send_telegram(f"➖ Removed {existing[target]['name']} ({target})")
            else:
                send_telegram(f"Couldn't find that store in the list: {text}")
            continue

        m = URL_RE.search(text)
        if not m:
            continue  # ordinary chatter, ignore
        domain = m.group(1)
        bare = domain.replace("www.", "")
        if bare in existing:
            send_telegram(f"Already tracking {existing[bare]['name']} ({bare})")
            continue

        # Validate: does it expose products.json? Try as given, then with www.
        working = None
        for candidate in (domain, f"www.{bare}"):
            try:
                fetch_products(candidate, 1)
                working = candidate
                break
            except Exception:
                continue

        if working:
            name = derive_name(working)
            cfg["stores"].append(
                {"name": name, "domain": working, "verified": True})
            changed = True
            send_telegram(f"➕ Added {name} ({working}). "
                          f"Seeding now; notifications start next run.")
        else:
            send_telegram(f"⚠️ {bare} didn't respond to /products.json — "
                          f"not a standard Shopify store, or a different domain. Not added.")

    return changed


# ------------------------------------------------------------------- run ----

def passes_filters(product, filters):
    title = (product.get("title") or "").lower()
    ptype = (product.get("product_type") or "").lower()

    inc = [k.lower() for k in filters.get("include_keywords", [])]
    if inc and not any(k in title for k in inc):
        return False

    exc = [k.lower() for k in filters.get("exclude_keywords", [])]
    if any(k in title for k in exc):
        return False

    inc_types = [t.lower() for t in filters.get("include_product_types", [])]
    if inc_types and ptype not in inc_types:
        return False

    if filters.get("notify_only_available", False):
        variants = product.get("variants", [])
        if variants and not any(v.get("available", True) for v in variants):
            return False

    return True


def format_message(store_name, domain, product):
    title = product.get("title", "Untitled")
    handle = product.get("handle", "")
    url = f"https://{domain}/products/{handle}"
    prices = sorted({v.get("price") for v in product.get("variants", []) if v.get("price")})
    price = prices[0] if prices else "?"
    vendor = product.get("vendor", "")
    lines = [f"🆕 {store_name}", title]
    if vendor and vendor.lower() not in title.lower():
        lines.append(vendor)
    lines.append(f"${price}" if not str(price).startswith("$") else str(price))
    lines.append(url)
    return "\n".join(lines)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "false",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def cmd_run(cfg, dry_run=False, poll_all=False):
    state = load_json(STATE_PATH, {})

    if not dry_run:
        try:
            cfg_changed = process_telegram_commands(cfg, state)
            if cfg_changed:
                save_json(CONFIG_PATH, cfg)
        except Exception as e:
            print(f"[warn] telegram command processing failed: {e}", file=sys.stderr)

    filters = cfg.get("filters", {})
    limit = cfg.get("poll", {}).get("products_per_store", 20)
    first_run_stores = 0
    notified = 0

    for s in cfg["stores"]:
        if not poll_all and not s.get("verified", False):
            continue
        name, domain = s["name"], s["domain"]
        try:
            products = fetch_products(domain, limit)
        except Exception as e:
            print(f"[warn] {name}: fetch failed: {e}", file=sys.stderr)
            continue

        seen = set(state.get(domain, []))
        current_ids = [str(p["id"]) for p in products if "id" in p]

        if not seen:
            # First time seeing this store: seed silently.
            state[domain] = current_ids
            first_run_stores += 1
            continue

        new_products = [p for p in products if str(p.get("id")) not in seen]
        for p in new_products:
            if not passes_filters(p, filters):
                continue
            msg = format_message(name, domain, p)
            if dry_run:
                print("---\n" + msg)
            else:
                try:
                    send_telegram(msg)
                    notified += 1
                    time.sleep(1)  # be gentle with Telegram rate limits
                except Exception as e:
                    print(f"[warn] Telegram send failed: {e}", file=sys.stderr)

        # Keep a rolling window of known IDs so state doesn't grow forever.
        state[domain] = list(dict.fromkeys(current_ids + list(seen)))[:500]
        time.sleep(1.5)  # be gentle with the stores too

    save_json(STATE_PATH, state)
    print(f"Done. Seeded {first_run_stores} store(s), sent {notified} notification(s).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["verify", "run"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="poll every store, including unverified ones")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit(f"Missing or invalid config: {CONFIG_PATH}")

    if args.command == "verify":
        cmd_verify(cfg)
    else:
        cmd_run(cfg, dry_run=args.dry_run, poll_all=args.all)


if __name__ == "__main__":
    main()
