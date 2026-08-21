"""Private, rebuildable evidence memory for multi-model-review artifacts."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence


SCHEMA_VERSION = 1


def normalized_text(value: Any) -> str:
    """Return a deterministic token-normalized representation."""
    return " ".join(re.findall(r"[a-z0-9]+", str(value).lower()))


def title_similarity(left: Any, right: Any) -> float:
    """Measure deterministic token overlap without model calls or embeddings."""
    left_tokens = set(normalized_text(left).split())
    right_tokens = set(normalized_text(right).split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _field_relevance(query_tokens: set[str], value: Any) -> float:
    """Score a field without letting one broad token dominate retrieval."""
    if value is None:
        return 0.0
    normalized = normalized_text(value)
    if not normalized:
        return 0.0
    field_tokens = set(normalized.split())
    if not query_tokens or not field_tokens:
        return 0.0
    overlap = len(query_tokens & field_tokens)
    if not overlap:
        return 0.0
    query_coverage = overlap / len(query_tokens)
    field_precision = overlap / len(field_tokens)
    jaccard = overlap / len(query_tokens | field_tokens)
    coverage_weight = (
        0.6 if len(query_tokens) >= 3 else 0.25 if len(query_tokens) == 2 else 0.1
    )
    return max(
        jaccard,
        coverage_weight * query_coverage
        + (1.0 - coverage_weight) * field_precision,
    )


def evidence_relevance(query: Any, row: sqlite3.Row) -> tuple[float, list[str]]:
    """Rank verified evidence across its searchable structured fields."""
    query_tokens = set(normalized_text(query).split())
    if not query_tokens:
        return 0.0, []
    field_weights = {
        "title": 1.0,
        "location": 0.9,
        "evidence_text": 0.75,
        "action_text": 0.75,
        "verification_text": 0.7,
    }
    scores: list[float] = []
    matched_fields: list[str] = []
    for field, weight in field_weights.items():
        relevance = _field_relevance(query_tokens, row[field])
        if relevance <= 0:
            continue
        scores.append(relevance * weight)
        matched_fields.append(
            {
                "evidence_text": "evidence",
                "action_text": "action",
                "verification_text": "verification",
            }.get(field, field)
        )
    if not scores:
        return 0.0, []
    # Multiple independent matching fields are stronger than one prose hit,
    # but keep the bounded score compatible with the existing 0..1 contract.
    multi_field_bonus = min(0.08, 0.02 * (len(scores) - 1))
    return min(1.0, max(scores) + multi_field_bonus), matched_fields


def _connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(database_path, timeout=30)
    database_path.chmod(0o600)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evidence (
            event_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            lineage_root TEXT NOT NULL,
            run_id TEXT NOT NULL,
            repository_id TEXT,
            repository_name TEXT,
            source_fingerprint TEXT,
            phase TEXT,
            round_number INTEGER,
            kind TEXT NOT NULL,
            reviewer TEXT,
            severity TEXT,
            item_id TEXT NOT NULL,
            title TEXT NOT NULL,
            normalized_title TEXT NOT NULL,
            location TEXT,
            decision TEXT,
            evidence_text TEXT,
            action_text TEXT,
            verification_text TEXT,
            decided_at TEXT,
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS evidence_repository_idx
            ON evidence(repository_id, kind);
        CREATE INDEX IF NOT EXISTS evidence_lineage_idx
            ON evidence(lineage_root, created_at);
        CREATE INDEX IF NOT EXISTS evidence_title_idx
            ON evidence(normalized_title);
        """
    )
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    return connection


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    """Open an existing evidence index without schema or permission writes."""
    uri = database_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


