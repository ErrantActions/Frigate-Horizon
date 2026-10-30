"""Offline authenticated image contracts with synthetic thumbnails and no Apple requests."""

import base64
import io
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import numpy as np
from joserfc import jwt
from joserfc.jwk import OctKey
from PIL import Image

from frigate.comms.apns_thumbnails import (
    NativeThumbnail,
    NativeThumbnailUnavailable,
    render_native_thumbnail,
)
from frigate.models import Event, ReviewSegment, User
from frigate.test import test_native_push_api as native_api_tests


class TestNativeImageAPI(native_api_tests.TestNativePushAPI):
    def setUp(self):
        super().setUp()
        for model in (Event, ReviewSegment):
            previous = model._meta.database
            self.addCleanup(lambda model=model, previous=previous: model.bind(previous))
        self.database.bind([Event, ReviewSegment])
        self.database.create_tables([Event, ReviewSegment])
        self.app = self.client.app
        self.app.jwt_token = OctKey.import_key(
            "synthetic-offline-session-key-32-bytes-long"
        )
        self.config.apns.image_notifications = True
        self.headers["Cookie"] = f"{self.config.auth.cookie_name}={self.token()}"
        self.client.post(
            "/notifications/apns/register", headers=self.headers, json=self.body
        )
        self.media = Path(self.directory.name) / "clips"
        (self.media / "review").mkdir(parents=True)
        self.thumbnail = self.media / "review/thumbnail.webp"
        image = Image.new("RGB", (640, 360), "red")
        image.save(self.thumbnail, format="WEBP")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        self.png = buffer.getvalue()
        self.patch_media = patch(
            "frigate.comms.apns_thumbnails.CLIPS_DIR", str(self.media)
        )
        self.patch_media.start()
        self.addCleanup(self.patch_media.stop)
        ReviewSegment.create(
            id="review-1",
            camera="front",
            start_time=datetime.now(),
            end_time=datetime.now(),
            severity="alert",
            thumb_path=str(self.thumbnail),
            data={},
        )
        Event.create(
            id="event-1",
            label="person",
            camera="front",
            start_time=datetime.now(),
            end_time=datetime.now(),
            top_score=0,
            score=0,
            false_positive=False,
            zones=[],
            thumbnail=base64.b64encode(self.png).decode(),
            region=[],
            box=[],
            area=0,
            plus_id="",
            model_hash="",
            detector_type="",
            model_type="",
            data={},
        )
        self.app.detected_frames_processor = SimpleNamespace(
            get_current_frame=lambda _camera, _options: np.zeros(
                (720, 1280, 3), dtype=np.uint8
            ),
            get_current_frame_time=lambda _camera: time.time(),
        )

    def token(self, username="alice", role="admin", issued=None, expires=None):
        return jwt.encode(
            {"alg": "HS256"},
            {
                "sub": username,
                "role": role,
                "iat": int(time.time()) - 10 if issued is None else issued,
                "exp": int(time.time()) + 3600 if expires is None else expires,
            },
            self.app.jwt_token,
        )

    def image(
        self,
        kind="review",
        identifier="review-1",
        headers=None,
        installation=None,
        query="",
    ):
        return self.client.get(
            f"/notifications/apns/thumbnail/{installation or self.body['installation_id']}/{kind}/{identifier}{query}",
            headers=self.headers if headers is None else headers,
        )

    def test_actual_review_event_and_camera_images_are_small_private_jpeg(self):
        self.assertTrue(
            self.client.get("/notifications/apns", headers=self.headers).json()[
                "image_notifications"
            ]
        )
        for kind, identifier in (
            ("review", "review-1"),
            ("event", "event-1"),
            ("camera", "front"),
        ):
            response = self.image(kind, identifier)
            self.assertEqual(
                response.status_code,
                200,
                response.text if response.status_code != 200 else "",
            )
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            self.assertEqual(response.headers["x-frigate-camera"], "front")
            self.assertEqual(response.headers["content-type"], "image/jpeg")
            self.assertLess(len(response.content), 2 * 1024 * 1024)
            with Image.open(io.BytesIO(response.content)) as image:
                self.assertLessEqual(max(image.size), 1024)
                self.assertLessEqual(image.height, 360)
                self.assertEqual(image.format, "JPEG")

    def test_headers_alone_expired_wrong_account_bad_signature_and_future_sessions_fail(
        self,
    ):
        denied = [
            {"remote-user": "alice", "remote-role": "admin"},
            {
                **self.headers,
                "Cookie": f"{self.config.auth.cookie_name}={self.token(expires=1)}",
            },
            {
                **self.headers,
                "Cookie": f"{self.config.auth.cookie_name}={self.token(username='bob')}",
            },
            {**self.headers, "Cookie": f"{self.config.auth.cookie_name}=not-a-jwt"},
            {
                **self.headers,
                "Cookie": f"{self.config.auth.cookie_name}={self.token(issued=int(time.time()) + 600)}",
            },
        ]
        for headers in denied:
            self.assertEqual(self.image(headers=headers).status_code, 401)

    def test_password_change_deleted_account_and_invalid_role_revoke_images(self):
        User.update(password_changed_at=datetime.now()).where(
            User.username == "alice"
        ).execute()
        self.assertEqual(self.image().status_code, 401)
        User.update(password_changed_at=None, role="revoked").where(
            User.username == "alice"
        ).execute()
        self.assertEqual(self.image().status_code, 403)
        User.delete().where(User.username == "alice").execute()
        self.assertEqual(self.image().status_code, 401)

    def test_installation_ownership_empty_selection_and_current_role_are_checked(self):
        self.assertEqual(self.image(installation=str(uuid4())).status_code, 403)
        other = {
            "remote-user": "bob",
            "remote-role": "admin",
            "Cookie": f"{self.config.auth.cookie_name}={self.token(username='bob')}",
        }
        self.assertEqual(self.image(headers=other).status_code, 403)
        self.client.post(
            "/notifications/apns/register",
            headers=self.headers,
            json={**self.body, "camera_ids": []},
        )
        self.assertEqual(self.image().status_code, 403)
        self.client.post(
            "/notifications/apns/register",
            headers=self.headers,
            json={**self.body, "camera_ids": None},
        )
        self.assertEqual(self.image().status_code, 200)
        User.update(role="revoked").where(User.username == "alice").execute()
        self.assertEqual(self.image().status_code, 403)

    def test_references_resolve_camera_from_database_not_from_claimed_role(self):
        Event.update(camera="garden").where(Event.id == "event-1").execute()
        ReviewSegment.update(camera="garden").where(
            ReviewSegment.id == "review-1"
        ).execute()
        self.assertEqual(self.image("event", "event-1").status_code, 403)
        self.assertEqual(self.image().status_code, 403)
        self.assertEqual(self.image("camera", "garden").status_code, 403)

    def test_unregistration_and_camera_revocation_during_render_prevent_return(self):
        def revoke(*args):
            self.client.delete(
                "/notifications/apns/register/" + self.body["installation_id"],
                headers=self.headers,
            )
            return self.png

        with patch(
            "frigate.api.notification.render_native_thumbnail", side_effect=revoke
        ):
            self.assertEqual(self.image().status_code, 403)

    def test_role_change_during_render_prevents_return(self):
        def revoke(*args):
            User.update(role="revoked").where(User.username == "alice").execute()
            return self.png

        with patch(
            "frigate.api.notification.render_native_thumbnail", side_effect=revoke
        ):
            self.assertEqual(self.image().status_code, 403)

    def test_feature_selection_password_and_source_revocation_during_render(self):
        def mutate(change):
            def render(*args):
                change()
                return self.png

            return render

        for field in ("enabled", "image_notifications"):
            with patch(
                "frigate.api.notification.render_native_thumbnail",
                side_effect=mutate(lambda: setattr(self.config.apns, field, False)),
            ):
                self.assertEqual(self.image().status_code, 503)
            setattr(self.config.apns, field, True)
        with patch(
            "frigate.api.notification.render_native_thumbnail",
            side_effect=mutate(
                lambda: self.client.post(
                    "/notifications/apns/register",
                    headers=self.headers,
                    json={**self.body, "camera_ids": []},
                )
            ),
        ):
            self.assertEqual(self.image().status_code, 403)
        self.client.post(
            "/notifications/apns/register", headers=self.headers, json=self.body
        )
        with patch(
            "frigate.api.notification.render_native_thumbnail",
            side_effect=mutate(
                lambda: (
                    User.update(password_changed_at=datetime.now())
                    .where(User.username == "alice")
                    .execute()
                )
            ),
        ):
            self.assertEqual(self.image().status_code, 401)
        User.update(password_changed_at=None).execute()
        with patch(
            "frigate.api.notification.render_native_thumbnail",
            side_effect=mutate(lambda: ReviewSegment.update(camera="garden").execute()),
        ):
            self.assertEqual(self.image().status_code, 404)
        ReviewSegment.update(camera="front").execute()
        with patch(
            "frigate.api.notification.render_native_thumbnail",
            side_effect=mutate(lambda: ReviewSegment.delete().execute()),
        ):
            self.assertEqual(self.image().status_code, 404)

    def test_wrong_signing_key_and_missing_required_session_claims_are_denied(self):
        for claims, key in (
            (
                {
                    "sub": "alice",
                    "role": "admin",
                    "iat": time.time() - 10,
                    "exp": time.time() + 300,
                },
                OctKey.import_key("another-synthetic-session-key-32-bytes"),
            ),
            (
                {"sub": "alice", "role": "admin", "exp": time.time() + 300},
                self.app.jwt_token,
            ),
            (
                {"sub": "alice", "role": "admin", "iat": time.time() - 10},
                self.app.jwt_token,
            ),
            (
                {
                    "sub": "alice",
                    "role": "admin",
                    "iat": True,
                    "exp": time.time() + 300,
                },
                self.app.jwt_token,
            ),
        ):
            encoded = jwt.encode({"alg": "HS256"}, claims, key)
            headers = {
                **self.headers,
                "Cookie": f"{self.config.auth.cookie_name}={encoded}",
            }
            self.assertEqual(self.image(headers=headers).status_code, 401)

    @unittest.skipIf(
        sys.platform == "win32", "Linux symlink permissions are the deployment target"
    )
    def test_actual_symlink_cannot_escape_private_review_root(self):
        outside = Path(self.directory.name) / "outside.webp"
        outside.write_bytes(self.png)
        link = self.media / "review" / "escape.webp"
        link.symlink_to(outside)
        ReviewSegment.update(thumb_path=str(link)).execute()
        self.assertEqual(self.image().status_code, 404)

    def test_stale_camera_frame_and_renderer_saturation_fall_back_to_text(self):
        self.app.detected_frames_processor.get_current_frame_time = lambda _camera: (
            time.time() - 60
        )
        with patch(
            "frigate.comms.apns_thumbnails.get_most_recent_preview_frame",
            return_value=None,
        ):
            self.assertEqual(self.image("camera", "front").status_code, 404)
        with patch("frigate.comms.apns_thumbnails._RENDER_SLOTS") as slots:
            slots.acquire.return_value = False
            self.assertEqual(self.image().status_code, 404)
            slots.release.assert_not_called()

    def test_feature_disabled_and_reference_free_test_cannot_return_images(self):
        self.config.apns.image_notifications = False
        self.assertFalse(
            self.client.get("/notifications/apns", headers=self.headers).json()[
                "image_notifications"
            ]
        )
        self.assertEqual(self.image().status_code, 503)
        self.config.apns.image_notifications = True
        self.assertEqual(self.image("test", "anything").status_code, 422)
        self.assertEqual(self.image(identifier="missing").status_code, 404)
        self.assertEqual(self.image(query="?height=0").status_code, 422)
        self.assertEqual(self.image(query="?height=1025").status_code, 422)

    def test_arbitrary_paths_symlinks_and_corrupt_media_never_return_bytes(self):
        ReviewSegment.update(
            thumb_path=str(Path(self.directory.name) / "outside.webp")
        ).execute()
        self.assertEqual(self.image().status_code, 404)
        self.thumbnail.write_bytes(b"not an image")
        ReviewSegment.update(thumb_path=str(self.thumbnail)).execute()
        self.assertEqual(self.image().status_code, 404)
        for reference in ("..", "bad%25path", "https:evil"):
            self.assertIn(self.image(identifier=reference).status_code, (403, 404, 422))

    def test_camera_without_existing_frame_uses_only_existing_preview(self):
        self.app.detected_frames_processor = None
        with patch(
            "frigate.comms.apns_thumbnails.get_most_recent_preview_frame",
            return_value=None,
        ):
            self.assertEqual(self.image("camera", "front").status_code, 404)

    def test_incomplete_native_person_best_shot_does_not_send_an_earlier_image(self):
        ReviewSegment.update(data={"native_person_best_shot": False}).execute()
        self.assertEqual(self.image().status_code, 404)
        ReviewSegment.update(data={"native_person_best_shot": True}).execute()
        self.assertEqual(self.image().status_code, 200)


class TestNativeThumbnailBounds(unittest.TestCase):
    def test_oversized_embedded_sources_and_dimensions_fail_closed(self):
        reference = NativeThumbnail(
            "event", "event-1", "front", embedded="a" * (3 * 1024 * 1024)
        )
        with self.assertRaises(NativeThumbnailUnavailable):
            render_native_thumbnail(reference, None, 360)
        image = Image.new("RGB", (10, 10))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        reference = NativeThumbnail(
            "event",
            "event-1",
            "front",
            embedded=base64.b64encode(buffer.getvalue()).decode(),
        )
        with patch("frigate.comms.apns_thumbnails.MAX_SOURCE_PIXELS", 5):
            with self.assertRaises(NativeThumbnailUnavailable):
                render_native_thumbnail(reference, None, 360)
