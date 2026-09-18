from django.apps import AppConfig

from . import __version__


class ForceReloginConfig(AppConfig):
    name = "forcerelogin"
    label = "forcerelogin"
    verbose_name = f"Herlogin v{__version__}"
    default_auto_field = "django.db.models.AutoField"

    def ready(self) -> None:
        from . import signals  # noqa: F401  (registreert de login-ontvanger)
