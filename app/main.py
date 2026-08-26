import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory


ROOT_DIR = Path(__file__).resolve().parents[1]
PUBLIC_DIR = ROOT_DIR / "public"
DEFAULT_USER_CLIENT_ID = "system"
DEFAULT_USER_NAME = "system"
DEFAULT_ALBUM_NAME = "未分類相簿"
RUN_STATUS_DRAFT = "draft"
RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"
ACTIVE_RUN_STATUSES = (RUN_STATUS_QUEUED, RUN_STATUS_RUNNING)
JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
TERMINAL_JOB_STATUSES = (JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED)


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def env_float(name: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_app() -> Flask:
    load_dotenv(ROOT_DIR / ".env")

    app = Flask(__name__, static_folder=None)
    max_upload_mb = env_int("MAX_UPLOAD_MB", 5, minimum=1)
    max_photos_per_album = env_int("MAX_PHOTOS_PER_ALBUM", 50, minimum=1)
    max_active_prompt_runs = env_int("MAX_ACTIVE_PROMPT_RUNS", 5, minimum=1)
    app.config["MAX_CONTENT_LENGTH"] = (max_upload_mb + 2) * 1024 * 1024
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")

    db_path = Path(os.getenv("DATABASE_PATH", "data/app.db"))
    upload_dir = Path(os.getenv("UPLOAD_DIR", "uploads"))
    if not db_path.is_absolute():
        db_path = ROOT_DIR / db_path
    if not upload_dir.is_absolute():
        upload_dir = ROOT_DIR / upload_dir
    db_path.parent.mkdir(parents=True, exist_ok=True)
    upload_dir.mkdir(parents=True, exist_ok=True)

    init_db(db_path)
    worker = AnalysisWorker(
        db_path=db_path,
        upload_dir=upload_dir,
        max_workers=env_int("GEMINI_MAX_CONCURRENT", 1, minimum=1),
        min_seconds_between_requests=env_float("GEMINI_MIN_SECONDS_BETWEEN_REQUESTS", 6.0, minimum=0.0),
        max_retries=env_int("GEMINI_MAX_RETRIES", 3, minimum=0),
        retry_base_seconds=env_int("GEMINI_RETRY_BASE_SECONDS", 30, minimum=1),
        retry_max_seconds=env_int("GEMINI_RETRY_MAX_SECONDS", 300, minimum=1),
        poll_seconds=env_float("QUEUE_POLL_SECONDS", 1.0, minimum=0.1),
    )
    if os.getenv("QUEUE_WORKER_ENABLED", "1").lower() not in ("0", "false", "no"):
        worker.start()
    app.config["ANALYSIS_WORKER"] = worker

    @app.errorhandler(ValueError)
    def handle_value_error(error: ValueError):
        return jsonify({"error": str(error)}), 400

    @app.errorhandler(RuntimeError)
    def handle_runtime_error(error: RuntimeError):
        return jsonify({"error": str(error)}), 502

    @app.errorhandler(requests.RequestException)
    def handle_request_error(error: requests.RequestException):
        return jsonify({"error": f"Gemini request failed: {error}"}), 502

    @app.get("/")
    def index():
        return send_from_directory(PUBLIC_DIR, "index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "time": utc_now()})

    @app.get("/api/config")
    def public_config():
        return jsonify(
            {
                "model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
                "maxPhotosPerAlbum": max_photos_per_album,
                "maxUploadMb": max_upload_mb,
                "maxActivePromptRuns": max_active_prompt_runs,
                "geminiMaxConcurrent": worker.max_workers,
                "geminiMinSecondsBetweenRequests": worker.throttle.min_seconds_between_requests,
            }
        )

    @app.get("/uploads/<path:path>")
    def uploaded_file(path: str):
        return send_from_directory(upload_dir, path)

    @app.get("/api/albums")
    def list_albums():
        with connect(db_path) as db:
            rows = db.execute(
                """
                SELECT a.id, a.name, a.description, a.created_at, a.updated_at,
                       COUNT(DISTINCT p.id) AS photo_count,
                       COUNT(DISTINCT pr.id) AS prompt_run_count,
                       best.id AS best_prompt_run_id,
                       best.version AS best_prompt_version,
                       best.name AS best_prompt_name
                FROM albums a
                LEFT JOIN photos p ON p.album_id = a.id
                LEFT JOIN prompt_runs pr ON pr.album_id = a.id
                LEFT JOIN prompt_runs best ON best.album_id = a.id AND best.is_best = 1
                GROUP BY a.id
                ORDER BY a.created_at DESC, a.id DESC
                """
            ).fetchall()
        return jsonify([serialize_album(row) for row in rows])

    @app.post("/api/albums")
    def create_album():
        payload = request.get_json(force=True)
        name = require_text(payload, "name")[:160]
        description = str(payload.get("description") or "")[:1000]
        now = utc_now()
        with connect(db_path) as db:
            cur = db.execute(
                """
                INSERT INTO albums (name, description, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (name, description, now, now),
            )
            db.commit()
            album_id = cur.lastrowid
        return jsonify({"id": album_id, "name": name, "description": description})

    @app.get("/api/albums/<int:album_id>")
    def get_album(album_id: int):
        with connect(db_path) as db:
            row = get_album_row(db, album_id)
        return jsonify(serialize_album(row))

    @app.patch("/api/albums/<int:album_id>")
    def update_album(album_id: int):
        payload = request.get_json(force=True)
        with connect(db_path) as db:
            get_album_row(db, album_id)
            if "name" in payload:
                name = str(payload.get("name") or "").strip()[:160]
                if not name:
                    return jsonify({"error": "name is required"}), 400
                db.execute("UPDATE albums SET name = ?, updated_at = ? WHERE id = ?", (name, utc_now(), album_id))
            if "description" in payload:
                description = str(payload.get("description") or "")[:1000]
                db.execute(
                    "UPDATE albums SET description = ?, updated_at = ? WHERE id = ?",
                    (description, utc_now(), album_id),
                )
            db.commit()
        return jsonify({"ok": True})

    @app.delete("/api/albums/<int:album_id>")
    def delete_album(album_id: int):
        with connect(db_path) as db:
            get_album_row(db, album_id)
            storage_paths = [
                row["storage_path"]
                for row in db.execute("SELECT storage_path FROM photos WHERE album_id = ?", (album_id,)).fetchall()
            ]
            run_ids = [row["id"] for row in db.execute("SELECT id FROM prompt_runs WHERE album_id = ?", (album_id,))]
            photo_ids = [row["id"] for row in db.execute("SELECT id FROM photos WHERE album_id = ?", (album_id,))]
            if run_ids:
                db.execute(f"DELETE FROM analysis_jobs WHERE prompt_run_id IN ({placeholders(run_ids)})", run_ids)
                db.execute(f"DELETE FROM analysis_results WHERE prompt_run_id IN ({placeholders(run_ids)})", run_ids)
            if photo_ids:
                db.execute(f"DELETE FROM analysis_jobs WHERE photo_id IN ({placeholders(photo_ids)})", photo_ids)
                db.execute(f"DELETE FROM analysis_results WHERE photo_id IN ({placeholders(photo_ids)})", photo_ids)
            db.execute("DELETE FROM prompt_runs WHERE album_id = ?", (album_id,))
            db.execute("DELETE FROM photos WHERE album_id = ?", (album_id,))
            db.execute("DELETE FROM albums WHERE id = ?", (album_id,))
            db.commit()
            remove_unreferenced_files(db, upload_dir, storage_paths)
        return jsonify({"ok": True})

    @app.get("/api/albums/<int:album_id>/photos")
    def list_album_photos(album_id: int):
        prompt_run_id = int(request.args.get("prompt_run_id") or 0)
        with connect(db_path) as db:
            get_album_row(db, album_id)
            if prompt_run_id:
                run = get_prompt_run_row(db, prompt_run_id)
                if int(run["album_id"]) != album_id:
                    return jsonify({"error": "prompt_run_id does not belong to this album"}), 400
            else:
                run = db.execute(
                    """
                    SELECT id
                    FROM prompt_runs
                    WHERE album_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (album_id,),
                ).fetchone()
                prompt_run_id = int(run["id"]) if run else 0
            rows = db.execute(
                """
                SELECT p.id, p.client_photo_id, p.original_filename, p.storage_path, p.created_at,
                       ar.tags_json, ar.description, ar.location_guess,
                       ar.confidence, ar.lat, ar.lng
                FROM photos p
                LEFT JOIN analysis_results ar ON ar.photo_id = p.id AND ar.prompt_run_id = ?
                WHERE p.album_id = ?
                ORDER BY p.created_at ASC, p.id ASC
                LIMIT 5000
                """,
                (prompt_run_id, album_id),
            ).fetchall()
        return jsonify([serialize_photo(row) for row in rows])

    @app.post("/api/albums/<int:album_id>/photos")
    def create_album_photo(album_id: int):
        payload = request.get_json(force=True)
        client_photo_id = str(payload.get("client_photo_id") or "")[:80]
        filename = sanitize_filename(str(payload.get("filename") or "uploaded.jpg"))
        image_base64 = require_text(payload, "image_base64")
        image_bytes = decode_image(image_base64)
        if len(image_bytes) > max_upload_mb * 1024 * 1024:
            return jsonify({"error": f"single photo must be <= {max_upload_mb}MB"}), 400
        image_hash = hashlib.sha256(image_bytes).hexdigest()
        image_path = upload_dir / f"{image_hash[:16]}_{filename}"
        if not image_path.exists():
            image_path.write_bytes(image_bytes)

        with connect(db_path) as db:
            get_album_row(db, album_id)
            existing = db.execute(
                "SELECT id FROM photos WHERE album_id = ? AND client_photo_id = ?",
                (album_id, client_photo_id),
            ).fetchone()
            if existing is None:
                photo_count = db.execute("SELECT COUNT(*) FROM photos WHERE album_id = ?", (album_id,)).fetchone()[0]
                if int(photo_count) >= max_photos_per_album:
                    return jsonify({"error": f"album photo limit is {max_photos_per_album}"}), 400
            user_id = upsert_user(db, DEFAULT_USER_CLIENT_ID, DEFAULT_USER_NAME)
            photo_id = upsert_photo(
                db,
                album_id=album_id,
                user_id=user_id,
                client_photo_id=client_photo_id,
                filename=filename,
                image_hash=image_hash,
                storage_path=str(image_path.relative_to(upload_dir)),
            )
            db.commit()
        return jsonify(
            {
                "id": photo_id,
                "client_photo_id": client_photo_id,
                "image_url": f"/uploads/{image_path.relative_to(upload_dir).as_posix()}",
            }
        )

    @app.delete("/api/albums/<int:album_id>/photos/<path:client_photo_id>")
    def delete_album_photo(album_id: int, client_photo_id: str):
        with connect(db_path) as db:
            row = db.execute(
                "SELECT id, storage_path FROM photos WHERE album_id = ? AND client_photo_id = ?",
                (album_id, client_photo_id),
            ).fetchone()
            if row is None:
                return jsonify({"ok": True, "deleted": 0})
            db.execute("DELETE FROM analysis_jobs WHERE photo_id = ?", (row["id"],))
            db.execute("DELETE FROM analysis_results WHERE photo_id = ?", (row["id"],))
            db.execute("DELETE FROM photos WHERE id = ?", (row["id"],))
            db.commit()
            remove_unreferenced_files(db, upload_dir, [row["storage_path"]])
        return jsonify({"ok": True, "deleted": 1})

    @app.get("/api/albums/<int:album_id>/prompt-runs")
    def list_album_prompt_runs(album_id: int):
        with connect(db_path) as db:
            get_album_row(db, album_id)
            runs = db.execute(
                """
                SELECT pr.id, pr.prompt_id, pr.version, pr.name, pr.prompt_text, pr.model,
                       pr.rating, pr.rating_note, pr.is_best, pr.status, pr.completed_at,
                       pr.duration_seconds, pr.created_at,
                       p.version AS prompt_version, p.name AS prompt_name,
                       a.id AS album_id, a.name AS album_name
                FROM prompt_runs pr
                JOIN albums a ON a.id = pr.album_id
                LEFT JOIN prompts p ON p.id = pr.prompt_id
                WHERE pr.album_id = ?
                ORDER BY pr.is_best DESC, pr.created_at DESC, pr.id DESC
                LIMIT 200
                """,
                (album_id,),
            ).fetchall()
            data = [serialize_prompt_run(db, run) for run in runs]
        return jsonify(data)

    @app.get("/api/prompts")
    def list_prompts():
        album_filter = int(request.args.get("album_id") or 0)
        with connect(db_path) as db:
            if album_filter:
                get_album_row(db, album_filter)
            rows = db.execute(
                """
                SELECT id, version, name, prompt_text, normalized_prompt_text, model, created_at, updated_at
                FROM prompts
                ORDER BY updated_at DESC, id DESC
                LIMIT 500
                """
            ).fetchall()
            data = [serialize_prompt(db, row, album_filter=album_filter) for row in rows]
        return jsonify(data)

    @app.delete("/api/prompts/<int:prompt_id>")
    def delete_prompt(prompt_id: int):
        with connect(db_path) as db:
            prompt = db.execute("SELECT id FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
            if prompt is None:
                return jsonify({"ok": True, "deleted": 0})
            run_ids = [row["id"] for row in db.execute("SELECT id FROM prompt_runs WHERE prompt_id = ?", (prompt_id,))]
            if run_ids:
                db.execute(f"DELETE FROM analysis_jobs WHERE prompt_run_id IN ({placeholders(run_ids)})", run_ids)
                db.execute(f"DELETE FROM analysis_results WHERE prompt_run_id IN ({placeholders(run_ids)})", run_ids)
                db.execute(f"DELETE FROM prompt_runs WHERE id IN ({placeholders(run_ids)})", run_ids)
            db.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))
            db.commit()
        return jsonify({"ok": True, "deleted": 1, "deletedRunCount": len(run_ids)})

    @app.post("/api/albums/<int:album_id>/prompt-runs")
    def create_album_prompt_run(album_id: int):
        payload = request.get_json(force=True)
        prompt_text = require_text(payload, "prompt_text")
        model = str(payload.get("model") or "gemini-2.5-flash")[:80]
        name = str(payload.get("name") or "")[:160]
        rating_note = str(payload.get("rating_note") or "")[:2000]
        prompt_id = int(payload.get("prompt_id") or 0)
        with connect(db_path) as db:
            get_album_row(db, album_id)
            user_id = upsert_user(db, DEFAULT_USER_CLIENT_ID, DEFAULT_USER_NAME)
            run_id, version = insert_prompt_run(
                db,
                album_id=album_id,
                user_id=user_id,
                prompt_text=prompt_text,
                model=model,
                name=name,
                rating_note=rating_note,
                prompt_id=prompt_id,
            )
            db.commit()
        return jsonify({"id": run_id, "version": version})

    @app.patch("/api/prompt-runs/<int:run_id>")
    def update_prompt_run(run_id: int):
        payload = request.get_json(force=True)
        rating = payload.get("rating")
        is_best = payload.get("is_best")
        if rating not in (None, "good", "bad"):
            return jsonify({"error": "rating must be good, bad, or null"}), 400
        with connect(db_path) as db:
            run = get_prompt_run_row(db, run_id)
            if is_best is not None:
                if bool(is_best):
                    db.execute("UPDATE prompt_runs SET is_best = 0 WHERE album_id = ?", (run["album_id"],))
                db.execute("UPDATE prompt_runs SET is_best = ? WHERE id = ?", (1 if bool(is_best) else 0, run_id))
            if "rating" in payload:
                db.execute("UPDATE prompt_runs SET rating = ? WHERE id = ?", (rating, run_id))
            if "name" in payload:
                name = str(payload.get("name") or "")[:160]
                db.execute("UPDATE prompt_runs SET name = ? WHERE id = ?", (name, run_id))
                if run["prompt_id"]:
                    db.execute("UPDATE prompts SET name = ?, updated_at = ? WHERE id = ?", (name, utc_now(), run["prompt_id"]))
            if "rating_note" in payload:
                db.execute(
                    "UPDATE prompt_runs SET rating_note = ? WHERE id = ?",
                    (str(payload.get("rating_note") or "")[:2000], run_id),
                )
            db.commit()
        return jsonify({"ok": True})

    @app.post("/api/albums/<int:album_id>/analyze")
    def analyze_album_image(album_id: int):
        payload = request.get_json(force=True)
        run_id = int(payload.get("run_id") or 0)
        client_photo_id = str(payload.get("client_photo_id") or "")[:80]
        filename = sanitize_filename(str(payload.get("filename") or "uploaded.jpg"))
        prompt_text = require_text(payload, "prompt_text")
        model = str(payload.get("model") or "gemini-2.5-flash")[:80]
        image_base64 = require_text(payload, "image_base64")
        image_bytes = decode_image(image_base64)
        image_hash = hashlib.sha256(image_bytes).hexdigest()
        image_path = upload_dir / f"{image_hash[:16]}_{filename}"
        if not image_path.exists():
            image_path.write_bytes(image_bytes)

        parsed, raw_response = call_gemini_image(model, prompt_text, image_base64)

        with connect(db_path) as db:
            get_album_row(db, album_id)
            user_id = upsert_user(db, DEFAULT_USER_CLIENT_ID, DEFAULT_USER_NAME)
            if run_id:
                run = get_prompt_run_row(db, run_id)
                if int(run["album_id"]) != album_id:
                    return jsonify({"error": "run_id does not belong to this album"}), 400
            else:
                run_id, _version = insert_prompt_run(
                    db,
                    album_id=album_id,
                    user_id=user_id,
                    prompt_text=prompt_text,
                    model=model,
                )
            photo_id = upsert_photo(
                db,
                album_id=album_id,
                user_id=user_id,
                client_photo_id=client_photo_id,
                filename=filename,
                image_hash=image_hash,
                storage_path=str(image_path.relative_to(upload_dir)),
            )
            save_analysis_result(db, run_id, photo_id, parsed, raw_response)
            completed_at = utc_now()
            duration_seconds = duration_between_seconds(get_prompt_run_row(db, run_id)["created_at"], completed_at)
            db.execute(
                "UPDATE prompt_runs SET status = ?, completed_at = ?, duration_seconds = ? WHERE id = ?",
                (RUN_STATUS_COMPLETED, completed_at, duration_seconds, run_id),
            )
            db.commit()
        return jsonify({"run_id": run_id, "result": parsed})

    @app.post("/api/albums/<int:album_id>/queue-analysis")
    def queue_album_analysis(album_id: int):
        payload = request.get_json(force=True)
        prompt_text = require_text(payload, "prompt_text")
        model = str(payload.get("model") or os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))[:80]
        name = str(payload.get("name") or "")[:160]
        prompt_id = int(payload.get("prompt_id") or 0)
        client_photo_ids = [str(item)[:80] for item in payload.get("client_photo_ids") or [] if str(item).strip()]
        if not client_photo_ids:
            return jsonify({"error": "client_photo_ids is required"}), 400
        force_rerun = bool(payload.get("force_rerun"))
        preferred_run_id = int(payload.get("preferred_run_id") or 0)
        replace_existing_jobs = bool(payload.get("replace_existing_jobs"))
        normalized_prompt = normalize_prompt_text(prompt_text)

        with connect(db_path) as db:
            get_album_row(db, album_id)
            photos_by_client_id = get_album_photos_by_client_ids(db, album_id, client_photo_ids)
            missing = [item for item in client_photo_ids if item not in photos_by_client_id]
            if missing:
                return jsonify({"error": f"photos not found: {', '.join(missing[:5])}"}), 400

            run = None
            reused_reason = None
            if not force_rerun:
                if preferred_run_id:
                    preferred = get_prompt_run_row(db, preferred_run_id)
                    preferred_status = str(preferred["status"] or "")
                    if (
                        int(preferred["album_id"]) == album_id
                        and str(preferred["model"]) == model
                        and normalize_prompt_text(str(preferred["prompt_text"])) == normalized_prompt
                        and preferred_status in (RUN_STATUS_COMPLETED, *ACTIVE_RUN_STATUSES)
                    ):
                        if replace_existing_jobs:
                            run = preferred
                            reused_reason = "replace"
                        elif preferred_status == RUN_STATUS_COMPLETED:
                            if prompt_run_has_results(
                                db, int(preferred["id"]), [int(row["id"]) for row in photos_by_client_id.values()]
                            ):
                                run = preferred
                                reused_reason = "completed"
                        else:
                            run = preferred
                            reused_reason = "active"
                if run is None:
                    run = find_matching_prompt_run(db, album_id, normalized_prompt, model, ACTIVE_RUN_STATUSES)
                if run:
                    reused_reason = reused_reason or "active"
                else:
                    completed = find_matching_prompt_run(db, album_id, normalized_prompt, model, (RUN_STATUS_COMPLETED,))
                    if completed and prompt_run_has_results(
                        db, int(completed["id"]), [int(row["id"]) for row in photos_by_client_id.values()]
                    ):
                        run = completed
                        reused_reason = "completed"

            if run is None:
                active_count = count_active_prompt_runs(db)
                if active_count >= max_active_prompt_runs:
                    return jsonify({"error": f"active prompt run limit is {max_active_prompt_runs}"}), 429
                user_id = upsert_user(db, DEFAULT_USER_CLIENT_ID, DEFAULT_USER_NAME)
                run_id, _version = insert_prompt_run(
                    db,
                    album_id=album_id,
                    user_id=user_id,
                    prompt_text=prompt_text,
                    model=model,
                    name=name,
                    prompt_id=prompt_id,
                    status=RUN_STATUS_QUEUED,
                )
                run = get_prompt_run_row(db, run_id)
            else:
                run_id = int(run["id"])

            if reused_reason != "completed":
                enqueue_analysis_jobs(
                    db,
                    run_id,
                    [int(row["id"]) for row in photos_by_client_id.values()],
                    replace_existing=replace_existing_jobs,
                )
                refresh_prompt_run_status(db, run_id)
            db.commit()
            run = get_prompt_run_row(db, run_id)
            data = serialize_prompt_run_status(db, run)
            data["reused"] = bool(reused_reason)
            data["reusedReason"] = reused_reason
        return jsonify(data)

    @app.get("/api/prompt-runs/<int:run_id>/status")
    def get_prompt_run_status(run_id: int):
        with connect(db_path) as db:
            run = get_prompt_run_row(db, run_id)
            data = serialize_prompt_run_status(db, run)
        return jsonify(data)

    @app.post("/api/prompt-runs/<int:run_id>/cancel")
    def cancel_prompt_run(run_id: int):
        with connect(db_path) as db:
            get_prompt_run_row(db, run_id)
            now = utc_now()
            db.execute(
                """
                UPDATE analysis_jobs
                SET status = ?, finished_at = ?, last_error = ''
                WHERE prompt_run_id = ? AND status = ?
                """,
                (JOB_STATUS_CANCELLED, now, run_id, JOB_STATUS_QUEUED),
            )
            refresh_prompt_run_status(db, run_id)
            db.commit()
            run = get_prompt_run_row(db, run_id)
            data = serialize_prompt_run_status(db, run)
            data["cancelRequested"] = True
        return jsonify(data)

    @app.post("/api/chat-extract")
    def chat_extract():
        payload = request.get_json(force=True)
        prompt_text = require_text(payload, "prompt")
        model = str(payload.get("model") or "gemini-2.5-flash")[:80]
        parsed, _raw_response = call_gemini_text(model, prompt_text)
        return jsonify(parsed)

    @app.get("/<path:path>")
    def public_file(path: str):
        target = PUBLIC_DIR / path
        if target.exists() and target.is_file():
            return send_from_directory(PUBLIC_DIR, path)
        return send_from_directory(PUBLIC_DIR, "index.html")

    return app


