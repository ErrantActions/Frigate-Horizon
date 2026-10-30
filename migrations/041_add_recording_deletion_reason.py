"""Distinguish explicit deletion from automatic retention and emergency cleanup."""


def migrate(migrator, database, fake=False, **kwargs):
    migrator.sql(
        'ALTER TABLE "recordings" ADD COLUMN "delete_reason" VARCHAR(16)'
    )


def rollback(migrator, database, fake=False, **kwargs):
    migrator.sql('ALTER TABLE "recordings" DROP COLUMN "delete_reason"')
