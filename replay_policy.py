"""Read-only replay of captured catalogues; no fetching, alerts or state writes.

Input is a JSON list of {store, products, captured_at}. Optional --baseline
points at the unchanged tracker.py and --state at an existing seen.json copy.
Catalogue eligibility is conditional on a meaningful event. Existing unchanged
listings remain deduped; this tool does not call cmd_run (--dry-run writes state).
"""
import argparse
import importlib.util
import json
import pathlib

import clothing_policy
import tracker


def replay(records, cfg, state=None, baseline=None, context=None):
    results = []
    for record in records:
        store = record["store"]
        products = record.get("products", [])
        events = None
        if state is not None:
            events, _, _ = tracker.diff_store(
                products, state.get(store["domain"], []),
                state.get("_stock", {}).get(store["domain"]))
            prices, _ = tracker.additional_price_events(
                products, events, state.get("_clothing_prices", {}).get(store["domain"]), cfg, store, context=context)
            events += prices
        event_by_id = {str(p["id"]): (kind, extra) for kind, p, extra in events or []}
        for product in products:
            kind, extra = event_by_id.get(str(product["id"]), ("new", None))
            decision = tracker.alert_decision(product, store, cfg, kind, extra, context)
            # A catalogue item that qualifies on a hypothetical new listing is
            # not a real new event. Keep the distinction explicit in output.
            result = {"store": store["name"], "domain": store["domain"],
                      "id": str(product["id"]), "title": product.get("title"),
                      "url": product.get("_url") or f"https://{store['domain']}/products/{product.get('handle', '')}",
                      "captured_at": record.get("captured_at"), "decision": decision,
                      "event": event_by_id.get(str(product["id"])),
                      "would_alert_now": bool(decision["keep"] and str(product["id"]) in event_by_id)
                      if state is not None else None}
            if baseline:
                result["baseline_kept"] = baseline.passes_filters(
                    product, baseline.resolve_filters(store, cfg.get("filters", {})))
            results.append(result)
    hats = [r for r in results if r["decision"]["category"] == "hat"]
    clothing = [r for r in results if r["decision"]["applies"]]
    non_new_era = [r for r in hats if r["decision"]["brand"] != "New Era"]
    changed = [r for r in hats if "baseline_kept" in r and r["baseline_kept"] != r["decision"]["keep"]]
    era_changed = [r for r in changed if r["decision"]["brand"] == "New Era"]
    team_apparel = [r for r in results if r["decision"].get("legacy_team_apparel")]
    team_comparison = {}
    for store in cfg.get("stores", []):
        if not store.get("filters", {}).get("include_keywords"):
            continue
        garments = [r for r in results if r["domain"] == store["domain"] and
                    r["decision"]["category"] in ("pants", "jacket", "clothing")]
        team_comparison[store["name"]] = {
            "garments": len(garments), "team_matches": sum(r["decision"].get("legacy_team_apparel", False) for r in garments),
            "baseline_kept": sum(r.get("baseline_kept", False) for r in garments),
            "candidate_kept": sum(r["decision"]["keep"] for r in garments),
            "changed": [r for r in garments if "baseline_kept" in r and r["baseline_kept"] != r["decision"]["keep"]]}
    return {"team_apparel_comparison": team_comparison, "summary": {"records": len(results), "clothing": len(clothing),
                        "legacy_team_apparel": len(team_apparel),
                        "clothing_eligible_on_new_listing": sum(r["decision"]["keep"] for r in clothing),
                        "clothing_suppressed": sum(not r["decision"]["keep"] for r in clothing),
                        "hats": len(hats), "non_new_era_hats": len(non_new_era),
                        "hat_decisions_changed": len(changed),
                        "non_new_era_decisions_changed": sum(r["decision"]["brand"] != "New Era" for r in changed),
                        "new_era_decisions_changed": len(era_changed),
                        "new_era_newly_suppressed": sum(r["baseline_kept"] and not r["decision"]["keep"] for r in era_changed),
                        "new_era_newly_kept": sum(not r["baseline_kept"] and r["decision"]["keep"] for r in era_changed),
                        "existing_state_events": sum(r["event"] is not None for r in results),
                        "would_alert_now": sum(r["would_alert_now"] is True for r in results)},
            "fetch_errors": [{"store": r["store"]["name"], "error": r["error"]} for r in records if "error" in r],
            "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalogues", type=pathlib.Path)
    parser.add_argument("--config", type=pathlib.Path, default=pathlib.Path(tracker.CONFIG_PATH))
    parser.add_argument("--state", type=pathlib.Path)
    parser.add_argument("--baseline", type=pathlib.Path)
    args = parser.parse_args()
    baseline = None
    if args.baseline:
        spec = importlib.util.spec_from_file_location("baseline_tracker", args.baseline)
        baseline = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(baseline)
    records = json.loads(args.catalogues.read_text())
    cfg = json.loads(args.config.read_text())
    state = json.loads(args.state.read_text()) if args.state else None
    print(json.dumps(replay(records, cfg, state, baseline), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
