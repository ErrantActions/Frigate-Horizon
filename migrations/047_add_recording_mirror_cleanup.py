"""Persist daily mirror cleanup progress without reclassifying existing footage."""


def migrate(migrator, database, fake=False, **kwargs):
    migrator.sql(
        'CREATE TABLE "recordingmirrorstate" ('
        '"id" INTEGER PRIMARY KEY CHECK ("id"=1),'
        '"last_cleanup" REAL NOT NULL, "cutoff" REAL,'
        '"cursor_end" REAL, "cursor_id" VARCHAR(30))'
    )
    migrator.sql(
        'CREATE INDEX "recordings_mirror_pending" ON "recordings" ("start_time") '
        "WHERE storage_tier='local' AND archive_bytes IS NULL AND NOT delete_requested"
    )
    migrator.sql(
        'CREATE INDEX "recordings_mirror_cleanup" ON "recordings" ("end_time","id") '
        "WHERE storage_tier='local' AND archive_bytes IS NOT NULL"
    )


def rollback(migrator, database, fake=False, **kwargs):
    migrator.sql('DROP INDEX "recordings_mirror_cleanup"')
    migrator.sql('DROP INDEX "recordings_mirror_pending"')
    migrator.sql('DROP TABLE "recordingmirrorstate"')
