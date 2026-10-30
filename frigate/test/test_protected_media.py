"""Protected library authorization, accounting and confirmed release tests."""

import unittest
from contextlib import contextmanager, nullcontext
from time import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from filelock import Timeout
from peewee import SqliteDatabase
from playhouse.sqliteq import SqliteQueueDatabase

from frigate.api.auth import require_admin_by_default
from frigate.api.event import router as event_router
from frigate.api.protected_media import router
from frigate.api.record import router as record_router
from frigate.api.review import router as review_router
from frigate.models import (
    Event,
    RecordingLease,
    Recordings,
    ReviewSegment,
    Timeline,
    UserReviewStatus,
)
from frigate.record.policy import (
    RecordingDeletionInProgress,
    RecordingProtected,
    retain_event,
)


class TestProtectedMedia(unittest.TestCase):
    def setUp(self):
        self.db = SqliteDatabase(
            f"file:protected-{uuid4().hex}?mode=memory&cache=shared",
            uri=True,
            check_same_thread=False,
        )
        models = [
            Event,
            Recordings,
            RecordingLease,
            ReviewSegment,
            Timeline,
            UserReviewStatus,
        ]
        self.enterContext(self.db.bind_ctx(models))
        self.db.connect()
        self.addCleanup(self.db.close)
        with patch.object(Event.end_time, "null", True):
            self.db.create_tables(models)
        record = SimpleNamespace(
            alerts=SimpleNamespace(pre_capture=5, post_capture=5),
            detections=SimpleNamespace(pre_capture=2, post_capture=2),
        )
        self.app = FastAPI(dependencies=[Depends(require_admin_by_default())])
        self.app.frigate_config = SimpleNamespace(
            record=record,
            cameras={
                name: SimpleNamespace(record=record) for name in ("front", "back")
            },
            auth=SimpleNamespace(
                roles={
                    "admin": [],
                    "viewer": [],
                    "front_only": ["front"],
                    "no_cameras": ["absent"],
                }
            ),
            proxy=SimpleNamespace(separator=","),
        )
        self.app.storage_maintainer = MagicMock()
        self.app.storage_maintainer.get_archive_status.return_value = {
            "state": "blocked",
            "updated_at": 100,
        }
        self.app.include_router(router)
        self.app.include_router(event_router)
        self.app.include_router(record_router)
        self.app.include_router(review_router)
        self.app.embeddings = None
        self.client = self.enterContext(TestClient(self.app))
        self.headers = {"remote-user": "test", "remote-role": "admin"}
        self.enterContext(
            patch("frigate.api.event.storage_lock", return_value=nullcontext())
        )
        self.enterContext(
            patch("frigate.api.record.storage_lock", return_value=nullcontext())
        )
        self.enterContext(
            patch("frigate.api.review.storage_lock", return_value=nullcontext())
        )

    def event(
        self, name="saved", camera="front", start=100, end=110, saved=True, **fields
    ):
        return Event.create(
            id=name,
            camera=camera,
            label="person",
            start_time=start,
            end_time=end,
            top_score=1,
            score=1,
            false_positive=False,
            zones=[],
            thumbnail="",
            region=[],
            box=[],
            area=0,
            retain_indefinitely=saved,
            has_clip=fields.pop("has_clip", True),
            has_snapshot=True,
            plus_id="",
            model_hash="",
            detector_type="",
            model_type="",
            data=fields.pop("data", {}),
            **fields,
        )

    def recording(self, name="segment", camera="front", start=100, end=110, **fields):
        return Recordings.create(
            id=name,
            camera=camera,
            path=f"/not-accessed/{name}.mp4",
            start_time=start,
            end_time=end,
            duration=end - start,
            segment_size=fields.pop("segment_size", 2),
            **fields,
        )

    def listing(self, role="admin", **params):
        return self.client.get(
            "/protected-media",
            params=params,
            headers={"remote-user": "test", "remote-role": role},
        )

    def release(self, event_id="saved", body=None, role="admin"):
        return self.client.request(
            "DELETE",
            f"/events/{event_id}/retain",
            json=body
            if body is not None
            else {"confirm": True, "acknowledge_retention": True},
            headers={"remote-user": "test", "remote-role": role},
        )

    def test_requires_authentication_and_known_role(self):
        self.event()
        self.assertEqual(self.client.get("/protected-media").status_code, 401)
        self.assertEqual(self.listing("unknown").status_code, 403)
        self.assertEqual(self.listing("no_cameras").json()["total"], 0)

    def test_camera_acl_applies_to_list_filters_aggregates_and_urls(self):
        self.event()
        self.event("secret", "back")
        self.recording()
        self.recording("secret-file", "back", segment_size=200)
        response = self.listing("front_only")
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["cameras"], ["front"])
        self.assertEqual(result["storage"]["local"]["known_bytes"], 2 * 2**20)
        self.assertNotIn("back", response.text)
        self.assertEqual(self.listing("front_only", camera="back").status_code, 403)
        self.assertTrue(result["items"][0]["original_url"].startswith("front/start/"))

    def test_historical_camera_uses_same_acl_as_existing_media_endpoints(self):
        self.event("old", "removed_camera")
        self.recording("old-file", "removed_camera")
        self.assertEqual(self.listing().json()["cameras"], ["removed_camera"])
        self.assertEqual(self.listing("viewer").json()["total"], 1)
        self.assertEqual(self.listing("front_only").json()["total"], 0)

    def test_history_remains_browsable_with_no_configured_cameras(self):
        self.event("old", "removed_camera")
        self.recording("old-file", "removed_camera")
        self.app.frigate_config.cameras = {}
        result = self.listing()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["storage"]["local"]["recordings"], 1)
        self.assertEqual(result.json()["total"], 1)
        self.assertEqual(self.listing("front_only").json()["total"], 0)

    def test_joins_count_shared_physical_segments_once_even_across_pages(self):
        self.event("first")
        self.event("second")
        self.recording()
        result = self.listing(limit=1).json()
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["storage"]["local"]["recordings"], 1)
        self.assertEqual(result["storage"]["local"]["total_bytes"], 2 * 2**20)
        self.assertNotEqual(
            result["items"][0]["id"],
            self.listing(limit=1, offset=1).json()["items"][0]["id"],
        )

    def test_capture_padding_and_legacy_clip_flag_match_protection_policy(self):
        self.event(has_clip=False)
        self.recording("pre", start=90, end=95)
        self.recording("post", start=115, end=120)
        self.recording("outside", start=121, end=130)
        result = self.listing().json()
        self.assertEqual(result["storage"]["local"]["recordings"], 2)
        self.assertIsNotNone(result["items"][0]["original_url"])

    def test_unknown_sizes_are_not_reported_as_zero_and_archive_is_offline_safe(self):
        self.event()
        self.recording(
            "archive", storage_tier="archive", archive_bytes=None, segment_size=0
        )
        with patch(
            "pathlib.Path.stat", side_effect=AssertionError("No filesystem probes")
        ):
            result = self.listing().json()
        archive = result["storage"]["archive"]
        self.assertEqual(archive["unknown_size_recordings"], 1)
        self.assertIsNone(archive["total_bytes"])
        self.assertEqual(result["items"][0]["availability"], "archive_unavailable")
        self.assertIsNone(result["items"][0]["original_url"])
        self.assertIsNotNone(result["items"][0]["snapshot_url"])

    def test_archive_bytes_and_pending_deletion(self):
        self.event()
        self.recording(
            "archive",
            storage_tier="archive",
            archive_bytes=12345,
            delete_requested=True,
        )
        result = self.listing().json()
        self.assertEqual(result["storage"]["archive"]["total_bytes"], 12345)
        self.assertEqual(result["items"][0]["availability"], "deleting")
        self.assertIsNone(result["items"][0]["original_url"])

    def test_only_fresh_cached_archive_health_enables_original_links(self):
        self.event()
        self.recording("archive", storage_tier="archive", archive_bytes=12345)
        health = self.app.storage_maintainer.get_archive_status
        health.return_value = {"state": "ready", "updated_at": time()}
        result = self.listing().json()
        self.assertEqual(result["items"][0]["availability"], "archive_online")
        self.assertIsNotNone(result["items"][0]["original_url"])
        health.return_value = {"state": "ready", "updated_at": time() - 120}
        result = self.listing().json()
        self.assertEqual(result["items"][0]["availability"], "archive_unavailable")
        self.assertIsNone(result["items"][0]["original_url"])

    def test_filters_validate_and_cover_full_history(self):
        self.event("old", start=10, end=20)
        self.event("recent", start=200, end=210)
        self.event("open", start=250, end=None)
        self.assertEqual(self.listing(after=19, before=21).json()["total"], 1)
        self.assertEqual(self.listing(after=300).json()["total"], 1)
        self.assertEqual(self.listing(after=2, before=1).status_code, 422)
        self.assertEqual(self.listing(limit=101).status_code, 422)
        self.assertEqual(self.listing(offset=-1).status_code, 422)
        self.assertEqual(self.listing(after="NaN").status_code, 422)

    def test_temporary_leases_are_not_permanent_evidence(self):
        self.event(saved=False)
        self.recording()
        RecordingLease.create(
            id="reader",
            camera="front",
            start_time=100,
            end_time=110,
            expires=9999999999,
        )
        result = self.listing().json()
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["storage"]["local"]["recordings"], 0)

    def test_unprotect_is_admin_only_and_requires_both_confirmations(self):
        self.event()
        self.assertEqual(self.release(role="viewer").status_code, 403)
        self.assertEqual(self.release(body={}).status_code, 422)
        self.assertEqual(self.release(body={"confirm": True}).status_code, 422)
        self.assertEqual(
            self.release(
                body={"confirm": False, "acknowledge_retention": True}
            ).status_code,
            422,
        )
        self.assertTrue(Event.get_by_id("saved").retain_indefinitely)

    def test_unprotect_preserves_unrelated_fields_recordings_and_other_protection(self):
        self.event(data={"description": "evidence"}, sub_label="original")
        self.event("other")
        self.recording()

        @contextmanager
        def lock():
            Event.update(sub_label="concurrent", data={"description": "new"}).where(
                Event.id == "saved"
            ).execute()
            yield

        with patch("frigate.api.event.storage_lock", lock):
            self.assertEqual(self.release().status_code, 200)
        event = Event.get_by_id("saved")
        self.assertFalse(event.retain_indefinitely)
        self.assertEqual(event.sub_label, "concurrent")
        self.assertEqual(event.data, {"description": "new"})
        self.assertTrue(event.has_clip)
        self.assertEqual(Recordings.select().count(), 1)
        self.assertTrue(Event.get_by_id("other").retain_indefinitely)
        self.assertEqual(self.listing().json()["storage"]["local"]["recordings"], 1)

    def test_unprotect_handles_event_removed_while_waiting_for_lock(self):
        self.event()

        @contextmanager
        def lock():
            Event.delete().where(Event.id == "saved").execute()
            yield

        with patch("frigate.api.event.storage_lock", lock):
            self.assertEqual(self.release().status_code, 404)
        self.assertEqual(self.release("absent").status_code, 404)

    def test_unprotect_lock_timeout_preserves_protection(self):
        self.event()
        with patch(
            "frigate.api.event.storage_lock",
            side_effect=Timeout("recording-storage"),
        ):
            response = self.release()
        self.assertEqual(response.status_code, 503)
        self.assertTrue(Event.get_by_id("saved").retain_indefinitely)

    def test_release_does_not_allow_retain_to_bypass_pending_deletion(self):
        self.event()
        self.recording(delete_requested=True, delete_reason="retention")
        self.assertEqual(self.release().status_code, 200)
        with patch("frigate.record.archive.storage_lock", return_value=nullcontext()):
            with self.assertRaises(RecordingDeletionInProgress):
                retain_event("saved")
        self.assertFalse(Event.get_by_id("saved").retain_indefinitely)

    def test_confirmed_unprotect_uses_atomic_update_with_production_queued_database(
        self,
    ):
        self.event()
        queued = SqliteQueueDatabase(
            self.db.database, uri=True, check_same_thread=False
        )
        try:
            with queued.bind_ctx([Event]):
                self.assertEqual(self.release().status_code, 200)
                self.assertFalse(Event.get_by_id("saved").retain_indefinitely)
        finally:
            queued.stop()
            queued.close()

    def test_saved_event_deletion_conflicts_without_touching_media_or_metadata(self):
        self.event()
        Timeline.create(
            timestamp=100,
            camera="front",
            source="tracked_object",
            source_id="saved",
            class_type="visible",
            data={},
        )
        with patch("frigate.api.event.Path.unlink") as unlink:
            response = self.client.delete("/events/saved", headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["message"], RecordingProtected.message)
        unlink.assert_not_called()
        self.assertTrue(Event.get_by_id("saved").retain_indefinitely)
        self.assertEqual(Timeline.select().count(), 1)

    def test_event_batch_preflights_all_protection_before_any_deletion(self):
        self.event("ordinary", saved=False)
        self.event()
        with patch("frigate.api.event.Path.unlink") as unlink:
            response = self.client.request(
                "DELETE",
                "/events/",
                json={"event_ids": ["ordinary", "saved"]},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 409)
        unlink.assert_not_called()
        self.assertEqual(Event.select().count(), 2)

    def test_event_deletion_rechecks_protection_after_acquiring_storage_lock(self):
        self.event(saved=False)

        @contextmanager
        def lock():
            Event.update(retain_indefinitely=True).where(Event.id == "saved").execute()
            yield

        with (
            patch("frigate.api.event.storage_lock", lock),
            patch("frigate.api.event.Path.unlink") as unlink,
        ):
            response = self.client.delete("/events/saved", headers=self.headers)
        self.assertEqual(response.status_code, 409)
        unlink.assert_not_called()
        self.assertTrue(Event.get_by_id("saved").retain_indefinitely)

    def test_event_can_be_deleted_only_after_confirmed_unprotect(self):
        self.event()
        self.assertEqual(self.release().status_code, 200)
        with patch("frigate.api.event.Path.unlink") as unlink:
            response = self.client.delete("/events/saved", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(unlink.call_count, 3)
        self.assertIsNone(Event.get_or_none(Event.id == "saved"))

    def test_unprotected_event_batch_preserves_existing_missing_id_response(self):
        self.event("one", saved=False)
        self.event("two", saved=False)
        with patch("frigate.api.event.Path.unlink") as unlink:
            response = self.client.request(
                "DELETE",
                "/events/",
                json={"event_ids": ["one", "missing", "two"]},
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted_events"], ["one", "two"])
        self.assertEqual(response.json()["not_found_events"], ["missing"])
        self.assertEqual(unlink.call_count, 6)
        self.assertEqual(Event.select().count(), 0)

    def test_recording_delete_preflights_entire_range_and_preserves_all_rows(self):
        self.event()
        self.recording("ordinary", start=200, end=210)
        self.recording("protected", start=95, end=100)
        with patch("frigate.api.record.request_recording_deletion") as delete:
            response = self.client.delete(
                "/recordings/start/90/end/220", headers=self.headers
            )
        self.assertEqual(response.status_code, 409)
        delete.assert_not_called()
        self.assertEqual(Recordings.select().count(), 2)
        self.assertFalse(
            Recordings.select().where(Recordings.delete_requested == True).exists()
        )

    def test_all_saved_events_sharing_padded_recording_must_be_unprotected_first(self):
        self.event()
        self.event("overlap", start=105, end=115)
        self.recording(start=95, end=100)
        self.assertEqual(self.release().status_code, 200)
        with patch(
            "frigate.api.record.request_recording_deletion", return_value=True
        ) as delete:
            response = self.client.delete(
                "/recordings/start/90/end/130", headers=self.headers
            )
            self.assertEqual(response.status_code, 409)
            delete.assert_not_called()
            self.assertEqual(self.release("overlap").status_code, 200)
            response = self.client.delete(
                "/recordings/start/90/end/130", headers=self.headers
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(delete.call_args.kwargs["capture_padding"].pre, 5)
        self.assertEqual(Recordings.select().count(), 0)

    def test_recording_delete_race_returns_conflict_without_deleting_protected_row(
        self,
    ):
        self.recording()
        with patch(
            "frigate.api.record.request_recording_deletion",
            side_effect=RecordingProtected,
        ):
            response = self.client.delete(
                "/recordings/start/90/end/130", headers=self.headers
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Recordings.select().count(), 1)
        self.assertFalse(Recordings.get_by_id("segment").delete_requested)

    def test_review_delete_cannot_bypass_shared_saved_recording_protection(self):
        self.event(start=105, end=115)
        self.recording(start=95, end=100)
        ReviewSegment.create(
            id="review",
            camera="front",
            start_time=90,
            end_time=130,
            severity="alert",
            thumb_path="/not-accessed/review.webp",
            data={},
        )
        UserReviewStatus.create(user_id="test", review_segment="review")
        with patch("frigate.api.review.request_recording_deletion") as delete:
            response = self.client.post(
                "/reviews/delete", json={"ids": ["review"]}, headers=self.headers
            )
        self.assertEqual(response.status_code, 409)
        delete.assert_not_called()
        self.assertEqual(Recordings.select().count(), 1)
        self.assertEqual(ReviewSegment.select().count(), 1)
        self.assertEqual(UserReviewStatus.select().count(), 1)

    def test_manual_deletion_entrypoints_remain_admin_only(self):
        self.event(saved=False)
        self.recording()
        viewer = {"remote-user": "test", "remote-role": "viewer"}
        with (
            patch("frigate.api.event.Path.unlink") as unlink,
            patch("frigate.api.record.request_recording_deletion") as delete_recording,
            patch("frigate.api.review.request_recording_deletion") as delete_review,
        ):
            responses = [
                self.client.delete("/events/saved", headers=viewer),
                self.client.request(
                    "DELETE", "/events/", json={"event_ids": ["saved"]}, headers=viewer
                ),
                self.client.delete("/recordings/start/90/end/130", headers=viewer),
                self.client.post(
                    "/reviews/delete", json={"ids": ["review"]}, headers=viewer
                ),
            ]
        self.assertEqual([response.status_code for response in responses], [403] * 4)
        unlink.assert_not_called()
        delete_recording.assert_not_called()
        delete_review.assert_not_called()
        self.assertEqual(Event.select().count(), 1)
        self.assertEqual(Recordings.select().count(), 1)

    def test_review_delete_after_unprotect_preserves_unrelated_saved_media(self):
        self.event(start=105, end=115)
        self.recording(start=95, end=100)
        self.event("unrelated", camera="back", start=200, end=210)
        self.recording("unrelated", camera="back", start=200, end=210)
        ReviewSegment.create(
            id="review",
            camera="front",
            start_time=90,
            end_time=130,
            severity="alert",
            thumb_path="/not-accessed/review.webp",
            data={},
        )
        UserReviewStatus.create(user_id="test", review_segment="review")
        self.assertEqual(self.release().status_code, 200)
        with patch(
            "frigate.api.review.request_recording_deletion", return_value=True
        ) as delete:
            response = self.client.post(
                "/reviews/delete", json={"ids": ["review"]}, headers=self.headers
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(delete.call_args.kwargs["capture_padding"].pre, 5)
        self.assertEqual(ReviewSegment.select().count(), 0)
        self.assertEqual(UserReviewStatus.select().count(), 0)
        self.assertEqual(Recordings.select().count(), 1)
        self.assertTrue(Event.get_by_id("unrelated").retain_indefinitely)
        self.assertEqual(Recordings.get_by_id("unrelated").camera, "back")
