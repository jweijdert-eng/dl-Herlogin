"""App Views"""

import logging
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.models import Permission
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from allianceauth.authentication.models import CharacterOwnership, UserProfile
from allianceauth.notifications import notify
from esi.decorators import token_required
from esi.models import Token

from . import __version__
from .middleware import NEXT_KEY
from .models import ReloginRequest, cancel, force
from .tokens import relink_url

logger = logging.getLogger(__name__)
User = get_user_model()

SCOPES = [
    {"key": "pending", "label": "Wachtend", "color": "warning"},
    {"key": "all", "label": "Alle accounts", "color": "secondary"},
]
DEFAULT_SCOPE = "all"
HISTORY_LIMIT = 50


def _matches(row, needle: str) -> bool:
    """Zoekt op main, corp, alliance, username én de namen van de alts —
    je kent vaak alleen het character waar iemand mee vloog."""
    haystack = " ".join([
        row["main_name"], row["corp_name"], row["alliance_name"],
        row["username"], *row["char_names"],
    ]).lower()
    return all(deel in haystack for deel in needle.lower().split())


def admin_ids() -> set:
    """Accounts die bij bulk overgeslagen worden: superuser, staff en iedereen
    met de Herlogin-permissie zelf — via user, groep óf AA-state (AA's
    StateBackend telt state-permissies mee in `has_perm`)."""
    try:
        perm = Permission.objects.get(content_type__app_label="forcerelogin", codename="basic_access")
    except Permission.DoesNotExist:
        perm = None
    filt = Q(is_superuser=True) | Q(is_staff=True)
    if perm is not None:
        filt |= Q(user_permissions=perm) | Q(groups__permissions=perm) | Q(profile__state__permissions=perm)
    return set(User.objects.filter(filt).values_list("pk", flat=True))


def corp_choices() -> list:
    """[(corporation_id, naam)] van alle mains, op naam gesorteerd."""
    paren = (
        UserProfile.objects.filter(main_character__isnull=False, user__is_active=True)
        .values_list("main_character__corporation_id", "main_character__corporation_name")
        .distinct()
    )
    return sorted(set(paren), key=lambda c: (c[1] or "").lower())


def build_rows(scope: str, q: str = "", corp: int = None) -> list:
    """Eén rij per AA-account met een main character."""
    profielen = (
        UserProfile.objects.filter(main_character__isnull=False, user__is_active=True)
        .select_related("user", "main_character", "state")
    )
    if corp:
        profielen = profielen.filter(main_character__corporation_id=corp)
    user_ids = [p.user_id for p in profielen]
    admins = admin_ids()

    open_per_user = {
        r.user_id: r
        for r in ReloginRequest.objects.pending().filter(user_id__in=user_ids)
        .select_related("requested_by__profile__main_character")
        .order_by("requested_at")  # jongste wint bij dubbelen
    }

    alts = {}
    for uid, naam in CharacterOwnership.objects.filter(user_id__in=user_ids).values_list(
        "user_id", "character__character_name"
    ):
        alts.setdefault(uid, []).append(naam)

    rows = []
    for p in profielen:
        main = p.main_character
        rij = {
            "user_id": p.user_id,
            "username": p.user.username,
            "main_name": main.character_name,
            "character_id": main.character_id,
            "corp_name": main.corporation_name or "",
            "alliance_name": main.alliance_name or "",
            "state": p.state.name if p.state else "",
            "last_login": p.user.last_login,
            "char_names": alts.get(p.user_id, []),
            "n_chars": len(alts.get(p.user_id, [])),
            "pending": open_per_user.get(p.user_id),
            "is_admin": p.user_id in admins,
        }
        if rij["pending"] and rij["pending"].status == ReloginRequest.STATUS_ALTS:
            klaar, totaal, _rijen = rij["pending"].alts_progress()
            rij["alts_done"], rij["alts_total"] = klaar, totaal
        if scope == "pending" and not rij["pending"]:
            continue
        if q and not _matches(rij, q):
            continue
        rows.append(rij)

    # Wie nog moet herloggen bovenaan, daarna op naam.
    rows.sort(key=lambda r: (r["pending"] is None, r["main_name"].lower()))
    return rows


