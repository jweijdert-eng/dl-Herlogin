"""ESI-tokens van een account intrekken — zonder het account zelf te slopen.

AA hangt het eigendom van een character aan de tokens: valt het laatste token
met een refresh_token voor een owner hash weg, dan ruimt AA's
`validate_ownership` de `CharacterOwnership` op, en `validate_main_character`
haalt vervolgens de main van het profiel. Het lid zakt daarmee naar Guest,
verliest z'n groepen en vliegt uit Discord. Dat is niet wat "opnieuw inloggen"
hoort te betekenen, dus die opruiming staat tijdens het wissen even uit. Lukt
het loskoppelen niet — AA heeft de receiver hernoemd — dan zet
`_herstel_ownership` achteraf terug wat er weggevallen is.

Wat hier bewust níét gebeurt: een revoke-call naar CCP. Een refresh token is
alleen bruikbaar mét de client-secret van deze installatie, dus de rij hier
weggooien ís het intrekken.

Gevolg voor het lid: elke app die ESI gebruikt (MemberAudit, CorpTools,
Character Scan) staat stil tot het z'n characters opnieuw koppelt. Het
herlogin-rondje van deze plugin vraagt alleen `publicData` en geeft die
toegang dus niet terug; daarvoor stuurt de plugin het lid naar CharLink.

Staat Character Scan erbij, dan gaan de aanmeldingen van dat account terug
naar *Nieuw* (`reset_character_scan`): met ingetrokken toegang hoort een
recruiter er opnieuw naar te kijken.
"""

import logging

from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)


def _snapshot(user) -> tuple:
    """(main_character_id, {character_id: owner_hash}) — het vangnet voor als
    AA's opruim-receiver niet losgekoppeld kon worden."""
    from allianceauth.authentication.models import CharacterOwnership

    eigendom = dict(
        CharacterOwnership.objects.filter(user=user)
        .values_list("character__character_id", "owner_hash")
    )
    try:
        main_id = user.profile.main_character_id
    except Exception:
        main_id = None
    return main_id, eigendom


def _herstel_ownership(user, snapshot) -> int:
    """Zet ownership en main terug die AA bij het wissen heeft opgeruimd."""
    from allianceauth.authentication.models import CharacterOwnership, UserProfile
    from allianceauth.eveonline.models import EveCharacter

    main_id, eigendom = snapshot
    hersteld = 0
    for character_id, owner_hash in eigendom.items():
        char = EveCharacter.objects.filter(character_id=character_id).first()
        if char is None:
            continue
        _rij, nieuw = CharacterOwnership.objects.get_or_create(
            character=char, defaults={"owner_hash": owner_hash, "user": user},
        )
        hersteld += int(nieuw)

    profiel = UserProfile.objects.filter(user=user).first()
    if profiel is not None and main_id and profiel.main_character_id is None:
        profiel.main_character_id = main_id
        profiel.save()  # zet meteen de state terug
        logger.info("Main character van %s teruggezet na het intrekken van tokens", user)
    if hersteld:
        logger.info("%d ownership-rij(en) van %s teruggezet", hersteld, user)
    return hersteld


def revoke_esi_tokens(user) -> int:
    """Gooit alle ESI-tokens van dit account weg en geeft het aantal terug.

    Alleen tokens die op dit account staan; een token van een character dat
    intussen bij iemand anders hoort blijft van die ander.
    """
    from django.db.models.signals import post_delete
    from esi.models import Token

    qs = Token.objects.filter(user=user)
    aantal = qs.count()
    if not aantal:
        return 0

    snapshot = _snapshot(user)
    try:
        from allianceauth.authentication.signals import validate_ownership
        losgekoppeld = post_delete.disconnect(validate_ownership, sender=Token)
    except Exception as fout:  # noqa: BLE001 — AA-intern, nooit het wissen blokkeren
        logger.warning("AA's ownership-opruiming niet kunnen loskoppelen: %s", fout)
        validate_ownership, losgekoppeld = None, False
    if not losgekoppeld:
        logger.warning("Ownership-opruiming van AA staat aan; herstel achteraf.")

    try:
        qs.delete()
    finally:
        if losgekoppeld:
            post_delete.connect(validate_ownership, sender=Token)

    if not losgekoppeld:
        _herstel_ownership(user, snapshot)

    logger.info("%d ESI-token(s) van %s ingetrokken", aantal, user)
    return aantal


def relink_url():
    """Waar het lid z'n characters opnieuw koppelt: CharLink als het er is."""
    try:
        return reverse("charlink:index")
    except NoReverseMatch:
        return None


def reset_character_scan(user, door=None, reden: str = "") -> int:
    """Zet de Character Scan-aanmeldingen van dit account terug op *Nieuw*.

    Hoort bij het intrekken van de tokens: is de toegang eraf, dan moet een
    recruiter er opnieuw naar kijken in plaats van op een oordeel van maanden
    geleden te leunen. Character Scan is een losse plugin — staat die er niet,
    dan gebeurt er niets.

    De aanmelding zelf blijft staan, met haar logboek; alleen de status gaat
    terug en er komt een regel *Heropend* bij, zodat zichtbaar is waarom.
    Geeft terug hoeveel aanmeldingen er heropend zijn.
    """
    from django.apps import apps

    if not apps.is_installed("characterscan"):
        return 0

    from allianceauth.authentication.models import CharacterOwnership
    from characterscan.models import Recruit, RecruitLogEntry

    char_ids = list(
        CharacterOwnership.objects.filter(user=user)
        .values_list("character__character_id", flat=True)
    )
    if not char_ids:
        return 0

    toelichting = _("Herlogin: ESI-tokens ingetrokken, opnieuw beoordelen.")
    if reden:
        toelichting += " " + _("Reden: %(reden)s") % {"reden": reden}

    rijen = list(
        Recruit.objects.filter(eve_character__character_id__in=char_ids)
        .exclude(status=Recruit.Status.NEW)
        .select_related("eve_character")
    )
    for recruit in rijen:
        recruit.status = Recruit.Status.NEW
        recruit.handled_by = None  # niemand heeft 'm meer onder handen
        recruit.save(update_fields=["status", "handled_by", "updated_at"])
        RecruitLogEntry.objects.create(
            recruit=recruit, actor=door, action=RecruitLogEntry.Action.NEW,
            comment=toelichting,
        )
    if rijen:
        logger.info("%d Character Scan-aanmelding(en) van %s heropend", len(rijen), user)
    return len(rijen)
