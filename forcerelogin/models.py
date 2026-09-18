"""
App Models

Eén `ReloginRequest` per keer dat iemand een lid dwingt opnieuw in te loggen.

Het verzoek zelf logt niemand uit. Dat doet `middleware.ForceReloginMiddleware`
bij het eerstvolgende HTTP-verzoek van het lid: elke sessie die **ouder** is
dan het herlogin-verzoek wordt beëindigd. Zo raakt het ook sessies op andere
apparaten en maakt het niet uit welke sessie-backend AA gebruikt (hier
`cached_db`); er hoeft niets in de sessietabel gezocht of gedecodeerd te
worden.

Het inlogtijdstip van een sessie komt uit `signals.stempel_login` (sessiesleutel
`forcerelogin_login_at`). Een sessie zonder stempel is van vóór deze plugin en
telt als "oud".

Een verzoek kent twee fasen. Fase 1: de main logt opnieuw in (`fulfilled_at`).
Fase 2 (alleen met `include_alts`): elke alt van het account moet ook nog een
keer door EVE SSO — AA controleert bij zo'n token opnieuw het eigendom. Tot
dat klaar is (`alts_done_at`) houdt de middleware het lid op de alts-pagina.
Welke alt al geweest is staat in `CharacterRelogin`.
"""

import logging

from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from allianceauth.authentication.models import CharacterOwnership

logger = logging.getLogger(__name__)

# Twee kaarten in één cache-sleutel: fase 1 {user_id: unix-tijd van het jongste
# open verzoek} en fase 2 {user_id: verzoek-id}. Kort gecached zodat de
# middleware niet bij elk verzoek de database raakt; elke wijziging aan een
# verzoek gooit hem weg, dus een forcering werkt meteen.
PENDING_CACHE_KEY = "forcerelogin_pending_v2"
PENDING_CACHE_TTL = 300


