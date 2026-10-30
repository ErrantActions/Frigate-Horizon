"""Export protection persists on the actual asset and never manufactures a bookmark."""

import datetime
import importlib
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from peewee import SqliteDatabase

from frigate.models import Export, ExportCase, User
from frigate.record.export_protection import (
    ExportProtectionConflict,
    protect_export_asset,
    protected_export_exists,
)


class TestExportProtectionMigration(unittest.TestCase):
    def test_046_preserves_existing_exports_and_defaults_them_to_unprotected(self):
        database = SqliteDatabase(":memory:")
        self.addCleanup(database.close)
        database.execute_sql(
            'CREATE TABLE "export" ("id" TEXT PRIMARY KEY, "name" TEXT)'
        )
        database.execute_sql(
            'INSERT INTO "export" VALUES (?, ?)', ("existing", "Evidence")
        )
        migration = importlib.import_module(
            "migrations.046_add_native_feature_protection"
        )
        migrator = SimpleNamespace(sql=database.execute_sql)
        migration.migrate(migrator, database)
        self.assertEqual(
            database.execute_sql(
                'SELECT id,name,retain_indefinitely FROM "export"'
            ).fetchone(),
            ("existing", "Evidence", 0),
        )
        database.execute_sql('UPDATE "export" SET retain_indefinitely=1')
        self.assertEqual(
            database.execute_sql('SELECT retain_indefinitely FROM "export"').fetchone(),
            (1,),
        )
        migration.rollback(migrator, database)
        self.assertEqual(
            database.execute_sql('SELECT id,name FROM "export"').fetchone(),
            ("existing", "Evidence"),
        )


