"""
Provenance Guard — Flask API.

Endpoints:
  POST /submit            classify text, persist the decision to the SQLite audit log
  POST /appeal            contest a classification (sets status to "under review")
  GET  /content/<id>      fetch a stored decision
  GET  /log               structured audit log (?limit=N optional)
  GET  /health            liveness + whether the LLM signal is configured

Rate limiting (flask-limiter, keyed per creator via X-Creator-Id, IP fallback):
  - POST /submit : 10 / minute  (blocks DoS / token-exhaustion on the paid LLM path)
  - global       : 200 / hour   (backstop across all endpoints)
See README "Rate limiting" for the reasoning behind these numbers.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import database
import detection

load_dotenv()
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# Guarantee the schema exists before serving any request.
database.init_db()


def client_key() -> str:
    """Rate-limit identity: creator_id from the JSON body (per-account limiting),
    falling back to an X-Creator-Id header, then the remote IP."""
    body = request.get_json(silent=True) or {}
    return (
        (body.get("creator_id") or "").strip()
        or request.headers.get("X-Creator-Id")
        or get_remote_address()
    )


limiter = Limiter(
    key_func=client_key,
    app=app,
    default_limits=["200 per hour"],
    storage_uri="memory://",
)


@app.get("/health")
def health():
    return jsonify({"status": "ok", "llm_signal": bool(os.getenv("GROQ_API_KEY"))})


@app.post("/submit")
@limiter.limit("10 per minute;100 per day")
def submit():
    data = request.get_json(silent=True) or {}
    creator_id = (data.get("creator_id") or "").strip()
    content_type = (data.get("content_type") or "text").strip()
    if not creator_id:
        return jsonify({"error": "field 'creator_id' is required and must be non-empty"}), 400

    # Route to the modality-specific pipeline.
    if content_type == "image_metadata":
        metadata = data.get("metadata")
        if not isinstance(metadata, dict) or not metadata:
            return jsonify({"error": "field 'metadata' (object) is required for content_type 'image_metadata'"}), 400
        try:
            result = detection.evaluate_image_metadata(metadata)
        except Exception:
            app.logger.exception("image submission processing failed")
            return jsonify({"error": "internal error while processing submission"}), 500
    elif content_type == "text":
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"error": "field 'text' is required and must be non-empty"}), 400
        if len(text) > 50_000:
            return jsonify({"error": "text exceeds 50,000 character limit"}), 413
        try:
            result = detection.evaluate_content(text)
        except Exception:
            app.logger.exception("submission processing failed")
            return jsonify({"error": "internal error while processing submission"}), 500
    else:
        return jsonify({"error": "content_type must be 'text' or 'image_metadata'"}), 400

    # Provenance certificate (stretch): does this creator hold a "Verified Human" credential?
    creator = database.get_creator(creator_id)
    verified = creator is not None

    try:
        content_id = database.insert_decision(result, creator_id, verified_creator=verified)
    except Exception:
        app.logger.exception("failed to persist decision")
        return jsonify({"error": "internal error while processing submission"}), 500

    label = result["transparency_label"]
    response = {
        "content_id": content_id,
        "creator_id": creator_id,
        "content_type": result["content_type"],
        "attribution": result["attribution"],
        "confidence": result["confidence"],
        "label": label,
        "status": "classified",
        "final_p_ai": result["final_p_ai"],
        "signals": result["signals_detail"],
        "weights": result["weights"],
    }
    if verified:
        response["label"] = (
            f"✅ Verified Human Creator (certificate {creator['certificate_id']}). "
            + label
        )
        response["provenance_certificate"] = {
            "certificate_id": creator["certificate_id"],
            "verified_at": creator["verified_at"],
        }
    return jsonify(response), 201


@app.post("/appeal")
@limiter.limit("5 per minute")
def appeal():
    data = request.get_json(silent=True) or {}
    content_id = (data.get("content_id") or "").strip()
    # Accept creator_reasoning (canonical) or reason (alias) for convenience.
    reasoning = (data.get("creator_reasoning") or data.get("reason") or "").strip()
    if not content_id or not reasoning:
        return jsonify(
            {"error": "fields 'content_id' and 'creator_reasoning' are both required"}
        ), 400

    try:
        updated = database.register_appeal(content_id, reasoning)
    except Exception:
        app.logger.exception("appeal processing failed")
        return jsonify({"error": "internal error while processing appeal"}), 500

    if updated is None:
        return jsonify({"error": f"no submission found with id {content_id}"}), 404

    return jsonify(
        {
            "content_id": content_id,
            "status": updated["status"],
            "appeal_reasoning": updated["appeal_reasoning"],
            "original_attribution": updated["attribution"],
            "original_confidence": updated["confidence"],
            "message": "Your appeal has been logged. This content is now under review.",
        }
    ), 201


@app.get("/content/<content_id>")
def content(content_id: str):
    row = database.get_decision(content_id)
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(row)


@app.get("/log")
def log():
    limit = request.args.get("limit", type=int)
    return jsonify({"entries": database.fetch_log(limit=limit)})


@app.post("/verify")
@limiter.limit("5 per minute")
def verify():
    """Provenance certificate (stretch): a creator earns a 'Verified Human' credential
    by signing a human-authorship attestation."""
    data = request.get_json(silent=True) or {}
    creator_id = (data.get("creator_id") or "").strip()
    attestation = (data.get("attestation") or "").strip()
    if not creator_id:
        return jsonify({"error": "field 'creator_id' is required"}), 400
    if len(attestation) < 20:
        return jsonify(
            {"error": "field 'attestation' is required (a signed human-authorship "
                      "statement of at least 20 characters)"}
        ), 400

    cert = database.verify_creator(creator_id, attestation)
    return jsonify(
        {
            "creator_id": cert["creator_id"],
            "certificate_id": cert["certificate_id"],
            "verified_at": cert["verified_at"],
            "badge": "✅ Verified Human Creator",
            "message": "Verification complete. Your content will now display a Verified "
                       "Human Creator badge.",
        }
    ), 201


@app.get("/analytics")
def analytics():
    """Analytics dashboard (stretch): detection patterns + platform health."""
    return jsonify(database.analytics())


@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify(
        {
            "error": "rate limit exceeded",
            "detail": str(e.description),
            "hint": "Creative work is submitted occasionally, not in bursts.",
        }
    ), 429


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