def _terug(request):
    """Terug naar waar de knop stond (filters blijven staan), anders het overzicht."""
    volgende = request.POST.get("next", "")
    if volgende and url_has_allowed_host_and_scheme(
        volgende, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(volgende)
    return redirect("forcerelogin:index")


def _alts_vlag(request) -> bool:
    """Checkbox 'incl. alts': staat standaard aan in de formulieren; een
    niet-aangevinkte checkbox komt niet mee in de POST."""
    return request.POST.get("alts") == "1"


def _tokens_vlag(request) -> bool:
    """Checkbox 'tokens intrekken': staat standaard úít — het lid moet daarna
    al z'n characters opnieuw koppelen in CharLink."""
    return request.POST.get("tokens") == "1"


def _scan_melding(n: int) -> str:
    """Character Scan is optioneel; alleen iets zeggen als er echt iets veranderd is."""
    if not n:
        return ""
    return " " + _("%(n)d Character Scan-aanmelding(en) terug naar Nieuw.") % {"n": n}


def _naam(user) -> str:
    try:
        main = user.profile.main_character
    except Exception:
        main = None
    return main.character_name if main else user.username


@login_required
@permission_required("forcerelogin.basic_access")
def index(request):
    scope = request.GET.get("scope", DEFAULT_SCOPE)
    if scope not in {s["key"] for s in SCOPES}:
        scope = DEFAULT_SCOPE
    q = request.GET.get("q", "").strip()
    corp = request.GET.get("corp", "")
    corp = int(corp) if corp.isdigit() else None

    rows = build_rows(scope, q, corp)
    corps = corp_choices()
    corp_name = next((naam for cid, naam in corps if cid == corp), "")
    # Wat 'Forceer alle getoonde' zou raken: open verzoeken en admins niet.
    n_forceable = sum(1 for r in rows if not r["pending"] and not r["is_admin"])
    n_accounts = UserProfile.objects.filter(main_character__isnull=False, user__is_active=True).count()
    n_pending = ReloginRequest.objects.pending().values("user_id").distinct().count()
    n_fulfilled = ReloginRequest.objects.filter(
        fulfilled_at__gte=timezone.now() - timedelta(days=30)
    ).count()

    history = (
        ReloginRequest.objects.select_related(
            "user__profile__main_character",
            "requested_by__profile__main_character",
            "cancelled_by__profile__main_character",
        )[:HISTORY_LIMIT]
    )

    return render(request, "forcerelogin/index.html", {
        "versie": __version__,
        "rows": rows,
        "scopes": SCOPES,
        "scope": scope,
        "q": q,
        "corp_id": corp,
        "corp_name": corp_name,
        "corps": corps,
        "n_forceable": n_forceable,
        "n_accounts": n_accounts,
        "n_pending": n_pending,
        "n_fulfilled": n_fulfilled,
        "history": history,
    })


@login_required
@permission_required("forcerelogin.basic_access")
@require_POST
def force_user(request, user_id: int):
    target = get_object_or_404(User, pk=user_id, is_active=True)
    reason = request.POST.get("reason", "").strip()[:200]

    alts = _alts_vlag(request)
    tokens = _tokens_vlag(request)
    verzoek, nieuw = force(target, by=request.user, reason=reason, include_alts=alts, revoke_tokens=tokens)
    naam = _naam(target)
    if not nieuw:
        melding = _("%(naam)s moest al opnieuw inloggen; verzoek stond al open.") % {"naam": naam}
        if tokens:
            melding += " " + _("De ESI-tokens zijn alsnog ingetrokken (%(n)d).") % {"n": verzoek.zojuist_ingetrokken}
            melding += _scan_melding(verzoek.scan_heropend)
        messages.info(request, melding)
        return _terug(request)

    _notify(target, reason, alts, tokens)
    melding = _("%(naam)s wordt bij het eerstvolgende bezoek uitgelogd en moet opnieuw inloggen.") % {"naam": naam}
    if tokens:
        melding += " " + _("%(n)d ESI-token(s) ingetrokken.") % {"n": verzoek.zojuist_ingetrokken}
        melding += _scan_melding(verzoek.scan_heropend)
    messages.success(request, melding)
    return _terug(request)


@login_required
@permission_required("forcerelogin.basic_access")
@require_POST
def cancel_user(request, user_id: int):
    target = get_object_or_404(User, pk=user_id)
    n = cancel(target, by=request.user)
    naam = _naam(target)
    if n:
        messages.success(request, _("Herlogin-verzoek voor %(naam)s ingetrokken.") % {"naam": naam})
    else:
        messages.info(request, _("Er stond geen open verzoek voor %(naam)s.") % {"naam": naam})
    return _terug(request)


def _notify(target, reason: str, alts: bool, tokens: bool = False) -> None:
    tekst = _("Een beheerder heeft je gevraagd opnieuw in te loggen op Auth.")
    if alts:
        tekst += " " + _("Daarna moet je ook al je alts één keer via EVE SSO inloggen.")
    if tokens:
        tekst += " " + _(
            "Je ESI-tokens zijn ingetrokken: koppel daarna al je characters opnieuw in CharLink, "
            "anders staan Member Audit en de andere apps stil."
        )
    if reason:
        tekst += " " + _("Reden: %(reden)s") % {"reden": reason}
    try:
        notify(user=target, title=_("Opnieuw inloggen vereist"), message=tekst, level="warning")
    except Exception as fout:  # een notificatie is bijzaak
        logger.warning("Notificatie voor %s mislukt: %s", target, fout)


@login_required
@permission_required("forcerelogin.basic_access")
@require_POST
def force_bulk(request):
    """Meerdere leden tegelijk: de aangevinkte rijen (`mode=selected`) of alles
    wat het huidige filter toont (`mode=shown` — met een corp-filter is dat de
    hele corp). Admins en wie al een open verzoek heeft worden overgeslagen."""
    mode = request.POST.get("mode", "")
    reason = request.POST.get("reason", "").strip()[:200]
    alts = _alts_vlag(request)
    tokens = _tokens_vlag(request)

    if mode == "selected":
        ids = {int(x) for x in request.POST.getlist("user_id") if x.isdigit()}
    elif mode == "shown":
        corp = request.POST.get("corp", "")
        rows = build_rows(
            request.POST.get("scope", DEFAULT_SCOPE), request.POST.get("q", "").strip(),
            int(corp) if corp.isdigit() else None,
        )
        ids = {r["user_id"] for r in rows}
    else:
        messages.error(request, _("Onbekende bulk-actie."))
        return _terug(request)

    if not ids:
        messages.info(request, _("Geen leden geselecteerd."))
        return _terug(request)

    admins = admin_ids() | {request.user.pk}
    doelen = User.objects.filter(pk__in=ids, is_active=True, profile__main_character__isnull=False)

    nieuw, al_open, overgeslagen, weg, heropend = 0, 0, 0, 0, 0
    for target in doelen:
        if target.pk in admins:
            overgeslagen += 1
            continue
        verzoek, created = force(target, by=request.user, reason=reason, include_alts=alts, revoke_tokens=tokens)
        if created:
            nieuw += 1
            _notify(target, reason, alts, tokens)
        else:
            al_open += 1
        if tokens:
            weg += verzoek.zojuist_ingetrokken
            heropend += verzoek.scan_heropend

    delen = [_("%(n)d lid/leden moeten opnieuw inloggen.") % {"n": nieuw}]
    if tokens:
        delen.append(_("%(n)d ESI-token(s) ingetrokken.") % {"n": weg})
        if heropend:
            delen.append(_("%(n)d Character Scan-aanmelding(en) terug naar Nieuw.") % {"n": heropend})
    if al_open:
        delen.append(_("%(n)d stond(en) al open.") % {"n": al_open})
    if overgeslagen:
        delen.append(_("%(n)d admin(s) overgeslagen.") % {"n": overgeslagen})
    (messages.success if nieuw else messages.info)(request, " ".join(delen))
    return _terug(request)


def _klaar_redirect(request, verzoek=None):
    """Waar het lid heen mag als alles klaar is: terug naar waar het heen
    wilde, maar met ingetrokken tokens eerst langs CharLink — de herlogin
    geeft alleen `publicData` terug, de apps moeten opnieuw gekoppeld."""
    volgende = request.session.pop(NEXT_KEY, None)
    if verzoek is not None and verzoek.revoke_tokens:
        doel = relink_url()
        if doel:
            messages.info(request, _(
                "Je ESI-tokens zijn ingetrokken. Koppel je characters hier opnieuw."
            ))
            return redirect(doel)
    return redirect(volgende or "authentication:dashboard")


@login_required
def alts(request):
    """Waar het lid tijdens fase 2 op wordt vastgehouden: welke alts nog moeten."""
    verzoek = ReloginRequest.objects.awaiting_alts().filter(user=request.user).order_by("-requested_at").first()
    if verzoek is None:
        messages.info(request, _("Je hoeft geen alts opnieuw in te loggen."))
        return _klaar_redirect(request)

    klaar, totaal, rijen = verzoek.alts_progress()
    if totaal - klaar == 0:
        verzoek.close_alts()
        messages.success(request, _("Al je alts zijn opnieuw ingelogd. Bedankt!"))
        return _klaar_redirect(request, verzoek)

    try:
        main = request.user.profile.main_character
    except Exception:
        main = None
    return render(request, "forcerelogin/alts.html", {
        "versie": __version__,
        "verzoek": verzoek,
        "main": main,
        "rijen": rijen,
        "klaar": klaar,
        "totaal": totaal,
    })


def verwerk_alt_token(request, token):
    """Eén alt is via SSO teruggekomen. AA heeft bij het opslaan van het token
    het eigendom al opnieuw beoordeeld (`record_character_ownership`), dus
    hier hoeft alleen gecheckt te worden dat het character nu bij dit account
    hoort en of het een alt is die nog openstond."""
    verzoek = ReloginRequest.objects.awaiting_alts().filter(user=request.user).order_by("-requested_at").first()
    if verzoek is None:
        messages.info(request, _("Er staat geen alts-herlogin voor je open."))
        return redirect("authentication:dashboard")

    eigen = CharacterOwnership.objects.filter(
        user=request.user, character__character_id=token.character_id,
        owner_hash=token.character_owner_hash,
    ).select_related("character").first()
    if eigen is None:
        messages.error(request, _(
            "%(naam)s hoort niet bij jouw account. Log in met een van je eigen alts."
        ) % {"naam": token.character_name})
        return redirect("forcerelogin:alts")

    try:
        is_main = request.user.profile.main_character_id == eigen.character_id
    except Exception:
        is_main = False
    if is_main:
        messages.info(request, _("Dat is je main; die is al ingelogd. Kies een alt."))
        return redirect("forcerelogin:alts")

    alles_klaar = verzoek.mark_alt_done(eigen.character)
    if alles_klaar:
        messages.success(request, _("%(naam)s ingelogd — al je alts zijn nu geweest. Bedankt!") % {
            "naam": eigen.character.character_name,
        })
        return _klaar_redirect(request, verzoek)

    messages.success(request, _("%(naam)s ingelogd, nog %(n)d te gaan.") % {
        "naam": eigen.character.character_name, "n": verzoek.open_alts(),
    })
    return redirect("forcerelogin:alts")


@login_required
@token_required(new=True, scopes=settings.LOGIN_TOKEN_SCOPES)
def alt_login(request, token):
    """SSO-rondje voor één alt. `new=True`: altijd écht naar EVE, geen
    bestaand token kiezen — daar gaat het juist om."""
    try:
        return verwerk_alt_token(request, token)
    finally:
        # Net als AA's sso_login: geen dubbel token bewaren als er al een
        # geldig gelijkwaardig token ligt.
        try:
            if Token.objects.exclude(pk=token.pk).equivalent_to(token).require_valid().exists():
                token.delete()
        except Exception as fout:
            logger.warning("Token-opruiming na alt-login mislukt: %s", fout)