class TestExportProtection(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.db = SqliteDatabase(":memory:")
        self.enterContext(self.db.bind_ctx([Export, ExportCase, User]))
        self.addCleanup(self.db.close)
        self.db.create_tables([ExportCase, Export, User])
        User.create(
            username="owner",
            password_hash="unused",
            role="admin",
            notification_tokens=[],
        )
        self.enterContext(
            patch("frigate.record.export_protection.EXPORT_DIR", str(self.root))
        )
        self.enterContext(
            patch("frigate.record.export_protection.storage_lock", nullcontext)
        )
        self.config = SimpleNamespace(
            auth=SimpleNamespace(roles={"admin": []}),
            cameras={"front": object()},
        )
        self.path = self.root / "evidence.mp4"
        self.path.write_bytes(b"completed exported evidence")
        self.export = Export.create(
            id="opaque-export-1",
            camera="front",
            name="Evidence",
            date=datetime.datetime.now(),
            video_path=str(self.path),
            thumb_path=str(self.root / "thumbnail.webp"),
            in_progress=False,
        )

    def protect(self):
        return protect_export_asset(
            self.export.id, "front", "owner", lambda: self.config
        )

    def test_completed_asset_is_durably_protected_and_idempotent(self):
        before = self.path.read_bytes()
        self.assertEqual(self.protect(), self.export.id)
        self.assertTrue(Export.get_by_id(self.export.id).retain_indefinitely)
        self.assertTrue(protected_export_exists([self.export.id]))
        self.assertEqual(self.protect(), self.export.id)
        self.assertEqual(self.path.read_bytes(), before)

    def test_in_progress_or_missing_asset_cannot_receive_successful_protection(self):
        Export.update(in_progress=True).execute()
        with self.assertRaises(ExportProtectionConflict):
            self.protect()
        self.assertFalse(Export.get_by_id(self.export.id).retain_indefinitely)
        Export.update(in_progress=False).execute()
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.protect()
        self.assertFalse(protected_export_exists([self.export.id]))

    def test_current_database_role_and_camera_are_checked_inside_operation_lock(self):
        User.update(role="viewer").execute()
        with self.assertRaises(PermissionError):
            self.protect()
        User.update(role="admin").execute()
        self.config.cameras = {}
        with self.assertRaises(PermissionError):
            self.protect()
        self.assertFalse(protected_export_exists([self.export.id]))

    @unittest.skipIf(os.name == "nt", "Replacing an open inode requires Linux")
    def test_asset_replacement_during_verification_does_not_protect_another_file(self):
        original_fstat = os.fstat

        def replace(descriptor):
            opened = original_fstat(descriptor)
            self.path.rename(self.root / "original.mp4")
            self.path.write_bytes(b"a different exported asset")
            return opened

        with patch("frigate.record.export_protection.os.fstat", side_effect=replace):
            with self.assertRaises(ExportProtectionConflict):
                self.protect()
        self.assertFalse(protected_export_exists([self.export.id]))

    def test_asset_modification_during_verification_is_rejected(self):
        original_fstat = os.fstat

        def modify(descriptor):
            opened = original_fstat(descriptor)
            self.path.write_bytes(b"modified evidence with a different length")
            return opened

        with patch("frigate.record.export_protection.os.fstat", side_effect=modify):
            with self.assertRaises(ExportProtectionConflict):
                self.protect()
        self.assertFalse(protected_export_exists([self.export.id]))

    def test_compare_swap_rejects_role_password_camera_or_completion_changes(self):
        original_fstat = os.fstat
        mutations = (
            lambda: User.update(role="viewer").execute(),
            lambda: User.update(password_changed_at=datetime.datetime.now()).execute(),
            lambda: Export.update(camera="back").execute(),
            lambda: Export.update(in_progress=True).execute(),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                User.update(role="admin", password_changed_at=None).execute()
                Export.update(camera="front", in_progress=False).execute()

                def change(descriptor):
                    opened = original_fstat(descriptor)
                    mutation()
                    return opened

                with patch(
                    "frigate.record.export_protection.os.fstat", side_effect=change
                ):
                    with self.assertRaises(ExportProtectionConflict):
                        self.protect()
                self.assertFalse(protected_export_exists([self.export.id]))

    def test_empty_files_and_directories_cannot_be_protected(self):
        self.path.write_bytes(b"")
        with self.assertRaises(ExportProtectionConflict):
            self.protect()
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(ExportProtectionConflict):
            self.protect()
        self.assertFalse(protected_export_exists([self.export.id]))

    def test_staged_temporary_output_is_not_a_finalized_export_asset(self):
        staged = self.root / "staging" / "temporary.mp4"
        staged.parent.mkdir()
        staged.write_bytes(b"unfinished staged output")
        Export.update(video_path=str(staged)).execute()
        with self.assertRaises(ExportProtectionConflict):
            self.protect()
        self.assertFalse(protected_export_exists([self.export.id]))

    @unittest.skipIf(os.name == "nt", "Deployment symlink behavior is tested on Linux")
    def test_export_cannot_link_outside_the_asset_store(self):
        outside = self.root.parent / (self.root.name + "-outside.mp4")
        outside.write_bytes(b"not an owned exported asset")
        self.addCleanup(outside.unlink)
        self.path.unlink()
        self.path.symlink_to(outside)
        with self.assertRaises(ExportProtectionConflict):
            self.protect()
        self.assertFalse(protected_export_exists([self.export.id]))


@unittest.skipIf(os.name == "nt", "Full API imports require the real Linux runtime")
class TestNativeExportProtectionAPI(unittest.TestCase):
    def setUp(self):
        from frigate.api.export import router
        from frigate.api.native import router as native_router
        from frigate.test import test_native_push_images as image_tests

        self.fixture = image_tests.TestNativeImageAPI()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.enterContext(self.fixture.database.bind_ctx([ExportCase, Export]))
        self.fixture.database.create_tables([ExportCase, Export])
        self.client = self.fixture.client
        self.client.app.include_router(router)
        self.client.app.include_router(native_router)
        self.client.app.state.native_export_protection = True
        User.update(role="admin").where(User.username == "alice").execute()
        self.headers = self.fixture.headers
        self.root = Path(self.fixture.directory.name) / "exports"
        self.root.mkdir()
        self.enterContext(
            patch("frigate.record.export_protection.EXPORT_DIR", str(self.root))
        )
        self.enterContext(
            patch("frigate.record.export_protection.storage_lock", nullcontext)
        )
        self.enterContext(patch("frigate.api.export.storage_lock", nullcontext))
        self.enterContext(
            patch("frigate.api.export._get_files_in_use", return_value=set())
        )
        self.path = self.root / "evidence.mp4"
        self.path.write_bytes(b"finalized synthetic exported evidence")
        self.case = ExportCase.create(
            id="evidence-case", name="Evidence", created_at=100, updated_at=100
        )
        self.export = Export.create(
            id="evidence-1",
            camera="front",
            name="Evidence",
            date=datetime.datetime.now(),
            video_path=str(self.path),
            thumb_path=str(self.root / "evidence.webp"),
            in_progress=False,
            export_case=self.case,
        )
        self.url = f"/exports/{self.export.id}/protect"

    def protect(self, headers=None):
        return self.client.post(
            self.url, headers=self.headers if headers is None else headers
        )

    def test_real_post_is_private_durable_and_idempotent(self):
        for _ in range(2):
            response = self.protect()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["protected_id"], self.export.id)
            self.assertTrue(response.json()["success"])
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            self.assertNotIn(str(self.root), response.text)
            self.assertTrue(Export.get_by_id(self.export.id).retain_indefinitely)
            self.assertTrue(self.path.is_file())

    def test_unavailable_or_missing_readiness_blocks_direct_post_and_advertisement(
        self,
    ):
        for missing in (False, True):
            with self.subTest(missing=missing):
                self.client.app.state.native_export_protection = False
                if missing:
                    del self.client.app.state.native_export_protection
                capability = self.client.get("/native/features", headers=self.headers)
                self.assertEqual(capability.status_code, 200, capability.text)
                self.assertIs(capability.json()["export_protection"], False)
                with patch("frigate.api.export.protect_export_asset") as mutation:
                    response = self.protect()
                self.assertEqual(response.status_code, 503, response.text)
                self.assertFalse(response.json().get("success", False))
                self.assertEqual(response.headers["cache-control"], "private, no-store")
                mutation.assert_not_called()
                self.assertFalse(Export.get_by_id(self.export.id).retain_indefinitely)
                self.assertTrue(self.path.is_file())
        self.client.app.state.native_export_protection = True
        capability = self.client.get("/native/features", headers=self.headers)
        self.assertIs(capability.json()["export_protection"], True)
        self.assertEqual(self.protect().status_code, 200)
        self.assertTrue(Export.get_by_id(self.export.id).retain_indefinitely)

    def test_unavailable_readiness_does_not_bypass_session_or_current_role_checks(self):
        self.client.app.state.native_export_protection = False
        self.assertEqual(
            self.protect({"remote-user": "alice", "remote-role": "admin"}).status_code,
            401,
        )
        User.update(role="front_only").where(User.username == "alice").execute()
        self.assertEqual(self.protect().status_code, 403)
        self.assertFalse(Export.get_by_id(self.export.id).retain_indefinitely)

    def test_disabling_readiness_does_not_disable_existing_protection_guards(self):
        self.assertEqual(self.protect().status_code, 200)
        self.client.app.state.native_export_protection = False
        response = self.client.post(
            "/exports/delete", headers=self.headers, json={"ids": [self.export.id]}
        )
        self.assertEqual(response.status_code, 409, response.text)
        response = self.client.delete(
            f"/cases/{self.case.id}?delete_exports=true", headers=self.headers
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue(self.path.is_file())
        self.assertTrue(Export.get_by_id(self.export.id).retain_indefinitely)

    def test_rename_cannot_overwrite_protection_from_a_stale_loaded_export(self):
        self.assertEqual(self.protect().status_code, 200)
        stale = Export.get_by_id(self.export.id)
        stale.retain_indefinitely = False
        renamed = self.root / "renamed.mp4"
        with (
            patch("frigate.api.export.Export.get", return_value=stale),
            patch("frigate.api.export.export_video_path", return_value=str(renamed)),
        ):
            response = self.client.patch(
                f"/export/{self.export.id}/rename",
                headers=self.headers,
                json={"name": "Renamed evidence"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        saved = Export.get_by_id(self.export.id)
        self.assertTrue(saved.retain_indefinitely)
        self.assertEqual(saved.video_path, str(renamed))
        self.assertTrue(renamed.is_file())

    def test_protected_rename_changes_only_metadata_and_never_moves_the_evidence(self):
        self.assertEqual(self.protect().status_code, 200)
        before = self.path.read_bytes()
        with patch("frigate.api.export.os.rename") as move:
            response = self.client.patch(
                f"/export/{self.export.id}/rename",
                headers=self.headers,
                json={"name": "Protected evidence"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        move.assert_not_called()
        saved = Export.get_by_id(self.export.id)
        self.assertEqual(saved.name, "Protected evidence")
        self.assertTrue(saved.retain_indefinitely)
        self.assertEqual(saved.video_path, str(self.path))
        self.assertEqual(self.path.read_bytes(), before)

    def test_current_database_role_scope_session_and_account_are_required(self):
        self.assertEqual(
            self.protect({"remote-user": "alice", "remote-role": "admin"}).status_code,
            401,
        )
        User.update(role="front_only").where(User.username == "alice").execute()
        self.assertEqual(self.protect().status_code, 403)
        User.update(role="admin").where(User.username == "alice").execute()
        Export.update(camera="removed").execute()
        self.assertEqual(self.protect().status_code, 403)
        Export.update(camera="front").execute()
        User.update(password_changed_at=datetime.datetime.now()).where(
            User.username == "alice"
        ).execute()
        self.assertEqual(self.protect().status_code, 401)
        User.delete().where(User.username == "alice").execute()
        self.assertIn(self.protect().status_code, (401, 404))
        self.assertFalse(Export.get_by_id(self.export.id).retain_indefinitely)

    def test_incomplete_or_missing_assets_fail_closed(self):
        Export.update(in_progress=True).execute()
        self.assertEqual(self.protect().status_code, 409)
        Export.update(in_progress=False).execute()
        self.path.unlink()
        self.assertEqual(self.protect().status_code, 404)
        self.assertFalse(Export.get_by_id(self.export.id).retain_indefinitely)

    def test_single_item_and_mixed_bulk_deletions_preserve_every_asset(self):
        self.assertEqual(self.protect().status_code, 200)
        other = self.root / "other.mp4"
        other.write_bytes(b"unprotected exported evidence")
        Export.create(
            id="evidence-2",
            camera="front",
            name="Other",
            date=100,
            video_path=str(other),
            thumb_path=str(self.root / "other.webp"),
            in_progress=False,
        )
        for ids in ([self.export.id], [self.export.id, "evidence-2"]):
            response = self.client.post(
                "/exports/delete", headers=self.headers, json={"ids": ids}
            )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertTrue(self.path.is_file())
            self.assertTrue(other.is_file())
            self.assertEqual(Export.select().count(), 2)

    def test_case_deletion_cannot_delete_protected_exports_but_can_unassign_them(self):
        self.assertEqual(self.protect().status_code, 200)
        response = self.client.delete(
            f"/cases/{self.case.id}?delete_exports=true", headers=self.headers
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue(ExportCase.get_or_none(ExportCase.id == self.case.id))
        self.assertTrue(self.path.is_file())
        response = self.client.delete(f"/cases/{self.case.id}", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        saved = Export.get_by_id(self.export.id)
        self.assertTrue(saved.retain_indefinitely)
        self.assertIsNone(saved.export_case_id)
        self.assertTrue(self.path.is_file())

    def test_failed_and_stale_export_cleanup_cannot_remove_protected_assets(self):
        from frigate.jobs.export import reap_stale_exports
        from frigate.record.export import RecordingExporter

        self.assertEqual(self.protect().status_code, 200)
        with patch("frigate.record.archive.storage_lock", nullcontext):
            RecordingExporter._discard_failed_export(
                SimpleNamespace(export_id=self.export.id),
                str(self.path),
                self.export.thumb_path,
            )
        self.assertTrue(self.path.is_file())
        self.assertTrue(Export.get_by_id(self.export.id).retain_indefinitely)
        Export.update(in_progress=True).execute()
        with (
            patch("frigate.record.archive.storage_lock", nullcontext),
            patch("frigate.jobs.export.EXPORT_STAGING_DIR", str(self.root / "staging")),
            patch("frigate.jobs.export.os.path.getsize", return_value=0),
        ):
            reap_stale_exports()
        self.assertTrue(self.path.is_file())
        self.assertTrue(Export.get_by_id(self.export.id).retain_indefinitely)

    def test_camera_export_cleanup_keeps_protected_rows_and_files(self):
        from frigate.models import Previews, Recordings, Regions, Timeline, Trigger
        from frigate.util.camera_cleanup import cleanup_camera_db

        self.assertEqual(self.protect().status_code, 200)
        models = [Previews, Recordings, Regions, Timeline, Trigger]
        self.enterContext(self.fixture.database.bind_ctx(models))
        self.fixture.database.create_tables(models)
        with patch("frigate.record.archive.storage_lock", nullcontext):
            counts, paths = cleanup_camera_db("front", delete_exports=True)
        self.assertEqual(counts["exports"], 0)
        self.assertEqual(paths, [])
        self.assertTrue(self.path.is_file())
        self.assertTrue(Export.get_by_id(self.export.id).retain_indefinitely)

    def test_orphan_sync_rechecks_live_references_before_deleting_a_protected_asset(
        self,
    ):
        from frigate.util.media import sync_exports

        self.assertEqual(self.protect().status_code, 200)
        original_select = Export.select
        calls = 0

        def stale_snapshot(*fields):
            nonlocal calls
            calls += 1
            if calls == 1:
                return []
            return original_select(*fields)

        with (
            patch("frigate.util.media.EXPORT_DIR", str(self.root)),
            patch("frigate.util.media.CLIPS_DIR", str(self.root / "clips")),
            patch("frigate.record.archive.storage_lock", nullcontext),
            patch("frigate.util.media.Export.select", side_effect=stale_snapshot),
        ):
            result = sync_exports(force=True)
        self.assertEqual(result.orphans_deleted, 0)
        self.assertEqual(result.bytes_reclaimed, 0)
        self.assertTrue(self.path.is_file())
        self.assertTrue(Export.get_by_id(self.export.id).retain_indefinitely)
