"""Read private clothing context into memory without logging or persistence.

An explicitly supplied environment value wins, even if empty or invalid.
The local path is consulted only when the environment key is absent.
"""
import json
import math
import os


FIT_NUMBERS = frozenset(("min_front_rise", "min_thigh", "min_knee",
                        "preferred_max_hem", "min_acceptable_front_rise",
                        "min_acceptable_thigh"))
CATEGORIES = frozenset(("pants", "jacket", "clothing"))


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _labels(value):
    if not isinstance(value, list) or any(
            type(v) not in (str, int) or not str(v).strip() for v in value):
        raise ValueError
    return [str(v).strip() for v in value]


def _references(value):
    if not isinstance(value, dict) or any(k not in CATEGORIES for k in value):
        raise ValueError
    return {k: _labels(v) for k, v in value.items()}


def validate_profile(value):
    """Accept only supported, well-typed context; unusable input becomes absent."""
    if not isinstance(value, dict):
        return {}
    try:
        result = {}
        if "fit" in value:
            fit = value["fit"]
            if not isinstance(fit, dict) or any(
                    k not in FIT_NUMBERS | {"waist_range"} for k in fit):
                return {}
            if any(not _number(v) for k, v in fit.items() if k in FIT_NUMBERS):
                return {}
            if "waist_range" in fit:
                bounds = fit["waist_range"]
                if (not isinstance(bounds, list) or len(bounds) != 2 or
                        not all(_number(v) for v in bounds) or bounds[0] > bounds[1]):
                    return {}
            if fit:
                result["fit"] = dict(fit)
        if "size_references" in value:
            references = _references(value["size_references"])
            if any(references.values()):
                result["size_references"] = references
        if "brand_size_references" in value:
            brands = value["brand_size_references"]
            if not isinstance(brands, dict) or any(not isinstance(k, str) or not k.strip() for k in brands):
                return {}
            references = {k: _references(v) for k, v in brands.items()}
            if any(any(categories.values()) for categories in references.values()):
                result["brand_size_references"] = references
        if "owned_models" in value:
            models = value["owned_models"]
            if not isinstance(models, list):
                return {}
            owned = []
            for model in models:
                if isinstance(model, str) and model.strip():
                    owned.append(model.strip())
                elif (isinstance(model, dict) and set(model) == {"brand", "tokens"}
                      and isinstance(model["brand"], str) and model["brand"].strip()
                      and isinstance(model["tokens"], list) and model["tokens"]
                      and all(isinstance(v, str) and v.strip() for v in model["tokens"])):
                    owned.append({"brand": model["brand"].strip(),
                                  "tokens": [v.strip() for v in model["tokens"]]})
                else:
                    return {}
            result["owned_models"] = owned
        return result if any(result.values()) else {}
    except (TypeError, ValueError, OverflowError):
        return {}


def load_profile_with_status(environ=None):
    """Return selected context and one fixed, non-revealing startup status."""
    env = os.environ if environ is None else environ
    try:
        if "CLOTHING_FIT_PROFILE_JSON" in env:
            raw = env["CLOTHING_FIT_PROFILE_JSON"]
        elif env.get("CLOTHING_CONTEXT_PATH"):
            with open(env["CLOTHING_CONTEXT_PATH"], encoding="utf-8") as stream:
                raw = stream.read()
        else:
            return {}, "absent"
        if isinstance(raw, str) and not raw.strip():
            return {}, "absent"
        profile = validate_profile(json.loads(raw))
        return (profile, "loaded") if profile else ({}, "invalid, using fallback")
    except FileNotFoundError:
        return {}, "absent"
    except (OSError, ValueError, TypeError, RecursionError):
        # Never include raw JSON, paths, exceptions or profile values in logs.
        return {}, "invalid, using fallback"


def load_profile(environ=None):
    """Silent context-only API for offline callers; production reports once."""
    return load_profile_with_status(environ)[0]
