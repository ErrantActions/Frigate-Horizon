"""Track exact archived content and durable incoming reservations."""


def migrate(migrator, database, fake=False, **kwargs):
    migrator.sql('ALTER TABLE "recordings" ADD COLUMN "archive_bytes" INTEGER')
    migrator.sql(
        'CREATE TABLE "archivetransfer" ('
        '"id" VARCHAR(30) PRIMARY KEY, "path" TEXT NOT NULL UNIQUE, '
        '"size" INTEGER NOT NULL CHECK ("size" >= 0))'
    )
    migrator.sql(
        'CREATE TABLE "recordinglease" ('
        '"id" VARCHAR(128) PRIMARY KEY, "camera" VARCHAR(20) NOT NULL, '
        '"start_time" REAL NOT NULL, "end_time" REAL NOT NULL, '
        '"expires" REAL NOT NULL, "owner" VARCHAR(256))'
    )
    migrator.sql('CREATE INDEX "recordinglease_camera" ON "recordinglease" ("camera")')
    migrator.sql(
        'CREATE INDEX "recordinglease_expires" ON "recordinglease" ("expires")'
    )


def rollback(migrator, database, fake=False, **kwargs):
    migrator.sql('DROP TABLE "recordinglease"')
    migrator.sql('DROP TABLE "archivetransfer"')
    migrator.sql('ALTER TABLE "recordings" DROP COLUMN "archive_bytes"')