@contextmanager
def _open_database(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield a transactional connection and always release its file handle."""
    connection = _connect(database_path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


@contextmanager
def _open_database_read_only(
    database_path: Path,
) -> Iterator[sqlite3.Connection]:
    """Yield a read-only connection and always release its file handle."""
    connection = _connect_read_only(database_path)
    try:
        yield connection
    finally:
        connection.close()


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _triage_items(triage: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for key in ("findings", "test_gaps"):
        value = triage.get(key)
        if isinstance(value, list):
            yield from (item for item in value if isinstance(item, dict))


def upsert_run(
    database_path: Path,
    run_dir: Path,
    *,
    lineage_root: str,
) -> int | None:
    """Replace one run's indexed evidence atomically and return its item count."""
    metadata = _read_object(run_dir / "metadata.json")
    triage = _read_object(run_dir / "triage.json")
    if metadata is None or triage is None:
        return None
    run_id = str(metadata.get("run_id") or run_dir.name)
    workflow_id = str(metadata.get("workflow_id") or "")
    repository = metadata.get("repository")
    repository = repository if isinstance(repository, dict) else {}
    try:
        round_number = int(metadata.get("round") or 0)
    except (TypeError, ValueError):
        return None
    rows: list[tuple[Any, ...]] = []
    for item in _triage_items(triage):
        item_id = str(item.get("id") or "")
        title = str(item.get("title") or "").strip()
        if not item_id or not title:
            continue
        rows.append(
            (
                f"{run_id}:{item_id}",
                workflow_id,
                lineage_root,
                run_id,
                str(repository.get("id") or "") or None,
                str(repository.get("name") or "") or None,
                str(metadata.get("source_fingerprint") or "") or None,
                str(metadata.get("phase") or "repair"),
                round_number,
                str(item.get("kind") or "finding"),
                str(item.get("reviewer") or "") or None,
                str(item.get("severity") or "") or None,
                item_id,
                title,
                normalized_text(title),
                str(item.get("location") or "") or None,
                str(item.get("decision") or "pending"),
                str(item.get("evidence") or "") or None,
                str(item.get("action") or "") or None,
                str(item.get("verification") or "") or None,
                str(item.get("decided_at") or "") or None,
                str(metadata.get("created_at") or "") or None,
            )
        )
    with _open_database(database_path) as connection:
        connection.execute("DELETE FROM evidence WHERE run_id = ?", (run_id,))
        connection.executemany(
            """
            INSERT INTO evidence(
                event_id, workflow_id, lineage_root, run_id, repository_id,
                repository_name, source_fingerprint, phase, round_number, kind,
                reviewer, severity, item_id, title, normalized_title, location,
                decision, evidence_text, action_text, verification_text,
                decided_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('updated_at', ?)",
            (dt.datetime.now(dt.timezone.utc).isoformat(),),
        )
    return len(rows)


def rebuild(
    database_path: Path,
    runs: Sequence[tuple[Path, str]],
) -> dict[str, Any]:
    """Rebuild the derived index from authoritative JSON artifacts."""
    temporary = database_path.with_name(f".{database_path.name}-{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    indexed_runs = 0
    indexed_items = 0
    skipped_runs = 0
    try:
        with _open_database(temporary) as connection:
            connection.execute("DELETE FROM evidence")
        for run_dir, lineage_root in runs:
            count = upsert_run(temporary, run_dir, lineage_root=lineage_root)
            if count is None:
                skipped_runs += 1
                continue
            indexed_runs += 1
            indexed_items += count
        temporary.replace(database_path)
        database_path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
        temporary.with_suffix(temporary.suffix + "-wal").unlink(missing_ok=True)
        temporary.with_suffix(temporary.suffix + "-shm").unlink(missing_ok=True)
    return {
        "database": str(database_path),
        "runs": indexed_runs,
        "skipped_runs": skipped_runs,
        "evidence_items": indexed_items,
    }


def _rank_rows(
    query: str,
    rows: Sequence[sqlite3.Row],
    *,
    limit: int,
    minimum_similarity: float,
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for row in rows:
        similarity, matched_fields = evidence_relevance(query, row)
        if similarity < minimum_similarity:
            continue
        ranked.append(
            {
                "similarity": round(similarity, 3),
                "matched_fields": matched_fields,
                "workflow_id": row["workflow_id"],
                "lineage_root": row["lineage_root"],
                "run_id": row["run_id"],
                "repository_id": row["repository_id"],
                "source_fingerprint": row["source_fingerprint"],
                "phase": row["phase"],
                "round": row["round_number"],
                "kind": row["kind"],
                "reviewer": row["reviewer"],
                "severity": row["severity"],
                "item_id": row["item_id"],
                "title": row["title"],
                "location": row["location"],
                "decision": row["decision"],
                "evidence": row["evidence_text"],
                "action": row["action_text"],
                "verification": row["verification_text"],
                "decided_at": row["decided_at"],
            }
        )
    ranked.sort(
        key=lambda item: (
            item["similarity"],
            str(item["decided_at"]),
            str(item["run_id"]),
            str(item["item_id"]),
        ),
        reverse=True,
    )
    return ranked[:limit]


def search_many(
    database_path: Path,
    queries: Sequence[str],
    *,
    repository_id: str | None = None,
    kind: str | None = None,
    exclude_run_id: str | None = None,
    limit: int = 20,
    minimum_similarity: float = 0.35,
    decided_only: bool = True,
) -> list[list[dict[str, Any]]]:
    """Rank multiple Codex-only evidence queries with one database scan."""
    if not queries:
        return []
    if not database_path.exists():
        return [[] for _ in queries]
    clauses: list[str] = []
    parameters: list[Any] = []
    if repository_id:
        clauses.append("repository_id = ?")
        parameters.append(repository_id)
    if kind:
        clauses.append("kind = ?")
        parameters.append(kind)
    if exclude_run_id:
        clauses.append("run_id != ?")
        parameters.append(exclude_run_id)
    if decided_only:
        clauses.append("decision IN ('fixed', 'rejected', 'covered', 'deferred')")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _open_database_read_only(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM evidence" + where + " ORDER BY created_at DESC",
            parameters,
        ).fetchall()
    return [
        _rank_rows(
            query,
            rows,
            limit=limit,
            minimum_similarity=minimum_similarity,
        )
        if normalized_text(query)
        else []
        for query in queries
    ]


def search(
    database_path: Path,
    query: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Return ranked prior evidence for one Codex query."""
    return search_many(database_path, [query], **kwargs)[0]


def status(database_path: Path) -> dict[str, Any]:
    if not database_path.exists():
        return {
            "database": str(database_path),
            "exists": False,
            "evidence_items": 0,
        }
    with _open_database_read_only(database_path) as connection:
        evidence_items = int(
            connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        )
        repositories = int(
            connection.execute(
                "SELECT COUNT(DISTINCT repository_id) FROM evidence"
            ).fetchone()[0]
        )
        workflows = int(
            connection.execute(
                "SELECT COUNT(DISTINCT workflow_id) FROM evidence"
            ).fetchone()[0]
        )
        updated = connection.execute(
            "SELECT value FROM metadata WHERE key = 'updated_at'"
        ).fetchone()
    return {
        "database": str(database_path),
        "exists": True,
        "size_bytes": database_path.stat().st_size,
        "evidence_items": evidence_items,
        "repositories": repositories,
        "workflows": workflows,
        "updated_at": updated[0] if updated else None,
    }


def compact(database_path: Path) -> dict[str, Any]:
    """Compact only the rebuildable index; authoritative artifacts are untouched."""
    if not database_path.exists():
        return status(database_path)
    before = database_path.stat().st_size
    connection = _connect(database_path)
    try:
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    result = status(database_path)
    result["size_before_bytes"] = before
    result["size_after_bytes"] = database_path.stat().st_size
    return result
