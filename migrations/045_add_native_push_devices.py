"""Keep native Apple device tokens separate from browser push subscriptions."""


def migrate(migrator, database, fake=False, **kwargs):
    migrator.sql(
        'ALTER TABLE "user" ADD COLUMN "native_push_devices" JSON NOT NULL DEFAULT \'[]\''
    )


def rollback(migrator, database, fake=False, **kwargs):
    migrator.sql('ALTER TABLE "user" DROP COLUMN "native_push_devices"')
