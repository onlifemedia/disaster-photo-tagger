import base64
import os
import shutil
import sqlite3
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


class AlbumFlowTest(unittest.TestCase):
    def setUp(self):
        tmp_root = Path(__file__).resolve().parent / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        root = tmp_root / uuid.uuid4().hex
        root.mkdir()
        (root / "uploads").mkdir()
        self.root = root
        os.environ["DATABASE_PATH"] = str(root / "app.db")
        os.environ["UPLOAD_DIR"] = str(root / "uploads")
        os.environ["GEMINI_API_KEY"] = "test-key"
        os.environ["QUEUE_WORKER_ENABLED"] = "0"
        os.environ["MAX_UPLOAD_MB"] = "5"
        os.environ["MAX_PHOTOS_PER_ALBUM"] = "50"
        os.environ["MAX_ACTIVE_PROMPT_RUNS"] = "5"
        os.environ["GEMINI_MIN_SECONDS_BETWEEN_REQUESTS"] = "0"

        from app.main import create_app

        self.app = create_app()
        self.client = self.app.test_client()
        self.db_path = root / "app.db"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_album_stats_and_prompt_runs_are_album_scoped(self):
        album_a = self._create_album("A")
        album_b = self._create_album("B")
        self._create_photo(album_a, "p1")
        run_a = self._create_prompt_run(album_a, "prompt A")
        run_b = self._create_prompt_run(album_b, "prompt B")

        self.client.patch(f"/api/prompt-runs/{run_a}", json={"is_best": True, "name": "best A"})
        self.client.patch(f"/api/prompt-runs/{run_b}", json={"is_best": True, "name": "best B"})

        runs_a = self.client.get(f"/api/albums/{album_a}/prompt-runs").get_json()
        runs_b = self.client.get(f"/api/albums/{album_b}/prompt-runs").get_json()
        self.assertEqual([run["id"] for run in runs_a], [run_a])
        self.assertEqual([run["id"] for run in runs_b], [run_b])
        self.assertTrue(runs_a[0]["isBest"])
        self.assertTrue(runs_b[0]["isBest"])

        albums = self.client.get("/api/albums").get_json()
        by_id = {album["id"]: album for album in albums}
        self.assertEqual(by_id[album_a]["photoCount"], 1)
        self.assertEqual(by_id[album_a]["promptRunCount"], 1)
        self.assertEqual(by_id[album_a]["bestPromptRun"]["id"], run_a)
        self.assertEqual(by_id[album_b]["bestPromptRun"]["id"], run_b)

    def test_analyze_overwrites_same_prompt_run_photo_result(self):
        album_id = self._create_album("Analyze")
        run_id = self._create_prompt_run(album_id, "prompt")

        first = ({"tags": ["淹水"], "description": "第一次", "confidence": "low"}, {"raw": 1})
        second = ({"tags": ["火災"], "description": "第二次", "confidence": "high"}, {"raw": 2})
        with patch("app.main.call_gemini_image", side_effect=[first, second]):
            self._analyze(album_id, run_id, "p1")
            self._analyze(album_id, run_id, "p1")

        runs = self.client.get(f"/api/albums/{album_id}/prompt-runs").get_json()
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(runs[0]["results"]), 1)
        self.assertEqual(runs[0]["results"][0]["description"], "第二次")
        self.assertEqual(runs[0]["results"][0]["tags"], ["火災"])

        with closing(sqlite3.connect(self.db_path)) as db:
            count = db.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0]
        self.assertEqual(count, 1)

    def test_prompt_run_versions_start_at_one_per_prompt_in_each_album(self):
        album_a = self._create_album("Version A")
        album_b = self._create_album("Version B")

        version_a1 = self._create_prompt_run(album_a, "prompt A1", return_version=True)
        version_a2 = self._create_prompt_run(album_a, "prompt A2", return_version=True)
        version_b1 = self._create_prompt_run(album_b, "prompt B1", return_version=True)

        self.assertEqual(version_a1, 1)
        self.assertEqual(version_a2, 1)
        self.assertEqual(version_b1, 1)

    def test_delete_album_removes_album_data_only(self):
        album_a = self._create_album("Delete A")
        album_b = self._create_album("Keep B")
        self._create_photo(album_a, "a1")
        self._create_photo(album_b, "b1")
        self._create_prompt_run(album_a, "prompt A")
        self._create_prompt_run(album_b, "prompt B")

        resp = self.client.delete(f"/api/albums/{album_a}")
        self.assertEqual(resp.status_code, 200)

        albums = self.client.get("/api/albums").get_json()
        album_ids = {album["id"] for album in albums}
        self.assertNotIn(album_a, album_ids)
        self.assertIn(album_b, album_ids)
        self.assertEqual(len(self.client.get(f"/api/albums/{album_b}/photos").get_json()), 1)
        self.assertEqual(len(self.client.get(f"/api/albums/{album_b}/prompt-runs").get_json()), 1)

    def test_queue_reuses_active_prompt_run_for_same_prompt(self):
        album_id = self._create_album("Queue Same Prompt")
        self._create_photo(album_id, "p1")
        self._create_photo(album_id, "p2")

        first = self._queue_analysis(album_id, ["p1", "p2"], "請辨識災害類型")
        second = self._queue_analysis(album_id, ["p1", "p2"], "  請辨識災害類型  ")

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["reusedReason"], "active")
        with closing(sqlite3.connect(self.db_path)) as db:
            run_count = db.execute("SELECT COUNT(*) FROM prompt_runs WHERE album_id = ?", (album_id,)).fetchone()[0]
            job_count = db.execute("SELECT COUNT(*) FROM analysis_jobs WHERE prompt_run_id = ?", (first["id"],)).fetchone()[0]
        self.assertEqual(run_count, 1)
        self.assertEqual(job_count, 2)

    def test_queue_creates_distinct_runs_for_different_prompts(self):
        album_id = self._create_album("Queue Different Prompt")
        self._create_photo(album_id, "p1")
        self._create_photo(album_id, "p2")

        first = self._queue_analysis(album_id, ["p1", "p2"], "prompt A")
        second = self._queue_analysis(album_id, ["p1", "p2"], "prompt B")

        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 1)
        with closing(sqlite3.connect(self.db_path)) as db:
            run_count = db.execute("SELECT COUNT(*) FROM prompt_runs WHERE album_id = ?", (album_id,)).fetchone()[0]
            job_count = db.execute("SELECT COUNT(*) FROM analysis_jobs").fetchone()[0]
        self.assertEqual(run_count, 2)
        self.assertEqual(job_count, 4)

    def test_same_prompt_is_grouped_across_albums(self):
        album_a = self._create_album("Album A")
        album_b = self._create_album("Album B")
        self._create_photo(album_a, "a1")
        self._create_photo(album_b, "b1")

        run_a = self._queue_analysis(album_a, ["a1"], "shared prompt", name="Shared")
        run_b = self._queue_analysis(album_b, ["b1"], "shared prompt", name="Shared", prompt_id=run_a["promptId"])

        self.assertEqual(run_a["promptId"], run_b["promptId"])
        prompts = self.client.get("/api/prompts").get_json()
        shared = [prompt for prompt in prompts if prompt["id"] == run_a["promptId"]]

        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["name"], "Shared")
        self.assertEqual({run["albumId"] for run in shared[0]["runs"]}, {album_a, album_b})

    def test_delete_prompt_removes_runs_jobs_and_results_but_keeps_photos(self):
        album_a = self._create_album("Delete Prompt A")
        album_b = self._create_album("Delete Prompt B")
        self._create_photo(album_a, "a1")
        self._create_photo(album_b, "b1")

        run_a = self._queue_analysis(album_a, ["a1"], "shared prompt", name="Delete Me")
        run_b = self._queue_analysis(album_b, ["b1"], "shared prompt", name="Delete Me", prompt_id=run_a["promptId"])
        raw_response = {"usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2}}
        parsed = {"tags": ["淹水"], "description": "測試結果", "confidence": "high"}
        with patch("app.main.call_gemini_image", return_value=(parsed, raw_response)):
            self.assertTrue(self.app.config["ANALYSIS_WORKER"].process_one())
            self.assertTrue(self.app.config["ANALYSIS_WORKER"].process_one())

        resp = self.client.delete(f"/api/prompts/{run_a['promptId']}")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["deletedRunCount"], 2)

        prompts = self.client.get("/api/prompts").get_json()
        self.assertNotIn(run_a["promptId"], {prompt["id"] for prompt in prompts})
        self.assertEqual(len(self.client.get(f"/api/albums/{album_a}/photos").get_json()), 1)
        self.assertEqual(len(self.client.get(f"/api/albums/{album_b}/photos").get_json()), 1)
        with closing(sqlite3.connect(self.db_path)) as db:
            prompt_count = db.execute("SELECT COUNT(*) FROM prompts WHERE id = ?", (run_a["promptId"],)).fetchone()[0]
            run_count = db.execute(
                "SELECT COUNT(*) FROM prompt_runs WHERE id IN (?, ?)",
                (run_a["id"], run_b["id"]),
            ).fetchone()[0]
            job_count = db.execute(
                "SELECT COUNT(*) FROM analysis_jobs WHERE prompt_run_id IN (?, ?)",
                (run_a["id"], run_b["id"]),
            ).fetchone()[0]
            result_count = db.execute(
                "SELECT COUNT(*) FROM analysis_results WHERE prompt_run_id IN (?, ?)",
                (run_a["id"], run_b["id"]),
            ).fetchone()[0]
        self.assertEqual(prompt_count, 0)
        self.assertEqual(run_count, 0)
        self.assertEqual(job_count, 0)
        self.assertEqual(result_count, 0)

    def test_worker_records_token_usage_and_completed_status(self):
        album_id = self._create_album("Queue Tokens")
        self._create_photo(album_id, "p1")
        queued = self._queue_analysis(album_id, ["p1"], "prompt")
        raw_response = {
            "usageMetadata": {
                "promptTokenCount": 123,
                "candidatesTokenCount": 45,
                "totalTokenCount": 168,
                "promptTokensDetails": [{"modality": "IMAGE", "tokenCount": 100}],
            }
        }
        parsed = {"tags": ["淹水"], "description": "完成", "confidence": "high"}

        with patch("app.main.call_gemini_image", return_value=(parsed, raw_response)):
            processed = self.app.config["ANALYSIS_WORKER"].process_one()

        self.assertTrue(processed)
        status = self.client.get(f"/api/prompt-runs/{queued['id']}/status").get_json()
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["progress"]["completed"], 1)
        self.assertEqual(status["tokenUsage"]["input"], 123)
        self.assertEqual(status["tokenUsage"]["output"], 45)
        self.assertEqual(status["tokenUsage"]["total"], 168)
        self.assertEqual(status["results"][0]["tokenUsage"]["input"], 123)
        self.assertIsNotNone(status["completedAt"])
        self.assertIsInstance(status["durationSeconds"], int)

        reused = self._queue_analysis(album_id, ["p1"], "prompt")
        self.assertEqual(reused["id"], queued["id"])
        self.assertEqual(reused["reusedReason"], "completed")

        runs = self.client.get(f"/api/albums/{album_id}/prompt-runs").get_json()
        self.assertIsInstance(runs[0]["durationSeconds"], int)
        with closing(sqlite3.connect(self.db_path)) as db:
            stored_duration = db.execute(
                "SELECT duration_seconds FROM prompt_runs WHERE id = ?",
                (queued["id"],),
            ).fetchone()[0]
        self.assertIsInstance(stored_duration, int)

    def test_force_rerun_creates_new_prompt_run_for_completed_prompt(self):
        album_id = self._create_album("Force Rerun")
        self._create_photo(album_id, "p1")
        queued = self._queue_analysis(album_id, ["p1"], "prompt")
        raw_response = {"usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2}}
        parsed = {"tags": ["淹水"], "description": "完成", "confidence": "high"}

        with patch("app.main.call_gemini_image", return_value=(parsed, raw_response)):
            self.assertTrue(self.app.config["ANALYSIS_WORKER"].process_one())

        rerun = self._queue_analysis(album_id, ["p1"], "prompt", force_rerun=True)

        self.assertNotEqual(rerun["id"], queued["id"])
        self.assertFalse(rerun["reused"])
        self.assertEqual(rerun["version"], 2)
        self.assertEqual(rerun["status"], "queued")

    def test_edited_prompt_starts_run_version_at_one_after_prior_prompt_reruns(self):
        album_id = self._create_album("Edited Prompt Version")
        self._create_photo(album_id, "p1")
        raw_response = {"usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2}}

        first = self._queue_analysis(album_id, ["p1"], "original prompt")
        with patch("app.main.call_gemini_image", return_value=({"tags": ["�T��"], "description": "v1", "confidence": "high"}, raw_response)):
            self.assertTrue(self.app.config["ANALYSIS_WORKER"].process_one())

        second = self._queue_analysis(album_id, ["p1"], "original prompt", force_rerun=True)
        with patch("app.main.call_gemini_image", return_value=({"tags": ["���a"], "description": "v2", "confidence": "high"}, raw_response)):
            self.assertTrue(self.app.config["ANALYSIS_WORKER"].process_one())

        edited = self._queue_analysis(album_id, ["p1"], "edited prompt")

        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertEqual(edited["version"], 1)
        self.assertNotEqual(edited["promptId"], first["promptId"])

    def test_legacy_global_prompt_run_version_unique_is_migrated(self):
        legacy_db = self.root / "legacy-global-version.db"
        with closing(sqlite3.connect(legacy_db)) as db:
            db.executescript(
                """
                CREATE TABLE albums (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE prompts (
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
                CREATE TABLE prompt_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prompt_id INTEGER,
                    album_id INTEGER NOT NULL,
                    user_id INTEGER,
                    version INTEGER NOT NULL UNIQUE,
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
                    created_at TEXT NOT NULL
                );
                INSERT INTO albums (id, name, description, created_at, updated_at)
                VALUES (1, 'Legacy', '', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
                INSERT INTO prompts (id, version, name, prompt_text, normalized_prompt_text, model, created_at, updated_at)
                VALUES
                    (1, 1, '', 'prompt 1', 'prompt 1', 'gemini-2.5-flash', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
                    (2, 2, '', 'prompt 2', 'prompt 2', 'gemini-2.5-flash', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
                INSERT INTO prompt_runs
                    (id, prompt_id, album_id, version, name, prompt_text, normalized_prompt_text,
                     model, status, rating_note, created_at)
                VALUES
                    (1, 1, 1, 1, '', 'prompt 1', 'prompt 1', 'gemini-2.5-flash', 'completed', '', '2026-01-01T00:00:00+00:00'),
                    (2, 1, 1, 2, '', 'prompt 1', 'prompt 1', 'gemini-2.5-flash', 'completed', '', '2026-01-01T00:01:00+00:00'),
                    (3, 2, 1, 3, '', 'prompt 2', 'prompt 2', 'gemini-2.5-flash', 'completed', '', '2026-01-01T00:02:00+00:00');
                UPDATE prompt_runs
                SET completed_at = '2026-01-01T00:03:00+00:00'
                WHERE id = 3;
                """
            )

        from app.main import init_db

        init_db(legacy_db)

        with closing(sqlite3.connect(legacy_db)) as db:
            versions = db.execute("SELECT prompt_id, version FROM prompt_runs ORDER BY id").fetchall()
            duration_seconds = db.execute(
                "SELECT duration_seconds FROM prompt_runs WHERE id = 3"
            ).fetchone()[0]
            unique_indexes = []
            for row in db.execute("PRAGMA index_list(prompt_runs)").fetchall():
                if row[2]:
                    unique_indexes.append(
                        tuple(col[2] for col in db.execute(f"PRAGMA index_info({row[1]})").fetchall())
                    )

        self.assertEqual(versions, [(1, 1), (1, 2), (2, 1)])
        self.assertEqual(duration_seconds, 60)
        self.assertIn(("album_id", "prompt_id", "version"), unique_indexes)
        self.assertNotIn(("version",), unique_indexes)

    def test_preferred_completed_prompt_run_is_reused_before_latest_match(self):
        album_id = self._create_album("Preferred Completed")
        self._create_photo(album_id, "p1")
        raw_response = {"usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2}}

        first = self._queue_analysis(album_id, ["p1"], "same prompt")
        with patch(
            "app.main.call_gemini_image",
            return_value=({"tags": ["淹水"], "description": "v1", "confidence": "high"}, raw_response),
        ):
            self.assertTrue(self.app.config["ANALYSIS_WORKER"].process_one())

        second = self._queue_analysis(album_id, ["p1"], "same prompt", force_rerun=True)
        with patch(
            "app.main.call_gemini_image",
            return_value=({"tags": ["火災"], "description": "v2", "confidence": "high"}, raw_response),
        ):
            self.assertTrue(self.app.config["ANALYSIS_WORKER"].process_one())

        reused = self._queue_analysis(album_id, ["p1"], "same prompt", preferred_run_id=first["id"])

        self.assertEqual(second["version"], 2)
        self.assertEqual(reused["id"], first["id"])
        self.assertEqual(reused["reusedReason"], "completed")
        self.assertEqual(reused["results"][0]["description"], "v1")

    def test_album_photos_default_to_latest_run_unless_prompt_run_is_selected(self):
        album_id = self._create_album("Latest Default")
        self._create_photo(album_id, "p1")
        raw_response = {"usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2}}

        first = self._queue_analysis(album_id, ["p1"], "prompt v1")
        with patch(
            "app.main.call_gemini_image",
            return_value=({"tags": ["�T��"], "description": "v1 result", "confidence": "high"}, raw_response),
        ):
            self.assertTrue(self.app.config["ANALYSIS_WORKER"].process_one())

        self.client.patch(f"/api/prompt-runs/{first['id']}", json={"is_best": True})

        second = self._queue_analysis(album_id, ["p1"], "prompt v2")
        with patch(
            "app.main.call_gemini_image",
            return_value=({"tags": ["���a"], "description": "v2 result", "confidence": "high"}, raw_response),
        ):
            self.assertTrue(self.app.config["ANALYSIS_WORKER"].process_one())

        default_photos = self.client.get(f"/api/albums/{album_id}/photos").get_json()
        selected_first_photos = self.client.get(
            f"/api/albums/{album_id}/photos?prompt_run_id={first['id']}"
        ).get_json()

        self.assertEqual(default_photos[0]["description"], "v2 result")
        self.assertEqual(selected_first_photos[0]["description"], "v1 result")

    def test_replace_existing_job_overwrites_result_in_same_prompt_run(self):
        album_id = self._create_album("Replace One Photo")
        self._create_photo(album_id, "p1")
        raw_response = {"usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2}}

        queued = self._queue_analysis(album_id, ["p1"], "prompt")
        with patch(
            "app.main.call_gemini_image",
            return_value=({"tags": ["淹水"], "description": "第一次", "confidence": "high"}, raw_response),
        ):
            self.assertTrue(self.app.config["ANALYSIS_WORKER"].process_one())

        replaced = self._queue_analysis(
            album_id,
            ["p1"],
            "prompt",
            preferred_run_id=queued["id"],
            replace_existing_jobs=True,
        )
        self.assertEqual(replaced["id"], queued["id"])
        self.assertEqual(replaced["reusedReason"], "replace")
        self.assertEqual(replaced["status"], "queued")

        with patch(
            "app.main.call_gemini_image",
            return_value=({"tags": ["火災"], "description": "第二次", "confidence": "high"}, raw_response),
        ):
            self.assertTrue(self.app.config["ANALYSIS_WORKER"].process_one())

        status = self.client.get(f"/api/prompt-runs/{queued['id']}/status").get_json()
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["results"][0]["description"], "第二次")
        with closing(sqlite3.connect(self.db_path)) as db:
            run_count = db.execute("SELECT COUNT(*) FROM prompt_runs WHERE album_id = ?", (album_id,)).fetchone()[0]
            result_count = db.execute("SELECT COUNT(*) FROM analysis_results WHERE prompt_run_id = ?", (queued["id"],)).fetchone()[0]
        self.assertEqual(run_count, 1)
        self.assertEqual(result_count, 1)

    def test_cancel_prompt_run_cancels_queued_jobs_without_deleting_photos(self):
        album_id = self._create_album("Cancel Run")
        self._create_photo(album_id, "p1")
        self._create_photo(album_id, "p2")
        queued = self._queue_analysis(album_id, ["p1", "p2"], "prompt")

        resp = self.client.post(f"/api/prompt-runs/{queued['id']}/cancel")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        self.assertEqual(data["status"], "cancelled")
        self.assertEqual(data["progress"]["cancelled"], 2)
        self.assertFalse(self.app.config["ANALYSIS_WORKER"].process_one())

        self.assertEqual(len(self.client.get(f"/api/albums/{album_id}/photos").get_json()), 2)
        with closing(sqlite3.connect(self.db_path)) as db:
            result_count = db.execute(
                "SELECT COUNT(*) FROM analysis_results WHERE prompt_run_id = ?",
                (queued["id"],),
            ).fetchone()[0]
        self.assertEqual(result_count, 0)

    def _create_album(self, name):
        resp = self.client.post("/api/albums", json={"name": name, "description": ""})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()["id"]

    def _create_photo(self, album_id, photo_id):
        resp = self.client.post(
            f"/api/albums/{album_id}/photos",
            json={
                "client_photo_id": photo_id,
                "filename": f"{photo_id}.jpg",
                "image_base64": self._image_base64(),
            },
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()["id"]

    def _create_prompt_run(self, album_id, prompt, return_version=False):
        resp = self.client.post(
            f"/api/albums/{album_id}/prompt-runs",
            json={"prompt_text": prompt, "model": "gemini-2.5-flash"},
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        data = resp.get_json()
        return data["version"] if return_version else data["id"]

    def _analyze(self, album_id, run_id, photo_id):
        resp = self.client.post(
            f"/api/albums/{album_id}/analyze",
            json={
                "run_id": run_id,
                "client_photo_id": photo_id,
                "filename": f"{photo_id}.jpg",
                "image_base64": self._image_base64(),
                "prompt_text": "prompt",
                "model": "gemini-2.5-flash",
            },
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()

    def _queue_analysis(
        self,
        album_id,
        photo_ids,
        prompt,
        force_rerun=False,
        preferred_run_id=None,
        replace_existing_jobs=False,
        name="",
        prompt_id=None,
    ):
        resp = self.client.post(
            f"/api/albums/{album_id}/queue-analysis",
            json={
                "client_photo_ids": photo_ids,
                "prompt_text": prompt,
                "model": "gemini-2.5-flash",
                "force_rerun": force_rerun,
                "preferred_run_id": preferred_run_id,
                "replace_existing_jobs": replace_existing_jobs,
                "name": name,
                "prompt_id": prompt_id,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()

    def _image_base64(self):
        return base64.b64encode(b"fake image bytes").decode("ascii")


if __name__ == "__main__":
    unittest.main()
