"""Claim-to-evidence contracts and conservative assurance evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Sequence


ASSURANCE_SCHEMA_VERSION = 1
CLAIM_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
CLAIM_STATUSES = {"verified", "deferred", "unverified"}
EVIDENCE_KINDS = {"repository", "test", "artifact", "runtime"}


class AssuranceError(ValueError):
    """A malformed or incomplete assurance contract."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_claim(value: str, *, critical: bool) -> dict[str, Any]:
    identifier, separator, text = value.partition("=")
    identifier = identifier.strip()
    text = text.strip()
    if not separator or not identifier or not text:
        raise AssuranceError(
            "Claims must use ID=TEXT with a non-empty stable ID and exact text."
        )
    if not CLAIM_ID_PATTERN.fullmatch(identifier):
        raise AssuranceError(
            f"Invalid claim ID {identifier!r}; use a letter followed by at most "
            "63 letters, digits, dots, underscores, or hyphens."
        )
    return {
        "id": identifier,
        "text": text,
        "kind": "critical_invariant" if critical else "acceptance_criterion",
        "critical": critical,
    }


def build_contract(
    criteria: Sequence[str], critical_invariants: Sequence[str]
) -> dict[str, Any]:
    claims = [
        *(parse_claim(value, critical=False) for value in criteria),
        *(parse_claim(value, critical=True) for value in critical_invariants),
    ]
    identifiers = [str(claim["id"]) for claim in claims]
    duplicates = sorted(
        identifier
        for identifier in set(identifiers)
        if identifiers.count(identifier) > 1
    )
    if duplicates:
        raise AssuranceError("Duplicate claim IDs: " + ", ".join(duplicates))
    claims.sort(key=lambda claim: str(claim["id"]))
    contract: dict[str, Any] = {
        "schema_version": ASSURANCE_SCHEMA_VERSION,
        "claims": claims,
    }
    contract["sha256"] = sha256_value(contract)
    return contract


def validate_contract(contract: Any) -> list[dict[str, Any]]:
    if not isinstance(contract, dict):
        raise AssuranceError("Assurance contract must be a JSON object.")
    if contract.get("schema_version") != ASSURANCE_SCHEMA_VERSION:
        raise AssuranceError("Unsupported assurance contract schema version.")
    claims = contract.get("claims")
    if not isinstance(claims, list):
        raise AssuranceError("Assurance contract claims must be a list.")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in claims:
        if not isinstance(raw, dict):
            raise AssuranceError("Each assurance claim must be an object.")
        identifier = raw.get("id")
        text = raw.get("text")
        kind = raw.get("kind")
        critical = raw.get("critical")
        if not isinstance(identifier, str) or not CLAIM_ID_PATTERN.fullmatch(identifier):
            raise AssuranceError("Assurance claim has an invalid stable ID.")
        if identifier in seen:
            raise AssuranceError(f"Duplicate assurance claim ID: {identifier}")
        if not isinstance(text, str) or not text.strip():
            raise AssuranceError(f"Claim {identifier} has no exact text.")
        if kind not in {"acceptance_criterion", "critical_invariant"}:
            raise AssuranceError(f"Claim {identifier} has an invalid kind.")
        if not isinstance(critical, bool) or critical != (
            kind == "critical_invariant"
        ):
            raise AssuranceError(f"Claim {identifier} has contradictory criticality.")
        seen.add(identifier)
        normalized.append(
            {"id": identifier, "text": text.strip(), "kind": kind, "critical": critical}
        )
    normalized.sort(key=lambda claim: str(claim["id"]))
    unsigned = {"schema_version": ASSURANCE_SCHEMA_VERSION, "claims": normalized}
    if contract.get("sha256") != sha256_value(unsigned):
        raise AssuranceError("Assurance contract fingerprint is invalid.")
    return normalized


def new_document(
    *,
    run_id: str,
    workflow_id: str,
    repository_id: str,
    source_fingerprint: str,
    contract: dict[str, Any],
    reviewer_coverage: dict[str, Any],
    created_at: str,
) -> dict[str, Any]:
    claims = validate_contract(contract)
    return {
        "schema_version": ASSURANCE_SCHEMA_VERSION,
        "run_id": run_id,
        "workflow_id": workflow_id,
        "repository_id": repository_id,
        "source_fingerprint": source_fingerprint,
        "contract_sha256": contract["sha256"],
        "created_at": created_at,
        "updated_at": created_at,
        "reviewer_coverage": reviewer_coverage,
        "claims": [
            {
                **claim,
                "status": "unverified",
                "rationale": None,
                "evidence": [],
            }
            for claim in claims
        ],
    }


