"""Provider-neutral review report contracts and renderers."""

from __future__ import annotations

import re
from typing import Any


CLAUDE_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "findings", "test_gaps", "notes"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["PASS_CLEAN", "PASS_WITH_FINDINGS", "BLOCK"],
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "severity",
                    "title",
                    "location",
                    "trigger",
                    "evidence",
                    "impact",
                    "smallest_fix",
                    "confidence",
                ],
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["blocker", "high", "medium", "low"],
                    },
                    "title": {"type": "string"},
                    "location": {"type": ["string", "null"]},
                    "trigger": {"type": "string"},
                    "evidence": {"type": "string"},
                    "impact": {"type": "string"},
                    "smallest_fix": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                },
            },
        },
        "test_gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "title", "needed_test", "risk"],
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["medium", "low"],
                    },
                    "title": {"type": "string"},
                    "needed_test": {"type": "string"},
                    "risk": {"type": "string"},
                },
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
}


def render_structured_review(payload: dict[str, Any]) -> str:
    """Render a schema-validated provider result into the audit Markdown format."""
    lines = ["# Verdict", str(payload["verdict"]), "", "# Findings"]
    findings = payload.get("findings") or []
    if not findings:
        lines.append("None.")
    for item in findings:
        lines.extend(
            [
                f"## [{item['severity']}] {item['title']}",
                f"- Location: {item.get('location') or 'Not specified'}",
                f"- Trigger: {item['trigger']}",
                f"- Evidence: {item['evidence']}",
                f"- Impact: {item['impact']}",
                f"- Smallest fix: {item['smallest_fix']}",
                f"- Confidence: {item['confidence']}",
                "",
            ]
        )
    lines.extend(["# Test gaps"])
    gaps = payload.get("test_gaps") or []
    if not gaps:
        lines.append("None.")
    for item in gaps:
        lines.extend(
            [
                f"## [{item['severity']}] {item['title']}",
                f"- Needed test: {item['needed_test']}",
                f"- Risk: {item['risk']}",
                "",
            ]
        )
    lines.extend(["# Notes"])
    notes = payload.get("notes") or []
    if not notes:
        lines.append("None.")
    else:
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines).rstrip() + "\n"


SEVERITIES = ("blocker", "high", "medium", "low")


def markdown_section(report: str, heading: str) -> str:
    match = re.search(rf"(?im)^#\s+{re.escape(heading)}\s*$", report)
    if not match:
        return ""
    start = match.end()
    next_heading = re.search(r"(?m)^#\s+", report[start:])
    end = start + next_heading.start() if next_heading else len(report)
    return report[start:end].strip()


def parse_severity_items(
    reviewer: str,
    section: str,
    *,
    identifier_kind: str,
) -> list[dict[str, Any]]:
    heading_pattern = re.compile(
        r"(?im)^##\s+\[(blocker|high|medium|low)\]\s+(.+?)\s*$"
    )
    matches = list(heading_pattern.finditer(section))
    items: list[dict[str, Any]] = []
    for offset, match in enumerate(matches):
        end = (
            matches[offset + 1].start()
            if offset + 1 < len(matches)
            else len(section)
        )
        body = section[match.end() : end].strip()
        location_match = re.search(r"(?im)^-\s*Location:\s*(.+?)\s*$", body)
        suffix = offset + 1
        identifier = (
            f"{reviewer}-{suffix:03d}"
            if identifier_kind == "finding"
            else f"{reviewer}-test-{suffix:03d}"
        )
        items.append(
            {
                "id": identifier,
                "kind": identifier_kind,
                "reviewer": reviewer,
                "severity": match.group(1).lower(),
                "title": match.group(2).strip(),
                "location": (
                    location_match.group(1).strip() if location_match else None
                ),
                "report_excerpt": body[:2_000],
            }
        )
    return items


def parse_bullet_test_gaps(
    reviewer: str, section: str, *, risk_profiled: bool
) -> list[dict[str, Any]]:
    if not section or section.strip().lower() in {"none", "none."}:
        return []
    bullets: list[str] = []
    current: list[str] = []
    for line in section.splitlines():
        if line.startswith("- "):
            if current:
                bullets.append(" ".join(current).strip())
            current = [line[2:].strip()]
        elif current and line.strip():
            current.append(line.strip())
    if current:
        bullets.append(" ".join(current).strip())
    return [
        {
            "id": f"{reviewer}-test-{index:03d}",
            "kind": "test_gap",
            "reviewer": reviewer,
            "severity": "medium" if risk_profiled else "low",
            "title": bullet,
            "location": None,
            "report_excerpt": bullet[:2_000],
        }
        for index, bullet in enumerate(bullets, start=1)
        if bullet and bullet.lower() not in {"none", "none."}
    ]


def parse_review_report(
    reviewer: str, report: str, *, risk_profiled: bool = False
) -> dict[str, Any]:
    verdict_match = re.search(
        r"(?im)^#\s+Verdict\s*\n+\s*"
        r"(PASS_CLEAN|PASS_WITH_FINDINGS|PASS|BLOCK)\b",
        report,
    )
    verdict = verdict_match.group(1).upper() if verdict_match else "UNKNOWN"
    if verdict == "PASS":
        verdict = "PASS_CLEAN"
    findings = parse_severity_items(
        reviewer,
        markdown_section(report, "Findings"),
        identifier_kind="finding",
    )
    test_gap_section = markdown_section(report, "Test gaps")
    test_gaps = parse_severity_items(
        reviewer, test_gap_section, identifier_kind="test_gap"
    )
    invalid_test_gap_severities = [
        item["id"]
        for item in test_gaps
        if item["severity"] in {"blocker", "high"}
    ]
    if not risk_profiled:
        for item in test_gaps:
            if item["severity"] == "medium":
                item["reported_severity"] = "medium"
                item["severity"] = "low"
                item["severity_adjustment"] = (
                    "Downgraded because no changed risk profile was selected."
                )
    if not test_gaps:
        test_gaps = parse_bullet_test_gaps(
            reviewer, test_gap_section, risk_profiled=risk_profiled
        )
    finding_counts = {
        severity: sum(item["severity"] == severity for item in findings)
        for severity in SEVERITIES
    }
    test_gap_counts = {
        severity: sum(item["severity"] == severity for item in test_gaps)
        for severity in SEVERITIES
    }
    declared_verdict = verdict
    normalizations: list[str] = []
    if verdict == "PASS_CLEAN" and (findings or test_gaps):
        verdict = "PASS_WITH_FINDINGS"
        normalizations.append(
            "Downgraded PASS_CLEAN because structured findings or test gaps exist."
        )
    return {
        "verdict": verdict,
        "declared_verdict": declared_verdict,
        "normalizations": normalizations,
        "finding_counts": finding_counts,
        "findings": findings,
        "test_gap_counts": test_gap_counts,
        "test_gaps": test_gaps,
        "invalid_test_gap_severities": invalid_test_gap_severities,
    }


def parsed_report_is_invalid(parsed: dict[str, Any]) -> bool:
    if parsed["verdict"] == "UNKNOWN" or parsed["invalid_test_gap_severities"]:
        return True
    if parsed["verdict"] == "PASS_WITH_FINDINGS" and not (
        parsed["findings"] or parsed["test_gaps"]
    ):
        return True
    if parsed["verdict"] == "BLOCK" and not any(
        finding["severity"] in {"blocker", "high"}
        for finding in parsed["findings"]
    ):
        return True
    return False
