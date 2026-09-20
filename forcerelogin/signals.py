"""Stempelt elke login met een tijdstip en sluit open herlogin-verzoeken af.

`user_logged_in` vuurt bij elke `django.contrib.auth.login()` — dus bij AA's
EVE-SSO-login, bij een admin-login én bij `Client.force_login` in tests.
"""

import logging
import time

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from .models import ReloginRequest, fulfil

logger = logging.getLogger(__name__)

# Sessiesleutel met het inlogtijdstip (unix-tijd). Sessies zonder deze sleutel
# stammen van vóór de plugin en gelden als ouder dan elk verzoek.
SESSION_KEY = "forcerelogin_login_at"

# Staat aan zodra een verzoek mét ingetrokken tokens helemaal klaar is: het lid
# moet z'n characters nog opnieuw koppelen. De middleware stuurt het daarna één
# keer naar CharLink.
RELINK_KEY = "forcerelogin_relink"


@receiver(user_logged_in, dispatch_uid="forcerelogin_stempel_login")
def stempel_login(sender, request=None, user=None, **kwargs):
    sessie = getattr(request, "session", None)
    if sessie is not None:
        sessie[SESSION_KEY] = time.time()
    if user is None:
        return
    try:
        afgesloten = fulfil(user)
    except Exception as fout:  # nooit een login laten klappen op onze administratie
        logger.warning("Herlogin-verzoek van %s niet kunnen afsluiten: %s", user, fout)
        return
    # Alleen als het verzoek híér al helemaal klaar is (geen alts meer te gaan);
    # anders regelt de alts-pagina het doorsturen naar CharLink.
    if sessie is not None and any(
        v.revoke_tokens and v.status == ReloginRequest.STATUS_FULFILLED for v in afgesloten
    ):
        sessie[RELINK_KEY] = True