def record(
    document: dict[str, Any],
    *,
    claim_id: str,
    status: str,
    evidence_kind: str,
    evidence: str,
    rationale: str | None,
    source_fingerprint: str,
    recorded_at: str,
) -> dict[str, Any]:
    if status not in CLAIM_STATUSES:
        raise AssuranceError(f"Invalid assurance status: {status}")
    if evidence_kind not in EVIDENCE_KINDS:
        raise AssuranceError(f"Invalid assurance evidence kind: {evidence_kind}")
    if not evidence.strip():
        raise AssuranceError("Assurance evidence cannot be empty.")
    if status == "deferred" and not (rationale or "").strip():
        raise AssuranceError("Deferred claims require a rationale.")
    if document.get("source_fingerprint") != source_fingerprint:
        raise AssuranceError("Assurance evidence does not match the reviewed source.")
    claims = document.get("claims")
    if not isinstance(claims, list):
        raise AssuranceError("Assurance document has no valid claims.")
    target = next(
        (
            claim
            for claim in claims
            if isinstance(claim, dict) and claim.get("id") == claim_id
        ),
        None,
    )
    if target is None:
        raise AssuranceError(f"Unknown assurance claim: {claim_id}")
    if bool(target.get("critical")) and status == "deferred":
        raise AssuranceError("Critical invariants cannot be deferred.")
    entry = {
        "kind": evidence_kind,
        "description": evidence.strip(),
        "source_fingerprint": source_fingerprint,
        "recorded_at": recorded_at,
    }
    existing = target.get("evidence")
    existing = existing if isinstance(existing, list) else []
    if entry not in existing:
        existing.append(entry)
    target["evidence"] = existing
    target["status"] = status
    target["rationale"] = (rationale or "").strip() or None
    document["updated_at"] = recorded_at
    return document


def evaluate(
    document: Any,
    *,
    contract: dict[str, Any],
    source_fingerprint: str,
) -> dict[str, Any]:
    claims = validate_contract(contract)
    expected = {str(claim["id"]): claim for claim in claims}
    issues: list[str] = []
    if not isinstance(document, dict):
        return {
            "status": "BLOCK",
            "complete": False,
            "issues": ["assurance.json is missing or malformed"],
            "counts": {},
        }
    if document.get("schema_version") != ASSURANCE_SCHEMA_VERSION:
        issues.append("assurance schema version is invalid")
    if document.get("contract_sha256") != contract.get("sha256"):
        issues.append("assurance contract fingerprint does not match")
    if document.get("source_fingerprint") != source_fingerprint:
        issues.append("assurance source fingerprint does not match")
    raw_claims = document.get("claims")
    raw_claims = raw_claims if isinstance(raw_claims, list) else []
    actual: dict[str, dict[str, Any]] = {}
    for raw in raw_claims:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            issues.append("assurance contains a malformed claim")
            continue
        identifier = str(raw["id"])
        if identifier in actual:
            issues.append(f"assurance contains duplicate claim {identifier}")
            continue
        actual[identifier] = raw
    if set(actual) != set(expected):
        issues.append("assurance claim IDs do not match the pinned contract")
    deferred: list[str] = []
    verified: list[str] = []
    unverified: list[str] = []
    for identifier, claim in expected.items():
        raw = actual.get(identifier, {})
        if any(raw.get(key) != claim.get(key) for key in ("text", "kind", "critical")):
            issues.append(f"assurance claim {identifier} contradicts the contract")
        status = raw.get("status")
        evidence = raw.get("evidence")
        evidence = evidence if isinstance(evidence, list) else []
        valid_evidence = bool(evidence) and all(
            isinstance(item, dict)
            and item.get("kind") in EVIDENCE_KINDS
            and isinstance(item.get("description"), str)
            and bool(item["description"].strip())
            and item.get("source_fingerprint") == source_fingerprint
            for item in evidence
        )
        if not valid_evidence:
            issues.append(f"claim {identifier} has no fresh concrete evidence")
        if status == "verified" and valid_evidence:
            verified.append(identifier)
        elif status == "deferred" and not claim["critical"] and valid_evidence:
            if not isinstance(raw.get("rationale"), str) or not raw["rationale"].strip():
                issues.append(f"deferred claim {identifier} has no rationale")
                unverified.append(identifier)
            else:
                deferred.append(identifier)
        else:
            if status == "deferred" and claim["critical"]:
                issues.append(f"critical invariant {identifier} cannot be deferred")
            unverified.append(identifier)
    status = "BLOCK" if issues or unverified else "PASS_WITH_FINDINGS" if deferred else "PASS_CLEAN"
    return {
        "status": status,
        "complete": status != "BLOCK",
        "issues": sorted(set(issues)),
        "counts": {
            "total": len(claims),
            "verified": len(verified),
            "deferred": len(deferred),
            "unverified": len(set(unverified)),
            "critical": sum(bool(claim["critical"]) for claim in claims),
        },
        "verified_claim_ids": sorted(verified),
        "deferred_claim_ids": sorted(deferred),
        "unverified_claim_ids": sorted(set(unverified)),
    }


def render_summary(document: dict[str, Any], evaluation: dict[str, Any]) -> str:
    lines = [
        "# Claim-to-Evidence Assurance",
        "",
        f"Status: {evaluation['status']}",
        f"Source fingerprint: {document.get('source_fingerprint')}",
        "",
    ]
    for claim in document.get("claims", []):
        if not isinstance(claim, dict):
            continue
        lines.extend(
            [
                f"## {claim.get('id')} [{claim.get('status')}]",
                str(claim.get("text") or ""),
                f"Kind: {claim.get('kind')}",
            ]
        )
        for evidence in claim.get("evidence", []):
            if isinstance(evidence, dict):
                lines.append(
                    f"- {evidence.get('kind')}: {evidence.get('description')}"
                )
        if claim.get("rationale"):
            lines.append(f"- Rationale: {claim['rationale']}")
        lines.append("")
    if evaluation.get("issues"):
        lines.extend(["## Blocking issues", ""])
        lines.extend(f"- {issue}" for issue in evaluation["issues"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
