"""Admin — de geschiedenis van herlogin-verzoeken, plus intrekken in bulk."""

from django.contrib import admin
from django.utils import timezone

from .models import CharacterRelogin, ReloginRequest, invalidate_pending


class StatusFilter(admin.SimpleListFilter):
    title = "status"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return (
            (ReloginRequest.STATUS_PENDING, "wachtend (main)"),
            (ReloginRequest.STATUS_ALTS, "alts open"),
            (ReloginRequest.STATUS_FULFILLED, "voldaan"),
            (ReloginRequest.STATUS_CANCELLED, "ingetrokken"),
        )

    def queryset(self, request, qs):
        if self.value() == ReloginRequest.STATUS_PENDING:
            return qs.awaiting_login()
        if self.value() == ReloginRequest.STATUS_ALTS:
            return qs.awaiting_alts()
        if self.value() == ReloginRequest.STATUS_FULFILLED:
            return qs.filter(fulfilled_at__isnull=False, alts_done_at__isnull=False, cancelled_at__isnull=True)
        if self.value() == ReloginRequest.STATUS_CANCELLED:
            return qs.filter(cancelled_at__isnull=False)
        return qs


class CharacterReloginInline(admin.TabularInline):
    model = CharacterRelogin
    extra = 0
    can_delete = False
    readonly_fields = ("character_id", "character_name", "done_at")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ReloginRequest)
class ReloginRequestAdmin(admin.ModelAdmin):
    list_display = (
        "_lid", "status", "requested_at", "_door", "reason", "include_alts",
        "fulfilled_at", "alts_done_at", "cancelled_at",
    )
    list_filter = (StatusFilter, "include_alts")
    search_fields = (
        "user__username", "user__profile__main_character__character_name",
        "requested_by__username", "reason",
    )
    readonly_fields = ("requested_at", "fulfilled_at", "alts_done_at", "cancelled_at", "cancelled_by")
    raw_id_fields = ("user", "requested_by")
    date_hierarchy = "requested_at"
    actions = ("intrekken",)
    inlines = (CharacterReloginInline,)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            "user__profile__main_character", "requested_by__profile__main_character",
        )

    @staticmethod
    def _main(user) -> str:
        if user is None:
            return "–"
        try:
            main = user.profile.main_character
        except Exception:
            main = None
        return main.character_name if main else user.username

    @admin.display(description="lid", ordering="user__profile__main_character__character_name")
    def _lid(self, obj):
        return self._main(obj.user)

    @admin.display(description="door", ordering="requested_by__username")
    def _door(self, obj):
        return self._main(obj.requested_by)

    @admin.action(description="Geselecteerde open verzoeken intrekken")
    def intrekken(self, request, queryset):
        n = queryset.pending().update(cancelled_at=timezone.now(), cancelled_by=request.user)
        invalidate_pending()
        self.message_user(request, f"{n} verzoek(en) ingetrokken.")
