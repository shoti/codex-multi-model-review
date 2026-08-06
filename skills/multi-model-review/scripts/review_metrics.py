"""Pure metric primitives for the multi-model review runner."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterable


TOKEN_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)
ARTIFACT_BYTE_FIELDS = (
    "prompt_bytes",
    "manifest_bytes",
    "patch_bytes",
    "reviewer_report_bytes",
    "raw_response_bytes",
)


def empty_token_usage() -> dict[str, int]:
    return {
        **{field: 0 for field in TOKEN_FIELDS},
        "total_input_tokens": 0,
        "total_tokens": 0,
    }


def numeric_token(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(float(value)) or value < 0:
        return 0
    return int(value)


def tokens_from_mapping(value: dict[str, Any]) -> dict[str, int]:
    aliases = {
        "input_tokens": ("input_tokens", "inputTokens"),
        "cache_creation_input_tokens": (
            "cache_creation_input_tokens",
            "cacheCreationInputTokens",
        ),
        "cache_read_input_tokens": (
            "cache_read_input_tokens",
            "cacheReadInputTokens",
            "cache_read_tokens",
        ),
        "output_tokens": ("output_tokens", "outputTokens"),
    }
    result = empty_token_usage()
    for field, keys in aliases.items():
        for key in keys:
            if key in value:
                result[field] = numeric_token(value.get(key))
                break
    result["total_input_tokens"] = sum(
        result[field]
        for field in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    )
    result["total_tokens"] = (
        result["total_input_tokens"] + result["output_tokens"]
    )
    return result


def add_token_usage(target: dict[str, int], value: dict[str, int]) -> None:
    for field in (*TOKEN_FIELDS, "total_input_tokens", "total_tokens"):
        target[field] = int(target.get(field) or 0) + int(value.get(field) or 0)


def normalized_usage_tokens(usage: dict[str, Any]) -> dict[str, int]:
    """Normalize provider token counters without double-counting nested totals."""
    model_usage = usage.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        total = empty_token_usage()
        for value in model_usage.values():
            if isinstance(value, dict):
                add_token_usage(total, tokens_from_mapping(value))
        return total
    nested = usage.get("usage")
    if isinstance(nested, dict):
        normalized = tokens_from_mapping(nested)
        if normalized["total_tokens"]:
            return normalized
    return tokens_from_mapping(usage)


def empty_artifact_bytes() -> dict[str, int]:
    return {field: 0 for field in ARTIFACT_BYTE_FIELDS}


def path_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def run_artifact_bytes(run_dir: Path) -> dict[str, int]:
    result = empty_artifact_bytes()
    result["prompt_bytes"] = path_size(run_dir / "prompt.md")
    result["manifest_bytes"] = path_size(run_dir / "manifest.md")
    result["patch_bytes"] = path_size(run_dir / "change.patch")
    result["reviewer_report_bytes"] = sum(
        path_size(path)
        for path in run_dir.glob("*.md")
        if path.name not in {"prompt.md", "manifest.md"}
    )
    result["raw_response_bytes"] = sum(
        path_size(path) for path in run_dir.glob("*.raw.json")
    )
    return result


def add_artifact_bytes(target: dict[str, int], value: dict[str, int]) -> None:
    for field in ARTIFACT_BYTE_FIELDS:
        target[field] = int(target.get(field) or 0) + int(value.get(field) or 0)


def percentile(sorted_values: list[float], probability: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] + (
        sorted_values[upper] - sorted_values[lower]
    ) * fraction


def numeric_distribution(values: Iterable[float | int]) -> dict[str, float | int]:
    numbers = sorted(
        float(value)
        for value in values
        if not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )
    if not numbers:
        return {"count": 0}
    return {
        "count": len(numbers),
        "min": round(numbers[0], 6),
        "p50": round(percentile(numbers, 0.5), 6),
        "p90": round(percentile(numbers, 0.9), 6),
        "max": round(numbers[-1], 6),
        "mean": round(sum(numbers) / len(numbers), 6),
    }
