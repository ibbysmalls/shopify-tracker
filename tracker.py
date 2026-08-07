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
            # Cloudflare bot protection. Retrying cannot help: the challenge
            # needs a JS engine and issues a cookie bound to IP + user agent,
            # so a datacenter runner will never pass it. Fail loudly instead.
            body = ""
            try:
                body = e.read(2000).decode("utf-8", errors="replace").lower()
            except Exception:
                pass
            if e.code in (403, 503) and "cloudflare" in body:
                raise RuntimeError(f"BLOCKED (Cloudflare {e.code})")
            if e.code in (429, 503) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))  # back off: 2s, 4s
                continue
            raise
    raise last_err


def try_json(url, timeout=10):
    """Non-raising probe. Returns parsed JSON or None."""
    try:
        return http_get_json(url, timeout=timeout, retries=1)
    except Exception:
        return None


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


def woo_products_url(domain, limit):
    return (f"https://{domain}/wp-json/wc/store/v1/products"
            f"?per_page={limit}&orderby=date&order=desc")


def woo_price(prices):
    """Woo returns prices as integer strings in minor units, with the exponent
    in currency_minor_unit: "26900" + minor_unit 2 -> "269.00"."""
    if not isinstance(prices, dict):
        return None
    raw = prices.get("price")
    if raw in (None, ""):
        return None
    try:
        minor = int(prices.get("currency_minor_unit", 2))
        return f"{int(raw) / (10 ** minor):.{minor}f}"
    except (TypeError, ValueError):
        return str(raw)


def normalize_woo(products, domain):
    """Reshape WooCommerce Store API products into the Shopify shape the rest
    of this script already understands. Adds _url, since Woo uses
    /product/slug/ while Shopify uses /products/slug."""
    out = []
    for p in products:
        price = woo_price(p.get("prices"))
        in_stock = bool(p.get("is_in_stock"))
        # Store API exposes variation ids and attribute values but not
        # per-variation stock, so availability falls back to product level.
        variants = []
        for v in p.get("variations") or []:
            label = " / ".join(str(a.get("value"))
                               for a in (v.get("attributes") or [])
                               if a.get("value"))
            variants.append({"title": label or str(v.get("id")),
                             "available": in_stock,
                             "price": price})
        if not variants:
            variants = [{"title": "Default", "available": in_stock, "price": price}]

        cats = p.get("categories") or []
        out.append({
            "id": p.get("id"),
            "title": p.get("name"),
            "handle": p.get("slug"),
            "vendor": "",
            "product_type": cats[0].get("name", "") if cats else "",
            "variants": variants,
            "_url": p.get("permalink") or f"https://{domain}/product/{p.get('slug')}/",
        })
    return out


def fetch_products(domain, limit, platform="shopify"):
    """Return a list of products in Shopify shape, whatever the platform."""
    if (platform or "shopify").lower() == "woocommerce":
        data = http_get_json(woo_products_url(domain, limit))
        if not isinstance(data, list):
            raise RuntimeError("unexpected shape from Woo Store API")
        return normalize_woo(data, domain)
    data = http_get_json(products_url(domain, limit))
    return data.get("products", [])


def detect_platform(domain):
    """Probe a domain. Returns 'shopify', 'woocommerce', or None.
    At most two requests, one product each."""
    data = try_json(products_url(domain, 1))
    if isinstance(data, dict) and "products" in data:
        return "shopify"
    data = try_json(woo_products_url(domain, 1))
    if isinstance(data, list):
        return "woocommerce"
    return None


# ---------------------------------------------------------------- verify ----

def cmd_verify(cfg):
    ok, failed = [], []
    for s in cfg["stores"]:
        domain = s["domain"]
        platform = detect_platform(domain)
        if platform is None:
            s["verified"] = False
            failed.append((s["name"], domain, "no shopify or woocommerce endpoint"))
            print(f"  FAIL  {s['name']:<24} neither products.json nor wc/store responded")
            time.sleep(0.5)
            continue
        try:
            products = fetch_products(domain, 1, platform)
            if isinstance(products, list):
                s["platform"] = platform
                s["verified"] = True
                ok.append((s["name"], domain, platform))
                print(f"  OK    {s['name']:<24} {platform}")
            else:
                s["verified"] = False
                failed.append((s["name"], domain, "unexpected shape"))
                print(f"  ????  {s['name']:<24} responded but not a product list")
        except Exception as e:
            s["verified"] = False
            failed.append((s["name"], domain, str(e)))
            print(f"  FAIL  {s['name']:<24} {e}")
        time.sleep(0.5)

    save_json(CONFIG_PATH, cfg)
    shopify = sum(1 for _, _, p in ok if p == "shopify")
    woo = sum(1 for _, _, p in ok if p == "woocommerce")
    print(f"\n{len(ok)} working ({shopify} shopify, {woo} woocommerce), "
          f"{len(failed)} failed.")
    if failed:
        print("Failed stores (check the domain via shop.app or the store site,")
        print("then correct 'domain' in stores.json):")
        for name, domain, err in failed:
            print(f"  - {name}: {domain}  ({err})")
    print("\nstores.json has been updated with the detected platform and")
    print("verified flags. Commit it to keep the results.")


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

        # Validate: what platform is it? Try as given, then with www.
        working = None
        platform = None
        for candidate in (domain, f"www.{bare}"):
            platform = detect_platform(candidate)
            if platform:
                working = candidate
                break

        if working:
            name = derive_name(working)
            cfg["stores"].append(
                {"name": name, "domain": working,
                 "platform": platform, "verified": True})
            changed = True
            send_telegram(f"➕ Added {name} ({working}, {platform}). "
                          f"Seeding now; notifications start next run.")
        else:
            send_telegram(f"⚠️ {bare} exposed neither a Shopify products.json "
                          f"nor a WooCommerce Store API. Not added.")

    return changed


