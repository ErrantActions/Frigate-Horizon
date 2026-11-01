"""Current-account authorization for long-lived recording readers."""

from peewee import DatabaseError

from frigate.models import User


def recording_account_allows(owner: str, camera: str, issued: float, config) -> bool:
    """Revoke readers after account, password, role or camera-scope changes."""
    database = User._meta.database
    if database is None or not config.auth.enabled:
        return False
    opened_here = database.is_closed()
    try:
        account = User.get_or_none(User.username == owner)
        if account is None or account.role not in config.auth.roles:
            return False
        if (
            account.password_changed_at is not None
            and issued < account.password_changed_at.timestamp()
        ):
            return False
        return camera in User.get_allowed_cameras(
            account.role, config.auth.roles, set(config.cameras)
        )
    except DatabaseError:
        return False
    finally:
        if opened_here:
            database.close()
