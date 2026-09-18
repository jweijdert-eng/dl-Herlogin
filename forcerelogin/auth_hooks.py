"""Hook into Alliance Auth"""

from django.utils.translation import gettext_lazy as _

from allianceauth import hooks
from allianceauth.services.hooks import MenuItemHook, UrlHook

from . import urls


class ForceReloginMenuItem(MenuItemHook):
    """Menu-item; elke hook heeft z'n eigen klasse nodig, want AA leidt de
    identiteit van een menu-item af uit `sha256(module.KlasseNaam)`."""

    def __init__(self):
        MenuItemHook.__init__(
            self,
            _("Herlogin"),
            "fas fa-user-lock fa-fw",
            "forcerelogin:index",
            order=1070,
            navactive=["forcerelogin:"],
        )

    def render(self, request):
        if request.user.has_perm("forcerelogin.basic_access"):
            return MenuItemHook.render(self, request)
        return ""


@hooks.register("menu_item_hook")
def register_menu():
    return ForceReloginMenuItem()


@hooks.register("url_hook")
def register_urls():
    return UrlHook(urls, "forcerelogin", r"^forcerelogin/")
