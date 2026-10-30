"""Track recording placement without changing stable recording paths."""

def migrate(migrator, database, fake=False, **kwargs):
    migrator.sql(
        'ALTER TABLE "recordings" ADD COLUMN "storage_tier" VARCHAR(8) NOT NULL DEFAULT \'local\''
    )
    migrator.sql(
        'ALTER TABLE "recordings" ADD COLUMN "delete_requested" INTEGER NOT NULL DEFAULT 0'
    )
    migrator.sql(
        'CREATE INDEX IF NOT EXISTS "recordings_storage_tier" '
        'ON "recordings" ("storage_tier")'
    )
    migrator.sql(
        'CREATE INDEX IF NOT EXISTS "recordings_tier_start" '
        'ON "recordings" ("storage_tier", "start_time")'
    )
    migrator.sql(
        'CREATE INDEX IF NOT EXISTS "recordings_tier_expiration" '
        'ON "recordings" ("storage_tier", "delete_requested", "end_time", "id")'
    )


def rollback(migrator, database, fake=False, **kwargs):
    migrator.sql('DROP INDEX IF EXISTS "recordings_tier_start"')
    migrator.sql('DROP INDEX IF EXISTS "recordings_tier_expiration"')
    migrator.sql('DROP INDEX IF EXISTS "recordings_storage_tier"')
    migrator.sql('ALTER TABLE "recordings" DROP COLUMN "delete_requested"')
    migrator.sql('ALTER TABLE "recordings" DROP COLUMN "storage_tier"')
