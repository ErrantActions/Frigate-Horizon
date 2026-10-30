"""Bookmark camera ACLs, creator ownership, retention independence and revisions."""

import copy
import tempfile
from pathlib import Path
from unittest.mock import patch

from frigate.models import RecordingBookmark, Recordings
from frigate.record.export import RecordingExporter
from frigate.record.export_metadata import (
    ExportMetadataCategory,
    investigation_metadata,
)
from frigate.record.export_timestamps import ExportTimestampMode, TimestampSpan
from frigate.test.http_api.base_http_test import AuthTestClient, BaseTestHttp


class TestHttpBookmarks(BaseTestHttp):
    def setUp(self):
        super().setUp([RecordingBookmark, Recordings])
        self.minimal_config["cameras"]["backyard"] = copy.deepcopy(
            self.minimal_config["cameras"]["front_door"]
        )
        self.minimal_config["auth"] = {
            "roles": {"front_only": ["front_door"], "other_only": ["backyard"]},
        }
        self.app = self.create_app(enforce_default_admin=True)
        self.base = "/front_door/recordings/bookmarks"

    def seed(self, **values):
        data = {
            "id": "bookmark",
            "camera": "front_door",
            "timestamp": 100,
            "title": "Incident",
            "notes": "Shared note",
            "owner": "alice",
            "created_at": 100,
            "updated_at": 100,
        }
        data.update(values)
        return RecordingBookmark.create(**data)

    def test_migration_has_required_identity_and_camera_ordering_index(self):
        columns = {
            row[1]: row
            for row in self.db.execute_sql(
                "PRAGMA table_info(recordingbookmark)"
            ).fetchall()
        }
        self.assertEqual(columns["id"][3], 1)
        index = self.db.execute_sql(
            "PRAGMA index_info(recordingbookmark_camera_timestamp_id)"
        ).fetchall()
        self.assertEqual([row[2] for row in index], ["camera", "timestamp", "id"])

    def test_unauthenticated_requests_are_rejected(self):
        with AuthTestClient(self.app) as client:
            headers = {"remote-user": "", "remote-role": ""}
            self.assertEqual(client.get(self.base, headers=headers).status_code, 401)
            self.assertEqual(
                client.post(
                    self.base,
                    json={"timestamp": 100, "title": "Denied"},
                    headers=headers,
                ).status_code,
                401,
            )
        self.assertFalse(RecordingBookmark.select().exists())

    def test_shared_reader_can_list_but_cannot_mutate_another_users_note(self):
        self.seed()
        headers = {"remote-user": "bob", "remote-role": "front_only"}
        with AuthTestClient(self.app) as client:
            response = client.get(self.base, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            item = response.json()["items"][0]
            self.assertEqual(item["notes"], "Shared note")
            self.assertFalse(item["can_edit"])
            response = client.patch(
                self.base + "/bookmark",
                json={
                    "title": "Overwrite",
                    "notes": "",
                    "revision": 1,
                },
                headers=headers,
            )
            self.assertEqual(response.status_code, 403)
            self.assertEqual(
                client.delete(
                    self.base + "/bookmark?revision=1", headers=headers
                ).status_code,
                403,
            )
        self.assertEqual(RecordingBookmark.get_by_id("bookmark").title, "Incident")

    def test_camera_scope_applies_to_every_operation(self):
        self.seed()
        denied = {"remote-user": "alice", "remote-role": "other_only"}
        with AuthTestClient(self.app) as client:
            self.assertEqual(client.get(self.base, headers=denied).status_code, 403)
            self.assertEqual(
                client.post(
                    self.base, json={"timestamp": 100, "title": "New"}, headers=denied
                ).status_code,
                403,
            )
            self.assertEqual(
                client.patch(
                    self.base + "/bookmark",
                    json={"title": "Edit", "revision": 1},
                    headers=denied,
                ).status_code,
                403,
            )
            self.assertEqual(
                client.delete(
                    self.base + "/bookmark?revision=1", headers=denied
                ).status_code,
                403,
            )

    def test_creator_can_edit_with_revision_and_stale_edits_are_rejected(self):
        self.seed()
        owner = {"remote-user": "alice", "remote-role": "front_only"}
        with AuthTestClient(self.app) as client:
            response = client.patch(
                self.base + "/bookmark",
                json={
                    "title": "Edited",
                    "notes": "Updated",
                    "revision": 1,
                },
                headers=owner,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["revision"], 2)
            for method in ("patch", "delete"):
                if method == "patch":
                    response = client.patch(
                        self.base + "/bookmark",
                        json={"title": "Stale", "revision": 1},
                        headers=owner,
                    )
                else:
                    response = client.delete(
                        self.base + "/bookmark?revision=1", headers=owner
                    )
                self.assertEqual(response.status_code, 409)
            self.assertEqual(
                client.delete(
                    self.base + "/bookmark?revision=2", headers=owner
                ).status_code,
                204,
            )
        self.assertFalse(RecordingBookmark.select().exists())

    def test_admin_can_edit_and_cross_camera_identifiers_are_not_resolved(self):
        self.seed()
        self.seed(id="other", camera="backyard")
        with AuthTestClient(self.app) as client:
            self.assertEqual(
                client.patch(
                    self.base + "/other", json={"title": "Wrong camera", "revision": 1}
                ).status_code,
                404,
            )
            response = client.patch(
                self.base + "/bookmark", json={"title": "Admin edit", "revision": 1}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["owner"], "alice")

    def test_creation_ignores_no_owner_input_and_does_not_protect_recordings(self):
        Recordings.create(
            id="recording",
            camera="front_door",
            path="/never-opened.mp4",
            start_time=90,
            end_time=110,
            duration=20,
        )
        creator = {"remote-user": "alice", "remote-role": "front_only"}
        with AuthTestClient(self.app) as client:
            response = client.post(
                self.base,
                json={
                    "timestamp": 100,
                    "end_time": 120,
                    "title": "  Incident  ",
                    "notes": "Details",
                },
                headers=creator,
            )
            self.assertEqual(response.status_code, 201)
            value = response.json()
            self.assertEqual(value["owner"], "alice")
            self.assertEqual(value["title"], "Incident")
            self.assertTrue(value["available_at_anchor"])
            self.assertFalse(value["protects_footage"])
            self.assertEqual(
                client.post(
                    self.base,
                    json={"timestamp": 100, "title": "Spoof", "owner": "admin"},
                    headers=creator,
                ).status_code,
                422,
            )
        self.assertEqual(Recordings.get_by_id("recording").storage_tier, "local")
        self.assertFalse(Recordings.get_by_id("recording").delete_requested)

    def test_expired_anchor_is_reported_and_annotation_survives(self):
        self.seed()
        with AuthTestClient(self.app) as client:
            item = client.get(self.base).json()["items"][0]
        self.assertFalse(item["available_at_anchor"])
        self.assertTrue(RecordingBookmark.select().exists())

    def test_pagination_range_and_input_limits(self):
        for index in range(4):
            self.seed(id=str(index), timestamp=100 + index)
        self.seed(id="spanning", timestamp=80, end_time=102)
        self.seed(id="other-camera", camera="backyard")
        with AuthTestClient(self.app) as client:
            page = client.get(self.base + "?after=100&before=104&limit=2").json()
            self.assertEqual([row["id"] for row in page["items"]], ["3", "2"])
            self.assertTrue(page["has_more"])
            page = client.get(
                self.base + "?after=100&before=104&limit=2&offset=4"
            ).json()
            self.assertEqual(page["items"][0]["id"], "spanning")
            self.assertFalse(page["has_more"])
            for data in (
                {"timestamp": 100, "title": " "},
                {"timestamp": -1, "title": "Invalid"},
                {"timestamp": 100, "end_time": 99, "title": "Invalid"},
                {"timestamp": 100, "title": "Invalid", "notes": "x" * 4001},
                {"timestamp": 1e30, "title": "Invalid"},
            ):
                self.assertEqual(client.post(self.base, json=data).status_code, 422)

    def test_note_export_includes_only_shared_camera_range_overlaps(self):
        self.seed(id="inside", timestamp=110)
        self.seed(id="spanning", timestamp=80, end_time=120)
        self.seed(id="before", timestamp=20, end_time=80)
        self.seed(id="after", timestamp=220)
        self.seed(id="other", camera="backyard", timestamp=110)
        exporter = RecordingExporter.__new__(RecordingExporter)
        exporter.config = self.app.frigate_config
        exporter.camera = "front_door"
        exporter.start_time = 100
        exporter.end_time = 200
        exporter.export_id = "note_test"
        exporter.timestamp = ExportTimestampMode.none
        exporter.metadata_categories = (ExportMetadataCategory.notes,)
        exporter._timestamp_files = []

        def mux(command, _playlist, *, step):
            Path(command[-1]).write_bytes(b"synthetic export fixture")
            return 0, b""

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "export.mp4"
            video.write_bytes(b"synthetic export fixture")
            with (
                patch("frigate.record.export.EXPORT_STAGING_DIR", directory),
                patch.object(
                    exporter,
                    "_timestamp_spans",
                    return_value=[TimestampSpan(100, 200, 100)],
                ),
                patch.object(exporter, "_run_ffmpeg_with_progress", side_effect=mux),
                patch(
                    "frigate.record.export.investigation_metadata",
                    wraps=investigation_metadata,
                ) as metadata,
            ):
                exporter._apply_investigation_metadata(str(video))
            notes = metadata.call_args.kwargs["notes"]
            self.assertEqual([note["id"] for note in notes], ["spanning", "inside"])
            self.assertEqual([note["owner"] for note in notes], ["alice", "alice"])