class General(models.Model):
    """Meta model voor de app-permissies."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            ("basic_access", _("Can force members to log in again")),
        )


class ReloginRequestQuerySet(models.QuerySet):
    def pending(self):
        """Alles wat nog niet helemaal klaar is: main nog niet ingelogd, óf alts nog open."""
        return self.filter(cancelled_at__isnull=True).filter(
            Q(fulfilled_at__isnull=True) | Q(include_alts=True, alts_done_at__isnull=True)
        )

    def awaiting_login(self):
        """Fase 1: de main moet nog opnieuw inloggen."""
        return self.filter(cancelled_at__isnull=True, fulfilled_at__isnull=True)

    def awaiting_alts(self):
        """Fase 2: main is binnen, alts nog niet allemaal."""
        return self.filter(
            cancelled_at__isnull=True, fulfilled_at__isnull=False,
            include_alts=True, alts_done_at__isnull=True,
        )


class ReloginRequest(models.Model):
    """Eén opdracht 'log opnieuw in' voor één lid, met wie/wanneer/waarom."""

    STATUS_PENDING = "pending"
    STATUS_ALTS = "alts"
    STATUS_FULFILLED = "fulfilled"
    STATUS_CANCELLED = "cancelled"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="forcerelogin_requests", verbose_name=_("lid"),
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+", verbose_name=_("aangevraagd door"),
    )
    requested_at = models.DateTimeField(default=timezone.now, verbose_name=_("aangevraagd op"))
    reason = models.CharField(max_length=200, blank=True, default="", verbose_name=_("reden"))
    include_alts = models.BooleanField(
        default=True, verbose_name=_("alts ook"),
        help_text=_("Ook elke alt van het account moet opnieuw door EVE SSO."),
    )
    fulfilled_at = models.DateTimeField(null=True, blank=True, verbose_name=_("main ingelogd op"))
    alts_done_at = models.DateTimeField(null=True, blank=True, verbose_name=_("alts klaar op"))
    cancelled_at = models.DateTimeField(null=True, blank=True, verbose_name=_("ingetrokken op"))
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+", verbose_name=_("ingetrokken door"),
    )

    objects = ReloginRequestQuerySet.as_manager()

    class Meta:
        default_permissions = ()
        ordering = ("-requested_at",)
        verbose_name = _("herlogin-verzoek")
        verbose_name_plural = _("herlogin-verzoeken")

    def __str__(self) -> str:
        return f"{self.user} @ {self.requested_at:%Y-%m-%d %H:%M} ({self.status})"

    @property
    def status(self) -> str:
        if self.cancelled_at:
            return self.STATUS_CANCELLED
        if not self.fulfilled_at:
            return self.STATUS_PENDING
        if self.include_alts and not self.alts_done_at:
            return self.STATUS_ALTS
        return self.STATUS_FULFILLED

    @property
    def is_pending(self) -> bool:
        return self.status in (self.STATUS_PENDING, self.STATUS_ALTS)

    @property
    def completed_at(self):
        """Wanneer het verzoek écht klaar was: na de alts als die meededen."""
        if self.status != self.STATUS_FULFILLED:
            return None
        return self.alts_done_at if self.include_alts else self.fulfilled_at

    def required_alts(self):
        """De alts van het account op dit moment (ownership minus de main).
        Live, niet als snapshot: een alt die intussen van het account af is
        hoeft niet meer, een nieuwe alt is via SSO binnengekomen en telt mee."""
        try:
            main_id = self.user.profile.main_character_id
        except Exception:
            main_id = None
        return (
            CharacterOwnership.objects.filter(user=self.user)
            .exclude(character_id=main_id)
            .select_related("character")
            .order_by("character__character_name")
        )

    def alts_progress(self):
        """(klaar, totaal, [ (EveCharacter, klaar?) … ]) voor de alts-pagina en de lijst."""
        klaar = set(self.characters.values_list("character_id", flat=True))
        rijen = [(o.character, o.character.character_id in klaar) for o in self.required_alts()]
        return sum(1 for _c, ok in rijen if ok), len(rijen), rijen

    def open_alts(self) -> int:
        klaar, totaal, _rijen = self.alts_progress()
        return totaal - klaar

    def mark_alt_done(self, character) -> bool:
        """Registreert dat deze alt via SSO geweest is; sluit fase 2 af als het
        de laatste was. Geeft terug of het verzoek nu helemaal klaar is."""
        CharacterRelogin.objects.get_or_create(
            request=self, character_id=character.character_id,
            defaults={"character_name": character.character_name},
        )
        if self.open_alts() == 0:
            self.close_alts()
            return True
        return False

    def close_alts(self) -> None:
        """Fase 2 afsluiten; `save()` gooit de cache weg."""
        self.alts_done_at = timezone.now()
        self.save(update_fields=["alts_done_at"])

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        invalidate_pending()

    def delete(self, *args, **kwargs):
        result = super().delete(*args, **kwargs)
        invalidate_pending()
        return result


class CharacterRelogin(models.Model):
    """Eén alt die voor een verzoek opnieuw door SSO is geweest."""

    request = models.ForeignKey(ReloginRequest, on_delete=models.CASCADE, related_name="characters")
    character_id = models.PositiveBigIntegerField()
    character_name = models.CharField(max_length=254, blank=True, default="")
    done_at = models.DateTimeField(default=timezone.now)

    class Meta:
        default_permissions = ()
        unique_together = (("request", "character_id"),)

    def __str__(self) -> str:
        return f"{self.character_name} ({self.character_id}) @ {self.done_at:%Y-%m-%d %H:%M}"


def invalidate_pending() -> None:
    cache.delete(PENDING_CACHE_KEY)


def _maps() -> dict:
    kaarten = cache.get(PENDING_CACHE_KEY)
    if kaarten is None:
        fase1 = {}
        for user_id, at in ReloginRequest.objects.awaiting_login().values_list("user_id", "requested_at"):
            ts = at.timestamp()
            if ts > fase1.get(user_id, 0.0):
                fase1[user_id] = ts
        fase2 = dict(ReloginRequest.objects.awaiting_alts().order_by("requested_at").values_list("user_id", "pk"))
        kaarten = {"login": fase1, "alts": fase2}
        cache.set(PENDING_CACHE_KEY, kaarten, PENDING_CACHE_TTL)
    return kaarten


def pending_map() -> dict:
    """Fase 1: {user_id: unix-tijd van het jongste open verzoek} — één cache-lookup per request."""
    return _maps()["login"]


def alts_map() -> dict:
    """Fase 2: {user_id: verzoek-id} van wie nog alts moet inloggen."""
    return _maps()["alts"]


def force(user, by=None, reason: str = "", include_alts: bool = True):
    """Zet een herlogin-verzoek klaar voor `user`.

    Staat er al een open verzoek, dan komt er geen tweede bij: de middleware
    kijkt toch alleen naar het jongste, en zo blijft de geschiedenis leesbaar.
    Geeft (verzoek, nieuw_aangemaakt) terug.
    """
    bestaand = ReloginRequest.objects.pending().filter(user=user).order_by("-requested_at").first()
    if bestaand:
        return bestaand, False
    verzoek = ReloginRequest.objects.create(
        user=user, requested_by=by, reason=(reason or "")[:200], include_alts=include_alts,
    )
    logger.info("Herlogin geforceerd voor %s door %s (%s, alts=%s)", user, by, reason or "-", include_alts)
    return verzoek, True


def fulfil(user, when=None) -> int:
    """Fase 1 afsluiten: de main is opnieuw ingelogd. Zonder alts (of zonder
    `include_alts`) is het verzoek daarmee meteen helemaal klaar. Geeft het
    aantal afgesloten verzoeken terug."""
    nu = when or timezone.now()
    n = 0
    for verzoek in ReloginRequest.objects.awaiting_login().filter(user=user):
        verzoek.fulfilled_at = nu
        if not verzoek.include_alts or not verzoek.required_alts().exists():
            verzoek.alts_done_at = nu
        verzoek.save(update_fields=["fulfilled_at", "alts_done_at"])
        n += 1
    return n


def cancel(user, by=None) -> int:
    """Trekt alle open verzoeken van `user` in. Geeft het aantal terug."""
    n = ReloginRequest.objects.pending().filter(user=user).update(
        cancelled_at=timezone.now(), cancelled_by=by,
    )
    if n:
        invalidate_pending()
    return n
