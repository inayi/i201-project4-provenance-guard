"""
Provenance Guard — SQLite persistence layer.

Tables:
  audit_log  — one structured row per attribution decision (text OR image modality),
               plus appeal metadata. The three text-signal columns are nullable (NULL
               for image rows); the full per-signal breakdown for any modality is stored
               as JSON in signals_json.
  creators   — the "Verified Human" certificate registry (stretch feature).

Appeals mutate only status + appeal_reasoning of an existing row; the original decision
and all raw scores are never overwritten.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "provenance.db")

_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def _now() -> str:
    # ISO-8601 UTC, millisecond precision, trailing 'Z' (e.g. 2026-07-01T14:32:10.123Z)
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def init_db() -> None:
    """Create tables if they do not already exist."""
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                content_id        TEXT PRIMARY KEY,
                creator_id        TEXT NOT NULL,
                content_type      TEXT NOT NULL DEFAULT 'text',
                timestamp         TEXT NOT NULL,
                raw_text_snippet  TEXT NOT NULL,
                burstiness_score  REAL,
                repetition_score  REAL,
                llm_score         REAL,
                signals_json      TEXT NOT NULL,
                final_p_ai        REAL NOT NULL,
                attribution       TEXT NOT NULL,
                confidence        REAL NOT NULL,
                verified_creator  INTEGER NOT NULL DEFAULT 0,
                status            TEXT NOT NULL DEFAULT 'classified',
                appeal_reasoning  TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS creators (
                creator_id      TEXT PRIMARY KEY,
                certificate_id  TEXT NOT NULL,
                verified_at     TEXT NOT NULL,
                attestation     TEXT NOT NULL
            )
            """
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------
def insert_decision(record: dict, creator_id: str, verified_creator: bool = False) -> str:
    """
    Insert one attribution decision (text or image modality). `record` is the dict from
    detection.evaluate_content() / detection.evaluate_image_metadata(). Returns content_id.
    """
    content_id = str(uuid.uuid4())
    snippet = record["text"][:200] + ("…" if len(record["text"]) > 200 else "")
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO audit_log (
                content_id, creator_id, content_type, timestamp, raw_text_snippet,
                burstiness_score, repetition_score, llm_score, signals_json,
                final_p_ai, attribution, confidence, verified_creator,
                status, appeal_reasoning
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'classified', NULL)
            """,
            (
                content_id,
                creator_id,
                record.get("content_type", "text"),
                _now(),
                snippet,
                record.get("burstiness_score"),
                record.get("repetition_score"),
                record.get("llm_score"),
                json.dumps(record.get("signals_detail", {})),
                float(record["final_p_ai"]),
                record["attribution"],
                float(record["confidence"]),
                1 if verified_creator else 0,
            ),
        )
        conn.commit()
    return content_id


def get_decision(content_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM audit_log WHERE content_id = ?", (content_id,)
        ).fetchone()
    return dict(row) if row else None


def register_appeal(content_id: str, reasoning: str) -> dict | None:
    """Set status to 'under_review' and store the reasoning. None if id unknown."""
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT content_id FROM audit_log WHERE content_id = ?", (content_id,)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            """
            UPDATE audit_log
               SET status = 'under_review',
                   appeal_reasoning = ?
             WHERE content_id = ?
            """,
            (reasoning, content_id),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM audit_log WHERE content_id = ?", (content_id,)
        ).fetchone()
    return dict(updated) if updated else None


def fetch_log(limit: int | None = None) -> list[dict]:
    if limit:
        query = "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?"
        params: tuple = (int(limit),)
    else:
        query = "SELECT * FROM audit_log ORDER BY timestamp ASC"
        params = ()
    with _LOCK, _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Provenance certificate (stretch feature)
# ---------------------------------------------------------------------------
def verify_creator(creator_id: str, attestation: str) -> dict:
    """Issue (or return the existing) 'Verified Human' certificate for a creator."""
    with _LOCK, _connect() as conn:
        existing = conn.execute(
            "SELECT * FROM creators WHERE creator_id = ?", (creator_id,)
        ).fetchone()
        if existing:
            return dict(existing)
        cert = {
            "creator_id": creator_id,
            "certificate_id": str(uuid.uuid4()),
            "verified_at": _now(),
            "attestation": attestation,
        }
        conn.execute(
            "INSERT INTO creators (creator_id, certificate_id, verified_at, attestation) "
            "VALUES (?, ?, ?, ?)",
            (cert["creator_id"], cert["certificate_id"], cert["verified_at"], cert["attestation"]),
        )
        conn.commit()
    return cert


def get_creator(creator_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM creators WHERE creator_id = ?", (creator_id,)
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Analytics dashboard (stretch feature)
# ---------------------------------------------------------------------------
def analytics() -> dict:
    """Aggregate detection patterns + platform health from the audit log."""
    with _LOCK, _connect() as conn:
        rows = conn.execute("SELECT * FROM audit_log").fetchall()
        verified_count = conn.execute("SELECT COUNT(*) FROM creators").fetchone()[0]

    total = len(rows)
    dist = {"likely_ai": 0, "uncertain": 0, "likely_human": 0}
    appeals = 0
    conf_sum = 0.0
    burst_vals, rep_vals, llm_vals = [], [], []
    modality = {"text": 0, "image_metadata": 0}

    for r in rows:
        dist[r["attribution"]] = dist.get(r["attribution"], 0) + 1
        if r["status"] == "under_review":
            appeals += 1
        conf_sum += r["confidence"]
        modality[r["content_type"]] = modality.get(r["content_type"], 0) + 1
        if r["burstiness_score"] is not None:
            burst_vals.append(r["burstiness_score"])
        if r["repetition_score"] is not None:
            rep_vals.append(r["repetition_score"])
        if r["llm_score"] is not None:
            llm_vals.append(r["llm_score"])

    def _avg(xs):
        return round(sum(xs) / len(xs), 4) if xs else 0.0

    return {
        "total_submissions": total,
        "attribution_distribution": dist,
        "appeal_count": appeals,
        "appeal_rate": round(appeals / total, 4) if total else 0.0,
        "avg_confidence": round(conf_sum / total, 4) if total else 0.0,
        "uncertain_rate": round(dist["uncertain"] / total, 4) if total else 0.0,
        "modality_distribution": modality,
        "verified_creators": verified_count,
        "avg_signal_scores": {
            "burstiness": _avg(burst_vals),
            "repetition": _avg(rep_vals),
            "llm": _avg(llm_vals),
        },
    }


# Ensure schema exists on import.
init_db()
