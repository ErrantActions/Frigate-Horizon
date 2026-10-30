"""Persist camera-scoped investigation bookmarks independently of retention."""


def migrate(migrator, database, fake=False, **kwargs):
    migrator.sql(
        'CREATE TABLE "recordingbookmark" ('
        '"id" VARCHAR(32) NOT NULL PRIMARY KEY, "camera" VARCHAR(20) NOT NULL, '
        '"timestamp" REAL NOT NULL, "end_time" REAL, '
        '"title" VARCHAR(128) NOT NULL, "notes" TEXT NOT NULL DEFAULT \'\', '
        '"owner" VARCHAR(256) NOT NULL, "created_at" REAL NOT NULL, '
        '"updated_at" REAL NOT NULL, "revision" INTEGER NOT NULL DEFAULT 1)'
    )
    migrator.sql(
        'CREATE INDEX "recordingbookmark_camera_timestamp_id" '
        'ON "recordingbookmark" ("camera", "timestamp", "id")'
    )


def rollback(migrator, database, fake=False, **kwargs):
    migrator.sql('DROP TABLE "recordingbookmark"')