@contextmanager
def connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(db_path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
    finally:
        db.close()


def init_db(db_path: Path) -> None:
    with connect(db_path) as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS albums (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS prompts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL UNIQUE,
                name TEXT NOT NULL DEFAULT '',
                prompt_text TEXT NOT NULL,
                normalized_prompt_text TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(normalized_prompt_text, model)
            );

            CREATE TABLE IF NOT EXISTS prompt_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt_id INTEGER,
                album_id INTEGER NOT NULL,
                user_id INTEGER,
                version INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                prompt_text TEXT NOT NULL,
                normalized_prompt_text TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                rating TEXT,
                rating_note TEXT NOT NULL DEFAULT '',
                is_best INTEGER NOT NULL DEFAULT 0,
                last_job_started_at TEXT,
                completed_at TEXT,
                duration_seconds INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE(album_id, prompt_id, version),
                FOREIGN KEY (prompt_id) REFERENCES prompts(id),
                FOREIGN KEY (album_id) REFERENCES albums(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                album_id INTEGER NOT NULL,
                user_id INTEGER,
                client_photo_id TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                image_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(album_id, client_photo_id),
                FOREIGN KEY (album_id) REFERENCES albums(id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS analysis_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt_run_id INTEGER NOT NULL,
                photo_id INTEGER NOT NULL,
                result_json TEXT NOT NULL,
                raw_response_json TEXT NOT NULL,
                tags_json TEXT NOT NULL,
                description TEXT,
                location_guess TEXT,
                confidence TEXT,
                lat REAL,
                lng REAL,
                input_token_count INTEGER NOT NULL DEFAULT 0,
                output_token_count INTEGER NOT NULL DEFAULT 0,
                total_token_count INTEGER NOT NULL DEFAULT 0,
                thoughts_token_count INTEGER NOT NULL DEFAULT 0,
                token_details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(prompt_run_id, photo_id),
                FOREIGN KEY (prompt_run_id) REFERENCES prompt_runs(id),
                FOREIGN KEY (photo_id) REFERENCES photos(id)
            );

            CREATE TABLE IF NOT EXISTS analysis_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                album_id INTEGER NOT NULL,
                prompt_run_id INTEGER NOT NULL,
                photo_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                scheduled_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(prompt_run_id, photo_id),
                FOREIGN KEY (album_id) REFERENCES albums(id),
                FOREIGN KEY (prompt_run_id) REFERENCES prompt_runs(id),
                FOREIGN KEY (photo_id) REFERENCES photos(id)
            );
            """
        )
        default_album_id = ensure_default_album(db)
        ensure_column(db, "photos", "album_id", "INTEGER")
        ensure_column(db, "prompt_runs", "prompt_id", "INTEGER")
        ensure_column(db, "prompt_runs", "album_id", "INTEGER")
        ensure_column(db, "prompt_runs", "name", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "prompt_runs", "normalized_prompt_text", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "prompt_runs", "status", "TEXT NOT NULL DEFAULT 'completed'")
        ensure_column(db, "prompt_runs", "rating_note", "TEXT NOT NULL DEFAULT ''")
        ensure_column(db, "prompt_runs", "last_job_started_at", "TEXT")
        ensure_column(db, "prompt_runs", "completed_at", "TEXT")
        ensure_column(db, "prompt_runs", "duration_seconds", "INTEGER")
        ensure_column(db, "analysis_results", "input_token_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "analysis_results", "output_token_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "analysis_results", "total_token_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "analysis_results", "thoughts_token_count", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(db, "analysis_results", "token_details_json", "TEXT NOT NULL DEFAULT '{}'")
        db.execute("UPDATE photos SET album_id = ? WHERE album_id IS NULL", (default_album_id,))
        db.execute("UPDATE prompt_runs SET album_id = ? WHERE album_id IS NULL", (default_album_id,))
        rows = db.execute("SELECT id, prompt_text FROM prompt_runs WHERE normalized_prompt_text = ''").fetchall()
        for row in rows:
            db.execute(
                "UPDATE prompt_runs SET normalized_prompt_text = ? WHERE id = ?",
                (normalize_prompt_text(row["prompt_text"]), row["id"]),
            )
        rows = db.execute(
            """
            SELECT id, name, prompt_text, model
            FROM prompt_runs
            WHERE prompt_id IS NULL
            ORDER BY id ASC
            """
        ).fetchall()
        for row in rows:
            prompt_id = get_or_create_prompt(
                db,
                prompt_text=str(row["prompt_text"]),
                model=str(row["model"]),
                name=str(row["name"] or ""),
            )
            db.execute("UPDATE prompt_runs SET prompt_id = ? WHERE id = ?", (prompt_id, row["id"]))
        db.commit()
        migrate_prompt_runs_version_scope(db)
        renumber_prompt_runs_per_prompt(db)
        backfill_prompt_run_durations(db)
        db.commit()


def ensure_default_album(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT id FROM albums ORDER BY id ASC LIMIT 1").fetchone()
    if row:
        return int(row["id"])
    now = utc_now()
    cur = db.execute(
        "INSERT INTO albums (name, description, created_at, updated_at) VALUES (?, '', ?, ?)",
        (DEFAULT_ALBUM_NAME, now, now),
    )
    return int(cur.lastrowid)


def ensure_column(db: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def prompt_runs_needs_version_scope_migration(db: sqlite3.Connection) -> bool:
    has_target_unique = False
    for index_row in db.execute("PRAGMA index_list(prompt_runs)").fetchall():
        if not int(index_row["unique"] or 0):
            continue
        columns = [
            row["name"]
            for row in db.execute(f"PRAGMA index_info({index_row['name']})").fetchall()
        ]
        if columns == ["album_id", "prompt_id", "version"]:
            has_target_unique = True
            continue
        if "version" in columns:
            return True
    return not has_target_unique


def migrate_prompt_runs_version_scope(db: sqlite3.Connection) -> None:
    if not prompt_runs_needs_version_scope_migration(db):
        return
    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    db.executescript(
        """
        CREATE TABLE prompt_runs_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_id INTEGER,
            album_id INTEGER NOT NULL,
            user_id INTEGER,
            version INTEGER NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            prompt_text TEXT NOT NULL,
            normalized_prompt_text TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            rating TEXT,
            rating_note TEXT NOT NULL DEFAULT '',
            is_best INTEGER NOT NULL DEFAULT 0,
            last_job_started_at TEXT,
            completed_at TEXT,
            duration_seconds INTEGER,
            created_at TEXT NOT NULL,
            UNIQUE(album_id, prompt_id, version),
            FOREIGN KEY (prompt_id) REFERENCES prompts(id),
            FOREIGN KEY (album_id) REFERENCES albums(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        INSERT INTO prompt_runs_new
            (id, prompt_id, album_id, user_id, version, name, prompt_text, normalized_prompt_text,
             model, status, rating, rating_note, is_best, last_job_started_at, completed_at, duration_seconds, created_at)
        SELECT
            id, prompt_id, album_id, user_id, version, name, prompt_text, normalized_prompt_text,
            model, status, rating, rating_note, is_best, last_job_started_at, completed_at, duration_seconds, created_at
        FROM prompt_runs;

        DROP TABLE prompt_runs;
        ALTER TABLE prompt_runs_new RENAME TO prompt_runs;
        """
    )
    db.commit()
    db.execute("PRAGMA foreign_keys = ON")


def renumber_prompt_runs_per_prompt(db: sqlite3.Connection) -> None:
    counters: dict[tuple[int, int], int] = {}
    rows = db.execute(
        """
        SELECT id, album_id, prompt_id
        FROM prompt_runs
        WHERE prompt_id IS NOT NULL
        ORDER BY album_id ASC, prompt_id ASC, created_at ASC, id ASC
        """
    ).fetchall()
    for row in rows:
        key = (int(row["album_id"]), int(row["prompt_id"]))
        counters[key] = counters.get(key, 0) + 1
        db.execute("UPDATE prompt_runs SET version = ? WHERE id = ?", (counters[key], row["id"]))


def backfill_prompt_run_durations(db: sqlite3.Connection) -> None:
    rows = db.execute(
        """
        SELECT id, created_at, completed_at
        FROM prompt_runs
        WHERE completed_at IS NOT NULL
          AND duration_seconds IS NULL
        """
    ).fetchall()
    for row in rows:
        duration_seconds = duration_between_seconds(row["created_at"], row["completed_at"])
        if duration_seconds is not None:
            db.execute(
                "UPDATE prompt_runs SET duration_seconds = ? WHERE id = ?",
                (duration_seconds, row["id"]),
            )


def upsert_user(db: sqlite3.Connection, client_id: str, display_name: str) -> int:
    now = utc_now()
    db.execute(
        """
        INSERT INTO users (client_id, display_name, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(client_id) DO UPDATE SET
            display_name = excluded.display_name,
            updated_at = excluded.updated_at
        """,
        (client_id, display_name, now, now),
    )
    row = db.execute("SELECT id FROM users WHERE client_id = ?", (client_id,)).fetchone()
    return int(row["id"])


def get_album_row(db: sqlite3.Connection, album_id: int) -> sqlite3.Row:
    row = db.execute(
        """
        SELECT a.id, a.name, a.description, a.created_at, a.updated_at,
               COUNT(DISTINCT p.id) AS photo_count,
               COUNT(DISTINCT pr.id) AS prompt_run_count,
               best.id AS best_prompt_run_id,
               best.version AS best_prompt_version,
               best.name AS best_prompt_name
        FROM albums a
        LEFT JOIN photos p ON p.album_id = a.id
        LEFT JOIN prompt_runs pr ON pr.album_id = a.id
        LEFT JOIN prompt_runs best ON best.album_id = a.id AND best.is_best = 1
        WHERE a.id = ?
        GROUP BY a.id
        """,
        (album_id,),
    ).fetchone()
    if row is None:
        raise ValueError("album not found")
    return row


def get_prompt_run_row(db: sqlite3.Connection, run_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM prompt_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise ValueError("prompt run not found")
    return row


def normalize_prompt_text(prompt_text: str) -> str:
    return re.sub(r"\s+", " ", prompt_text.strip())


def count_active_prompt_runs(db: sqlite3.Connection) -> int:
    placeholders_sql = placeholders(list(ACTIVE_RUN_STATUSES))
    row = db.execute(
        f"SELECT COUNT(*) FROM prompt_runs WHERE status IN ({placeholders_sql})",
        list(ACTIVE_RUN_STATUSES),
    ).fetchone()
    return int(row[0])


def find_matching_prompt_run(
    db: sqlite3.Connection,
    album_id: int,
    normalized_prompt_text: str,
    model: str,
    statuses: tuple[str, ...],
) -> sqlite3.Row | None:
    placeholders_sql = placeholders(list(statuses))
    return db.execute(
        f"""
        SELECT *
        FROM prompt_runs
        WHERE album_id = ?
          AND normalized_prompt_text = ?
          AND model = ?
          AND status IN ({placeholders_sql})
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        [album_id, normalized_prompt_text, model, *statuses],
    ).fetchone()


def get_album_photos_by_client_ids(
    db: sqlite3.Connection,
    album_id: int,
    client_photo_ids: list[str],
) -> dict[str, sqlite3.Row]:
    if not client_photo_ids:
        return {}
    unique_ids = list(dict.fromkeys(client_photo_ids))
    rows = db.execute(
        f"""
        SELECT *
        FROM photos
        WHERE album_id = ?
          AND client_photo_id IN ({placeholders(unique_ids)})
        """,
        [album_id, *unique_ids],
    ).fetchall()
    return {str(row["client_photo_id"]): row for row in rows}


def prompt_run_has_results(db: sqlite3.Connection, prompt_run_id: int, photo_ids: list[int]) -> bool:
    if not photo_ids:
        return False
    row = db.execute(
        f"""
        SELECT COUNT(DISTINCT photo_id) AS result_count
        FROM analysis_results
        WHERE prompt_run_id = ?
          AND photo_id IN ({placeholders(photo_ids)})
        """,
        [prompt_run_id, *photo_ids],
    ).fetchone()
    return int(row["result_count"] or 0) == len(set(photo_ids))


def enqueue_analysis_jobs(
    db: sqlite3.Connection,
    prompt_run_id: int,
    photo_ids: list[int],
    *,
    replace_existing: bool = False,
) -> int:
    run = get_prompt_run_row(db, prompt_run_id)
    now = utc_now()
    inserted = 0
    for photo_id in dict.fromkeys(photo_ids):
        if replace_existing:
            existing = db.execute(
                "SELECT id FROM analysis_jobs WHERE prompt_run_id = ? AND photo_id = ?",
                (prompt_run_id, photo_id),
            ).fetchone()
            if existing:
                db.execute(
                    """
                    UPDATE analysis_jobs
                    SET status = ?, attempts = 0, last_error = '', scheduled_at = ?,
                        started_at = NULL, finished_at = NULL
                    WHERE id = ?
                    """,
                    (JOB_STATUS_QUEUED, now, existing["id"]),
                )
                inserted += 1
                continue
        cur = db.execute(
            """
            INSERT OR IGNORE INTO analysis_jobs
                (album_id, prompt_run_id, photo_id, status, scheduled_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run["album_id"], prompt_run_id, photo_id, JOB_STATUS_QUEUED, now, now),
        )
        inserted += int(cur.rowcount or 0)
    if inserted:
        db.execute(
            "UPDATE prompt_runs SET status = ?, completed_at = NULL, duration_seconds = NULL WHERE id = ? AND status != ?",
            (RUN_STATUS_QUEUED, prompt_run_id, RUN_STATUS_RUNNING),
        )
    return inserted


def refresh_prompt_run_status(db: sqlite3.Connection, prompt_run_id: int) -> str:
    current_run = get_prompt_run_row(db, prompt_run_id)
    counts = job_counts(db, prompt_run_id)
    if counts["running"] > 0:
        status = RUN_STATUS_RUNNING
    elif counts["queued"] > 0:
        status = RUN_STATUS_QUEUED
    elif counts["failed"] > 0:
        status = RUN_STATUS_FAILED
    elif counts["cancelled"] > 0:
        status = RUN_STATUS_CANCELLED
    elif counts["total"] > 0:
        status = RUN_STATUS_COMPLETED
    else:
        result_count = db.execute(
            "SELECT COUNT(*) FROM analysis_results WHERE prompt_run_id = ?",
            (prompt_run_id,),
        ).fetchone()[0]
        status = RUN_STATUS_COMPLETED if int(result_count) > 0 else RUN_STATUS_DRAFT
    if status in (RUN_STATUS_COMPLETED, RUN_STATUS_FAILED, RUN_STATUS_CANCELLED):
        completed_at = row_value(current_run, "completed_at") or utc_now()
        duration_seconds = row_value(current_run, "duration_seconds")
        if duration_seconds is None:
            duration_seconds = duration_between_seconds(row_value(current_run, "created_at"), completed_at)
    else:
        completed_at = None
        duration_seconds = None
    db.execute(
        "UPDATE prompt_runs SET status = ?, completed_at = ?, duration_seconds = ? WHERE id = ?",
        (status, completed_at, duration_seconds, prompt_run_id),
    )
    return status


def next_prompt_version(db: sqlite3.Connection, album_id: int, prompt_id: int) -> int:
    row = db.execute(
        """
        SELECT COALESCE(MAX(version), 0) + 1 AS version
        FROM prompt_runs
        WHERE album_id = ? AND prompt_id = ?
        """,
        (album_id, prompt_id),
    ).fetchone()
    return int(row["version"])


def next_prompt_library_version(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT COALESCE(MAX(version), 0) + 1 AS version FROM prompts").fetchone()
    return int(row["version"])


def get_or_create_prompt(
    db: sqlite3.Connection,
    *,
    prompt_text: str,
    model: str,
    name: str = "",
    prompt_id: int = 0,
) -> int:
    normalized_prompt_text = normalize_prompt_text(prompt_text)
    now = utc_now()
    if prompt_id:
        row = db.execute("SELECT * FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
        if row and str(row["model"]) == model and str(row["normalized_prompt_text"]) == normalized_prompt_text:
            if name and name != str(row["name"] or ""):
                db.execute("UPDATE prompts SET name = ?, updated_at = ? WHERE id = ?", (name, now, prompt_id))
            return int(row["id"])

    row = db.execute(
        "SELECT * FROM prompts WHERE normalized_prompt_text = ? AND model = ?",
        (normalized_prompt_text, model),
    ).fetchone()
    if row:
        if name and name != str(row["name"] or ""):
            db.execute("UPDATE prompts SET name = ?, updated_at = ? WHERE id = ?", (name, now, row["id"]))
        return int(row["id"])

    cur = db.execute(
        """
        INSERT INTO prompts (version, name, prompt_text, normalized_prompt_text, model, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (next_prompt_library_version(db), name, prompt_text, normalized_prompt_text, model, now, now),
    )
    return int(cur.lastrowid)


def insert_prompt_run(
    db: sqlite3.Connection,
    *,
    album_id: int,
    user_id: int,
    prompt_text: str,
    model: str,
    name: str = "",
    rating_note: str = "",
    status: str = RUN_STATUS_DRAFT,
    prompt_id: int = 0,
) -> tuple[int, int]:
    normalized_prompt_text = normalize_prompt_text(prompt_text)
    prompt_id = get_or_create_prompt(db, prompt_text=prompt_text, model=model, name=name, prompt_id=prompt_id)
    version = next_prompt_version(db, album_id, prompt_id)
    cur = db.execute(
        """
        INSERT INTO prompt_runs
            (prompt_id, album_id, user_id, version, name, prompt_text, normalized_prompt_text,
             model, status, rating_note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            prompt_id,
            album_id,
            user_id,
            version,
            name,
            prompt_text,
            normalized_prompt_text,
            model,
            status,
            rating_note,
            utc_now(),
        ),
    )
    return int(cur.lastrowid), version


def upsert_photo(
    db: sqlite3.Connection,
    *,
    album_id: int,
    user_id: int,
    client_photo_id: str,
    filename: str,
    image_hash: str,
    storage_path: str,
) -> int:
    existing = db.execute(
        "SELECT id FROM photos WHERE album_id = ? AND client_photo_id = ?",
        (album_id, client_photo_id),
    ).fetchone()
    if existing:
        db.execute(
            """
            UPDATE photos
            SET user_id = ?, original_filename = ?, storage_path = ?, image_sha256 = ?
            WHERE id = ?
            """,
            (user_id, filename, storage_path, image_hash, existing["id"]),
        )
        return int(existing["id"])
    cur = db.execute(
        """
        INSERT INTO photos
            (album_id, user_id, client_photo_id, original_filename, storage_path, image_sha256, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (album_id, user_id, client_photo_id, filename, storage_path, image_hash, utc_now()),
    )
    return int(cur.lastrowid)


def save_analysis_result(
    db: sqlite3.Connection,
    prompt_run_id: int,
    photo_id: int,
    parsed: dict[str, Any],
    raw_response: dict[str, Any],
) -> None:
    usage = extract_token_usage(raw_response)
    tags = parsed.get("tags") if isinstance(parsed.get("tags"), list) else []
    coordinates = parsed.get("coordinates") if isinstance(parsed.get("coordinates"), dict) else {}
    lat = coordinates.get("lat") if isinstance(coordinates.get("lat"), (int, float)) else None
    lng = coordinates.get("lng") if isinstance(coordinates.get("lng"), (int, float)) else None
    values = (
        json.dumps(parsed, ensure_ascii=False),
        json.dumps(raw_response, ensure_ascii=False),
        json.dumps(tags, ensure_ascii=False),
        str(parsed.get("description") or ""),
        str(parsed.get("location_guess") or ""),
        str(parsed.get("confidence") or ""),
        lat,
        lng,
        usage["input"],
        usage["output"],
        usage["total"],
        usage["thoughts"],
        json.dumps(usage["details"], ensure_ascii=False),
        utc_now(),
    )
    existing = db.execute(
        "SELECT id FROM analysis_results WHERE prompt_run_id = ? AND photo_id = ?",
        (prompt_run_id, photo_id),
    ).fetchone()
    if existing:
        db.execute(
            """
            UPDATE analysis_results
            SET result_json = ?, raw_response_json = ?, tags_json = ?, description = ?,
                location_guess = ?, confidence = ?, lat = ?, lng = ?,
                input_token_count = ?, output_token_count = ?, total_token_count = ?,
                thoughts_token_count = ?, token_details_json = ?, created_at = ?
            WHERE id = ?
            """,
            (*values, existing["id"]),
        )
        return
    db.execute(
        """
        INSERT INTO analysis_results (
            prompt_run_id, photo_id, result_json, raw_response_json, tags_json,
            description, location_guess, confidence, lat, lng, input_token_count,
            output_token_count, total_token_count, thoughts_token_count, token_details_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (prompt_run_id, photo_id, *values),
    )


def serialize_album(row: sqlite3.Row) -> dict[str, Any]:
    best_version = row["best_prompt_version"]
    best_name = row["best_prompt_name"] or ""
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"] or "",
        "photoCount": int(row["photo_count"] or 0),
        "promptRunCount": int(row["prompt_run_count"] or 0),
        "bestPromptRun": (
            {
                "id": row["best_prompt_run_id"],
                "version": best_version,
                "name": best_name,
                "label": f"v{best_version}" + (f" {best_name}" if best_name else ""),
            }
            if row["best_prompt_run_id"]
            else None
        ),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


def parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def duration_between_seconds(start_value: Any, end_value: Any) -> int | None:
    started = parse_iso_datetime(start_value)
    ended = parse_iso_datetime(end_value)
    if started is None or ended is None:
        return None
    return max(0, int(round((ended - started).total_seconds())))


def prompt_run_duration_seconds(run: sqlite3.Row) -> int | None:
    stored_duration = row_value(run, "duration_seconds")
    if stored_duration is not None:
        return int(stored_duration)
    started = parse_iso_datetime(row_value(run, "created_at"))
    if started is None:
        return None
    status = str(row_value(run, "status", "") or "")
    ended = parse_iso_datetime(row_value(run, "completed_at"))
    if ended is None and status in ACTIVE_RUN_STATUSES:
        ended = datetime.now(timezone.utc)
    if ended is None:
        return None
    return max(0, int(round((ended - started).total_seconds())))


def serialize_prompt(db: sqlite3.Connection, prompt: sqlite3.Row, album_filter: int = 0) -> dict[str, Any]:
    params: list[Any] = [prompt["id"]]
    album_clause = ""
    if album_filter:
        album_clause = "AND pr.album_id = ?"
        params.append(album_filter)
    runs = db.execute(
        f"""
        SELECT pr.id, pr.prompt_id, pr.version, pr.name, pr.prompt_text, pr.model,
               pr.rating, pr.rating_note, pr.is_best, pr.status, pr.completed_at,
               pr.duration_seconds, pr.created_at,
               p.version AS prompt_version, p.name AS prompt_name,
               a.id AS album_id, a.name AS album_name
        FROM prompt_runs pr
        JOIN albums a ON a.id = pr.album_id
        LEFT JOIN prompts p ON p.id = pr.prompt_id
        WHERE pr.prompt_id = ? {album_clause}
        ORDER BY pr.is_best DESC, pr.created_at DESC, pr.id DESC
        LIMIT 500
        """,
        params,
    ).fetchall()
    return {
        "id": prompt["id"],
        "version": prompt["version"],
        "name": prompt["name"] or "",
        "promptText": prompt["prompt_text"],
        "model": prompt["model"],
        "createdAt": prompt["created_at"],
        "updatedAt": prompt["updated_at"],
        "runs": [serialize_prompt_run(db, run) for run in runs],
    }


def serialize_prompt_run(db: sqlite3.Connection, run: sqlite3.Row) -> dict[str, Any]:
    results = db.execute(
        """
        SELECT p.client_photo_id, ar.tags_json, ar.description, ar.location_guess,
               ar.confidence, ar.lat, ar.lng, ar.result_json,
               ar.input_token_count, ar.output_token_count, ar.total_token_count,
               ar.thoughts_token_count, ar.token_details_json
        FROM analysis_results ar
        JOIN photos p ON p.id = ar.photo_id
        WHERE ar.prompt_run_id = ?
        ORDER BY p.created_at ASC, p.id ASC
        """,
        (run["id"],),
    ).fetchall()
    return {
        "id": run["id"],
        "promptId": row_value(run, "prompt_id"),
        "promptVersion": row_value(run, "prompt_version"),
        "promptName": row_value(run, "prompt_name", "") or "",
        "albumId": row_value(run, "album_id"),
        "albumName": row_value(run, "album_name", "") or "",
        "version": run["version"],
        "name": run["name"] or "",
        "promptText": run["prompt_text"],
        "model": run["model"],
        "status": run["status"],
        "timestamp": run["created_at"],
        "completedAt": row_value(run, "completed_at"),
        "durationSeconds": prompt_run_duration_seconds(run),
        "rating": run["rating"],
        "ratingNote": run["rating_note"] or "",
        "isBest": bool(run["is_best"]),
        "progress": job_counts(db, int(run["id"])),
        "tokenUsage": sum_token_usage(db, int(run["id"])),
        "results": [serialize_result(row) for row in results],
    }


def serialize_result(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["client_photo_id"],
        "tags": json.loads(row["tags_json"] or "[]"),
        "description": row["description"] or "",
        "location": row["location_guess"] or "",
        "confidence": row["confidence"] or "",
        "lat": row["lat"],
        "lng": row["lng"],
        "tokenUsage": {
            "input": int(row["input_token_count"] or 0),
            "output": int(row["output_token_count"] or 0),
            "total": int(row["total_token_count"] or 0),
            "thoughts": int(row["thoughts_token_count"] or 0),
            "details": json.loads(row["token_details_json"] or "{}"),
        },
        "result": json.loads(row["result_json"] or "{}"),
    }


def serialize_prompt_run_status(db: sqlite3.Connection, run: sqlite3.Row) -> dict[str, Any]:
    results = db.execute(
        """
        SELECT p.client_photo_id, ar.tags_json, ar.description, ar.location_guess,
               ar.confidence, ar.lat, ar.lng, ar.result_json,
               ar.input_token_count, ar.output_token_count, ar.total_token_count,
               ar.thoughts_token_count, ar.token_details_json
        FROM analysis_results ar
        JOIN photos p ON p.id = ar.photo_id
        WHERE ar.prompt_run_id = ?
        ORDER BY p.created_at ASC, p.id ASC
        """,
        (run["id"],),
    ).fetchall()
    jobs = db.execute(
        """
        SELECT p.client_photo_id, j.status, j.attempts, j.last_error,
               j.scheduled_at, j.started_at, j.finished_at
        FROM analysis_jobs j
        JOIN photos p ON p.id = j.photo_id
        WHERE j.prompt_run_id = ?
        ORDER BY j.created_at ASC, j.id ASC
        """,
        (run["id"],),
    ).fetchall()
    return {
        "id": run["id"],
        "promptId": row_value(run, "prompt_id"),
        "promptVersion": row_value(run, "prompt_version"),
        "promptName": row_value(run, "prompt_name", "") or "",
        "version": run["version"],
        "name": run["name"] or "",
        "promptText": run["prompt_text"],
        "model": run["model"],
        "status": run["status"],
        "timestamp": run["created_at"],
        "completedAt": row_value(run, "completed_at"),
        "durationSeconds": prompt_run_duration_seconds(run),
        "progress": job_counts(db, int(run["id"])),
        "tokenUsage": sum_token_usage(db, int(run["id"])),
        "jobs": [serialize_job(row) for row in jobs],
        "results": [serialize_result(row) for row in results],
    }


def serialize_job(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["client_photo_id"],
        "status": row["status"],
        "attempts": int(row["attempts"] or 0),
        "lastError": row["last_error"] or "",
        "scheduledAt": row["scheduled_at"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
    }


def job_counts(db: sqlite3.Connection, prompt_run_id: int) -> dict[str, int]:
    rows = db.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM analysis_jobs
        WHERE prompt_run_id = ?
        GROUP BY status
        """,
        (prompt_run_id,),
    ).fetchall()
    counts = {row["status"]: int(row["count"]) for row in rows}
    total = sum(counts.values())
    completed = counts.get(JOB_STATUS_COMPLETED, 0)
    failed = counts.get(JOB_STATUS_FAILED, 0)
    cancelled = counts.get(JOB_STATUS_CANCELLED, 0)
    done = completed + failed + cancelled
    return {
        "total": total,
        "queued": counts.get(JOB_STATUS_QUEUED, 0),
        "running": counts.get(JOB_STATUS_RUNNING, 0),
        "completed": completed,
        "failed": failed,
        "cancelled": cancelled,
        "done": done,
    }


def sum_token_usage(db: sqlite3.Connection, prompt_run_id: int) -> dict[str, int]:
    row = db.execute(
        """
        SELECT COALESCE(SUM(input_token_count), 0) AS input_tokens,
               COALESCE(SUM(output_token_count), 0) AS output_tokens,
               COALESCE(SUM(total_token_count), 0) AS total_tokens,
               COALESCE(SUM(thoughts_token_count), 0) AS thoughts_tokens
        FROM analysis_results
        WHERE prompt_run_id = ?
        """,
        (prompt_run_id,),
    ).fetchone()
    return {
        "input": int(row["input_tokens"] or 0),
        "output": int(row["output_tokens"] or 0),
        "total": int(row["total_tokens"] or 0),
        "thoughts": int(row["thoughts_tokens"] or 0),
    }


def serialize_photo(row: sqlite3.Row) -> dict[str, Any]:
    storage_path = str(row["storage_path"] or "").replace("\\", "/")
    if storage_path.startswith("uploads/"):
        storage_path = storage_path[len("uploads/") :]
    tags = json.loads(row["tags_json"] or "[]") if row["tags_json"] else []
    return {
        "id": row["client_photo_id"],
        "photoId": row["id"],
        "filename": row["original_filename"],
        "imageUrl": f"/uploads/{storage_path}",
        "status": "done" if row["description"] else "pending",
        "tags": tags,
        "description": row["description"] or "",
        "location": row["location_guess"] or "",
        "confidence": row["confidence"] or "",
        "lat": row["lat"],
        "lng": row["lng"],
        "createdAt": row["created_at"],
    }


def remove_unreferenced_files(db: sqlite3.Connection, upload_dir: Path, storage_paths: list[str]) -> None:
    for raw_path in storage_paths:
        storage_path = str(raw_path or "").replace("\\", "/")
        if storage_path.startswith("uploads/"):
            storage_path = storage_path[len("uploads/") :]
        still_used = db.execute("SELECT 1 FROM photos WHERE storage_path = ? LIMIT 1", (storage_path,)).fetchone()
        if still_used:
            continue
        target = (upload_dir / storage_path).resolve()
        try:
            target.relative_to(upload_dir.resolve())
        except ValueError:
            continue
        if target.exists() and target.is_file():
            target.unlink()


def placeholders(values: list[Any]) -> str:
    return ",".join("?" for _ in values)


def require_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value.strip()


def sanitize_filename(filename: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")
    return stem or "uploaded.jpg"


def decode_image(image_base64: str) -> bytes:
    clean = image_base64.split(",", 1)[1] if "," in image_base64[:80] else image_base64
    return base64.b64decode(clean, validate=False)


def extract_json_from_text(text: str) -> dict[str, Any]:
    clean = text.strip()
    clean = re.sub(r"^```json", "", clean, flags=re.IGNORECASE).strip()
    clean = re.sub(r"^```", "", clean).strip()
    clean = re.sub(r"```$", "", clean).strip()
    match = re.search(r"\{[\s\S]*\}", clean)
    if match:
        clean = match.group(0)
    return json.loads(clean)


def extract_token_usage(raw_response: dict[str, Any]) -> dict[str, Any]:
    usage = raw_response.get("usageMetadata") if isinstance(raw_response, dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    return {
        "input": int(usage.get("promptTokenCount") or 0),
        "output": int(usage.get("candidatesTokenCount") or 0),
        "total": int(usage.get("totalTokenCount") or 0),
        "thoughts": int(usage.get("thoughtsTokenCount") or 0),
        "details": {
            "prompt": usage.get("promptTokensDetails") or [],
            "candidates": usage.get("candidatesTokensDetails") or [],
        },
    }


class GeminiThrottle:
    def __init__(self, min_seconds_between_requests: float):
        self.min_seconds_between_requests = min_seconds_between_requests
        self._lock = threading.Lock()
        self._next_request_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait_seconds = max(0.0, self._next_request_at - now)
            if wait_seconds:
                time.sleep(wait_seconds)
            self._next_request_at = time.monotonic() + self.min_seconds_between_requests


class AnalysisWorker:
    def __init__(
        self,
        *,
        db_path: Path,
        upload_dir: Path,
        max_workers: int,
        min_seconds_between_requests: float,
        max_retries: int,
        retry_base_seconds: int,
        retry_max_seconds: int,
        poll_seconds: float,
    ):
        self.db_path = db_path
        self.upload_dir = upload_dir
        self.max_workers = max_workers
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.poll_seconds = poll_seconds
        self.throttle = GeminiThrottle(min_seconds_between_requests)
        self._started = False
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for idx in range(self.max_workers):
            thread = threading.Thread(target=self._run_loop, name=f"analysis-worker-{idx + 1}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def _run_loop(self) -> None:
        while True:
            try:
                processed = self.process_one()
                if not processed:
                    time.sleep(self.poll_seconds)
            except Exception:
                time.sleep(self.poll_seconds)

    def process_one(self) -> bool:
        job = self._claim_next_job()
        if job is None:
            return False
        try:
            image_base64 = read_photo_base64(self.upload_dir, job["storage_path"])
            self.throttle.wait()
            parsed, raw_response = call_gemini_image(job["model"], job["prompt_text"], image_base64)
            with connect(self.db_path) as db:
                save_analysis_result(db, int(job["prompt_run_id"]), int(job["photo_id"]), parsed, raw_response)
                db.execute(
                    """
                    UPDATE analysis_jobs
                    SET status = ?, last_error = '', finished_at = ?
                    WHERE id = ?
                    """,
                    (JOB_STATUS_COMPLETED, utc_now(), int(job["id"])),
                )
                refresh_prompt_run_status(db, int(job["prompt_run_id"]))
                db.commit()
        except Exception as exc:
            self._mark_job_error(job, exc)
        return True

    def _claim_next_job(self) -> dict[str, Any] | None:
        now = utc_now()
        with connect(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute(
                """
                SELECT pr.id
                FROM prompt_runs pr
                WHERE EXISTS (
                    SELECT 1
                    FROM analysis_jobs j
                    WHERE j.prompt_run_id = pr.id
                      AND j.status = ?
                      AND j.scheduled_at <= ?
                )
                ORDER BY COALESCE(pr.last_job_started_at, ''), pr.created_at, pr.id
                LIMIT 1
                """,
                (JOB_STATUS_QUEUED, now),
            ).fetchone()
            if run is None:
                db.commit()
                return None
            row = db.execute(
                """
                SELECT j.id, j.prompt_run_id, j.photo_id, j.attempts,
                       p.storage_path, pr.prompt_text, pr.model
                FROM analysis_jobs j
                JOIN photos p ON p.id = j.photo_id
                JOIN prompt_runs pr ON pr.id = j.prompt_run_id
                WHERE j.prompt_run_id = ?
                  AND j.status = ?
                  AND j.scheduled_at <= ?
                ORDER BY j.created_at, j.id
                LIMIT 1
                """,
                (run["id"], JOB_STATUS_QUEUED, now),
            ).fetchone()
            if row is None:
                db.commit()
                return None
            started_at = utc_now()
            db.execute(
                """
                UPDATE analysis_jobs
                SET status = ?, attempts = attempts + 1, started_at = ?
                WHERE id = ?
                """,
                (JOB_STATUS_RUNNING, started_at, row["id"]),
            )
            db.execute(
                "UPDATE prompt_runs SET status = ?, last_job_started_at = ? WHERE id = ?",
                (RUN_STATUS_RUNNING, started_at, row["prompt_run_id"]),
            )
            db.commit()
            return dict(row)

    def _mark_job_error(self, job: dict[str, Any], error: Exception) -> None:
        attempts = int(job["attempts"] or 0) + 1
        message = str(error)[:2000]
        with connect(self.db_path) as db:
            if attempts > self.max_retries:
                db.execute(
                    """
                    UPDATE analysis_jobs
                    SET status = ?, attempts = ?, last_error = ?, finished_at = ?
                    WHERE id = ?
                    """,
                    (JOB_STATUS_FAILED, attempts, message, utc_now(), int(job["id"])),
                )
            else:
                delay = min(self.retry_max_seconds, self.retry_base_seconds * (2 ** (attempts - 1)))
                scheduled_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                db.execute(
                    """
                    UPDATE analysis_jobs
                    SET status = ?, attempts = ?, last_error = ?, scheduled_at = ?, started_at = NULL
                    WHERE id = ?
                    """,
                    (JOB_STATUS_QUEUED, attempts, message, scheduled_at, int(job["id"])),
                )
            refresh_prompt_run_status(db, int(job["prompt_run_id"]))
            db.commit()


def read_photo_base64(upload_dir: Path, storage_path: str) -> str:
    normalized = str(storage_path or "").replace("\\", "/")
    if normalized.startswith("uploads/"):
        normalized = normalized[len("uploads/") :]
    target = (upload_dir / normalized).resolve()
    target.relative_to(upload_dir.resolve())
    return base64.b64encode(target.read_bytes()).decode("ascii")


def call_gemini_image(model: str, prompt_text: str, image_base64: str) -> tuple[dict[str, Any], dict[str, Any]]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    resp = requests.post(
        url,
        params={"key": api_key},
        headers={"Content-Type": "application/json"},
        json={
            "contents": [
                {
                    "parts": [
                        {"text": prompt_text},
                        {"inline_data": {"mime_type": "image/jpeg", "data": image_base64}},
                    ]
                }
            ]
        },
        timeout=90,
    )
    data = resp.json()
    if resp.status_code >= 400 or data.get("error"):
        message = data.get("error", {}).get("message") or f"Gemini HTTP {resp.status_code}"
        raise RuntimeError(message)
    text = gemini_text(data)
    return extract_json_from_text(text), data


def call_gemini_text(model: str, prompt_text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    resp = requests.post(
        url,
        params={"key": api_key},
        headers={"Content-Type": "application/json"},
        json={"contents": [{"parts": [{"text": prompt_text}]}]},
        timeout=60,
    )
    data = resp.json()
    if resp.status_code >= 400 or data.get("error"):
        message = data.get("error", {}).get("message") or f"Gemini HTTP {resp.status_code}"
        raise RuntimeError(message)
    text = gemini_text(data)
    return extract_json_from_text(text), data


def gemini_text(data: dict[str, Any]) -> str:
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Gemini response did not include text") from exc
