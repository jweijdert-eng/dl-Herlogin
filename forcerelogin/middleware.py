"""Beëindigt sessies die ouder zijn dan een open herlogin-verzoek, en houdt
daarna het lid op de alts-pagina tot alle alts opnieuw door SSO zijn geweest.

Hoort ná `AuthenticationMiddleware` en `MessageMiddleware` — dus gewoon
achteraan in `MIDDLEWARE`:

    MIDDLEWARE += ['forcerelogin.middleware.ForceReloginMiddleware']

De login-flow zelf (`/account/login/`, alles onder `/sso/`, uitloggen) wordt
nooit onderbroken; anders zou een lid dat al bezig is met opnieuw inloggen
halverwege teruggestuurd worden. Wie zelf Herlogin mag beheren houdt tijdens
de alts-fase toegang tot het beheerscherm, zodat een beheerder die zichzelf
geforceerd heeft het verzoek altijd nog kan intrekken.
"""

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect, resolve_url
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext as _

from .models import ReloginRequest, alts_map, fulfil, pending_map
from .signals import SESSION_KEY

logger = logging.getLogger(__name__)

# Waar het lid heen wilde vóór de alts-pagina ertussen kwam.
NEXT_KEY = "forcerelogin_next"


class ForceReloginMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        # Pas bij het eerste verzoek bepaald, dan is de URLconf zeker geladen.
        self._paden = None

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return self.get_response(request)

        paden = self._resolve()
        pad = request.path
        if pad in paden["login_vrij"] or pad.startswith(paden["prefix"]):
            return self.get_response(request)

        try:
            sinds = pending_map().get(user.pk)
            alts_verzoek = None if sinds is not None else alts_map().get(user.pk)
        except Exception as fout:
            # Dit draait op elk verzoek: nooit de hele site laten vallen als de
            # tabel nog niet gemigreerd is of de cache plat ligt.
            logger.warning("Herlogin-verzoeken niet te lezen, sla over: %s", fout)
            return self.get_response(request)

        if sinds is not None:
            return self._fase_login(request, user, sinds)
        if alts_verzoek is not None:
            return self._fase_alts(request, user, alts_verzoek, paden)
        return self.get_response(request)

    def _fase_login(self, request, user, sinds):
        ingelogd = request.session.get(SESSION_KEY)
        if ingelogd is not None and ingelogd >= sinds:
            # Deze sessie is jonger dan het verzoek: het lid heeft al opnieuw
            # ingelogd. Vangnet voor als het login-signaal niet gelopen heeft.
            fulfil(user)
            return self.get_response(request)

        logger.info("Herlogin afgedwongen voor %s op %s", user, request.path)
        logout(request)
        messages.warning(request, _(
            "Je bent uitgelogd door een beheerder. Log opnieuw in om verder te gaan."
        ))
        return redirect_to_login(request.get_full_path())

    def _fase_alts(self, request, user, verzoek_id, paden):
        pad = request.path
        if pad in paden["alts_vrij"]:
            return self.get_response(request)
        if pad.startswith(paden["beheer_prefix"]) and user.has_perm("forcerelogin.basic_access"):
            return self.get_response(request)

        try:
            verzoek = ReloginRequest.objects.get(pk=verzoek_id, user=user)
        except ReloginRequest.DoesNotExist:
            return self.get_response(request)
        if verzoek.open_alts() == 0:
            # Alles al binnen (bijv. de laatste alt is van het account gehaald).
            verzoek.close_alts()
            return self.get_response(request)

        # Alleen echte paginanavigatie onthouden als terugkeeradres — niet de
        # AJAX-calls die AA's eigen JS op de achtergrond doet (notificatie-teller).
        navigatie = request.method == "GET" and "text/html" in request.headers.get("Accept", "")
        if navigatie and NEXT_KEY not in request.session:
            request.session[NEXT_KEY] = request.get_full_path()
        return redirect(paden["alts_pagina"])

    def _resolve(self) -> dict:
        if self._paden is None:
            login_vrij, prefix = set(), ()
            try:
                login_vrij.add(resolve_url(settings.LOGIN_URL))
            except NoReverseMatch:
                pass
            try:
                login_vrij.add(reverse("logout"))
            except NoReverseMatch:
                pass
            try:
                # /sso/login → alles onder /sso/ (ook de callback van django-esi)
                sso = reverse("auth_sso_login")
                prefix = (sso.rsplit("/", 1)[0] + "/",)
            except NoReverseMatch:
                pass
            alts_pagina = reverse("forcerelogin:alts")
            self._paden = {
                "login_vrij": login_vrij,
                "prefix": prefix,
                "alts_pagina": alts_pagina,
                "alts_vrij": {alts_pagina, reverse("forcerelogin:alt_login")},
                "beheer_prefix": reverse("forcerelogin:index"),
            }
        return self._paden
