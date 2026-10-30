"""Persist protected exported assets and closed native control leases."""


def migrate(migrator, database, fake=False, **kwargs):
    migrator.sql(
        'ALTER TABLE "export" ADD COLUMN "retain_indefinitely" INTEGER NOT NULL DEFAULT 0'
    )
    migrator.sql(
        'CREATE TABLE "nativecontroltombstone" ('
        '"id" VARCHAR(64) NOT NULL PRIMARY KEY,'
        '"camera" VARCHAR(256) NOT NULL,"owner" VARCHAR(256) NOT NULL,'
        '"lease_id" VARCHAR(36) NOT NULL,"closed_at" REAL NOT NULL)'
    )


def rollback(migrator, database, fake=False, **kwargs):
    migrator.sql('DROP TABLE "nativecontroltombstone"')
    migrator.sql('ALTER TABLE "export" DROP COLUMN "retain_indefinitely"')
