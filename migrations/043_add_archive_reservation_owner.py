"""Identify compression reservations without reclaiming another worker's files."""


def migrate(migrator, database, fake=False, **kwargs):
    migrator.sql(
        'ALTER TABLE "archivetransfer" ADD COLUMN "kind" VARCHAR(16) '
        "NOT NULL DEFAULT 'transfer'"
    )
    migrator.sql(
        'ALTER TABLE "archivetransfer" ADD COLUMN "owner" VARCHAR(32)'
    )


def rollback(migrator, database, fake=False, **kwargs):
    migrator.sql('ALTER TABLE "archivetransfer" DROP COLUMN "owner"')
    migrator.sql('ALTER TABLE "archivetransfer" DROP COLUMN "kind"')
