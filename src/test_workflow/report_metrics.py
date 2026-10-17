"""Aggregate recorded generation/review telemetry, preserving incomplete totals."""

TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _sum(values):
    return None if any(value is None for value in values) else sum(values)


def model_metrics(nodes, identity, field, *, measured=True):
    calls = [node for node in nodes if node["node"] == identity]
    telemetry = [node[field] if node["protocol_matches"] else None for node in calls]
    if not measured:
        telemetry = []
    counts = {}
    for name in ("client_calls", "request_attempts", "external_requests"):
        values = [item.get(name) if item is not None else None for item in telemetry]
        counts[name] = _sum(values)
        counts[f"known_{name}"] = sum(value for value in values if value is not None)
    usage, known_usage = {}, {}
    for name in TOKEN_FIELDS:
        values = [(item.get("total_usage") or {}).get(name) if item is not None and item["client_calls"] else
                  0 if item is not None else None for item in telemetry]
        usage[name] = _sum(values)
        known_usage[name] = sum(value for value in values if value is not None)
    return {**counts, "token_usage": usage, "known_token_usage": known_usage,
            "telemetry_complete": all(item is not None for item in telemetry)
                                  and all(counts[name] is not None for name in ("client_calls", "request_attempts", "external_requests"))
                                  and all(value is not None for value in usage.values()),
            "client_modes": sorted({item["client_mode"] for item in telemetry if item is not None})}


def model_usage(nodes, *, measured=True):
    generation = model_metrics(nodes, "test_generation", "generation", measured=measured)
    review = model_metrics(nodes, "test_quality_review", "review", measured=measured)
    total = {}
    for name in ("client_calls", "request_attempts", "external_requests"):
        total[name] = _sum([generation[name], review[name]])
        total[f"known_{name}"] = generation[f"known_{name}"] + review[f"known_{name}"]
    total["token_usage"] = {name: _sum([generation["token_usage"][name], review["token_usage"][name]]) for name in TOKEN_FIELDS}
    total["known_token_usage"] = {name: generation["known_token_usage"][name] + review["known_token_usage"][name] for name in TOKEN_FIELDS}
    total["telemetry_complete"] = generation["telemetry_complete"] and review["telemetry_complete"]
    return {"generation": generation, "review": review, "total": total}