def report_health(state, ok_names, failed, skipped, dry_run, threshold=3):
    """Alert on Telegram when a store's health *changes*, not every run.

    A store that fails transiently (GitHub blip, store hiccup) is ignored until
    it has failed `threshold` consecutive runs. A store deactivated by verify
    (verified:false) alerts immediately, since that is a config state rather
    than a blip. Recoveries are announced so the alert always closes.
    """
    health = state.setdefault("_health", {})
    fails = health.setdefault("fails", {})
    alerted = set(health.setdefault("alerted", []))
    messages = []

    # Anything that polled cleanly resets, and closes any open alert.
    for name in ok_names:
        fails.pop(name, None)
        if name in alerted:
            alerted.discard(name)
            messages.append(f"✅ {name} is back. Polling normally again.")

    # Repeated failures cross the threshold and open an alert.
    for name in failed:
        fails[name] = fails.get(name, 0) + 1
        if fails[name] >= threshold and name not in alerted:
            alerted.add(name)
            messages.append(
                f"🔴 {name} has failed {fails[name]} runs in a row. "
                f"No drops from this store are being caught.")

    # Deactivated stores are silent holes, so flag them straight away.
    for name in skipped:
        if name not in alerted:
            alerted.add(name)
            messages.append(
                f"⚠️ {name} is set verified:false and is not being polled. "
                f"Re-run verify, or fix its domain in stores.json.")

    health["alerted"] = sorted(alerted)
    # Keep the counter dict from growing with stores that no longer exist.
    live = set(ok_names) | set(failed) | set(skipped)
    health["fails"] = {k: v for k, v in fails.items() if k in live}

    for msg in messages:
        if dry_run:
            print("---\n" + msg)
        else:
            try:
                send_telegram(msg)
                time.sleep(1)
            except Exception as e:
                print(f"[warn] health alert send failed: {e}", file=sys.stderr)

    return messages


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
    # Woo products carry _url because their permalink shape differs.
    url = product.get("_url") or f"https://{domain}/products/{handle}"
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
    timings = []   # (seconds, name, status)
    run_start = time.time()

    total_configured = len(cfg["stores"])
    skipped_unverified = []
    polled_ok = 0
    ok_names = []
    failed = []
    empty = []

    for s in cfg["stores"]:
        name, domain = s["name"], s["domain"]
        if not poll_all and not s.get("verified", False):
            skipped_unverified.append(name)
            continue
        t0 = time.time()
        try:
            products = fetch_products(domain, limit, s.get("platform", "shopify"))
            timings.append((time.time() - t0, name, "ok"))
            polled_ok += 1
            ok_names.append(name)
            if not products:
                empty.append(name)
        except Exception as e:
            timings.append((time.time() - t0, name, f"FAILED: {e}"))
            failed.append(name)
            print(f"[warn] {name}: fetch failed after {time.time()-t0:.1f}s: {e}",
                  file=sys.stderr)
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

    threshold = cfg.get("health", {}).get("fail_alerts_after", 3)
    report_health(state, ok_names, failed, skipped_unverified, dry_run, threshold)

    save_json(STATE_PATH, state)
    total = time.time() - run_start
    print(f"Done in {total:.1f}s. Seeded {first_run_stores} store(s), "
          f"sent {notified} notification(s).")

    # Census heartbeat: proof every store was actually reached.
    print(f"Census: {polled_ok}/{total_configured} polled ok, "
          f"{len(failed)} failed, {len(empty)} returned empty, "
          f"{len(skipped_unverified)} skipped (unverified).")
    if failed:
        print(f"  FAILED: {', '.join(failed)}")
    if empty:
        print(f"  EMPTY (responded but no products): {', '.join(empty)}")
    if skipped_unverified:
        print(f"  SKIPPED (verified:false): {', '.join(skipped_unverified)}")

    # Timing report: anything slow or failed, slowest first.
    problems = sorted(
        [t for t in timings if t[0] >= 2.0 or t[2] != "ok"],
        key=lambda t: t[0], reverse=True)
    if problems:
        print("\nSlow or failing stores (>=2s or errored), slowest first:")
        for secs, name, status in problems:
            tag = "" if status == "ok" else f"  [{status}]"
            print(f"  {secs:6.1f}s  {name}{tag}")
    else:
        print("All stores responded in under 2s.")


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
