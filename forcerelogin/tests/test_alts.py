"""Fase 2: na de main moeten ook alle alts via SSO. Draaien vanuit myauth/:
manage.py test forcerelogin --keepdb --noinput"""

from types import SimpleNamespace

from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory
from django.urls import reverse

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from allianceauth.tests.auth_utils import AuthUtils

from forcerelogin.middleware import NEXT_KEY
from forcerelogin.models import CharacterRelogin, ReloginRequest, cancel, force
from forcerelogin.views import verwerk_alt_token

from .test_forcerelogin import DASHBOARD, Basis, client


def geef_character(user, naam, character_id, owner_hash, corp_id=98000001, corp_name="Testcorp"):
    char = EveCharacter.objects.create(
        character_id=character_id, character_name=naam,
        corporation_id=corp_id, corporation_name=corp_name, corporation_ticker="TST",
    )
    CharacterOwnership.objects.create(user=user, character=char, owner_hash=owner_hash)
    return char


def token(char, owner_hash):
    """Wat django-esi's token_required aan de view geeft, voor zover wij het gebruiken."""
    return SimpleNamespace(
        character_id=char.character_id, character_name=char.character_name,
        character_owner_hash=owner_hash, pk=0,
    )


class AltsBasis(Basis):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # AuthUtils maakt geen ownership voor de main; in het echt is die er wel.
        CharacterOwnership.objects.create(user=cls.lid, character=cls.lid.profile.main_character, owner_hash="h-main")
        cls.alt1 = geef_character(cls.lid, "Alt Een", 9101101, "h-alt1")
        cls.alt2 = geef_character(cls.lid, "Alt Twee", 9101102, "h-alt2")
        cls.vreemd = geef_character(cls.gast, "Vreemde Alt", 9101201, "h-vreemd", corp_id=98000002, corp_name="Andere Corp")

    def request_voor(self, user, pad="/"):
        """RequestFactory-request mét sessie en messages, zoals de view die krijgt."""
        req = RequestFactory(SERVER_NAME="localhost").get(pad)
        req.user = user
        SessionMiddleware(lambda r: None).process_request(req)
        req.session.save()
        MessageMiddleware(lambda r: None).process_request(req)
        return req

    def in_alts_fase(self) -> ReloginRequest:
        """Forceer + main opnieuw ingelogd → verzoek staat in fase 2."""
        verzoek, _nieuw = force(self.lid, by=self.beheer, reason="alts-test")
        c = client()
        c.force_login(self.lid)
        verzoek.refresh_from_db()
        self.assertEqual(verzoek.status, ReloginRequest.STATUS_ALTS)
        return verzoek, c


class FaseTweeTests(AltsBasis):
    def test_login_opent_alts_fase(self):
        verzoek, c = self.in_alts_fase()
        self.assertEqual(verzoek.alts_progress()[:2], (0, 2))

        # AJAX-achtige call (geen text/html): wel omleiden, niet onthouden.
        r = c.get("/notifications/", HTTP_ACCEPT="application/json")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn(NEXT_KEY, c.session)

        r = c.get(DASHBOARD, HTTP_ACCEPT="text/html,*/*")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("forcerelogin:alts"))
        self.assertEqual(c.session[NEXT_KEY], DASHBOARD)

        r = c.get(reverse("forcerelogin:alts"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Alt Een")
        self.assertContains(r, "Alt Twee")
        self.assertContains(r, "alts-test")
        self.assertContains(r, "0 / 2")

    def test_zonder_alts_vlag_meteen_klaar(self):
        verzoek, _nieuw = force(self.lid, include_alts=False)
        c = client()
        c.force_login(self.lid)
        verzoek.refresh_from_db()
        self.assertEqual(verzoek.status, ReloginRequest.STATUS_FULFILLED)
        self.assertEqual(verzoek.completed_at, verzoek.fulfilled_at)
        self.assertEqual(c.get(DASHBOARD).status_code, 200)

    def test_zonder_alts_op_account_meteen_klaar(self):
        verzoek, _nieuw = force(self.lid2)  # lid2 heeft geen ownership-rijen
        c = client()
        c.force_login(self.lid2)
        verzoek.refresh_from_db()
        self.assertEqual(verzoek.status, ReloginRequest.STATUS_FULFILLED)
        self.assertEqual(c.get(DASHBOARD).status_code, 200)

    def test_alts_afronden(self):
        verzoek, c = self.in_alts_fase()
        c.get(DASHBOARD, HTTP_ACCEPT="text/html")  # zet NEXT_KEY

        req = self.request_voor(self.lid)
        req.session[NEXT_KEY] = "/notifications/"
        r = verwerk_alt_token(req, token(self.alt1, "h-alt1"))
        self.assertEqual(r["Location"], reverse("forcerelogin:alts"))
        self.assertTrue(CharacterRelogin.objects.filter(request=verzoek, character_id=self.alt1.character_id).exists())
        verzoek.refresh_from_db()
        self.assertEqual(verzoek.status, ReloginRequest.STATUS_ALTS)
        self.assertEqual(verzoek.alts_progress()[:2], (1, 2))

        r = verwerk_alt_token(req, token(self.alt2, "h-alt2"))
        self.assertEqual(r["Location"], "/notifications/")
        self.assertNotIn(NEXT_KEY, req.session)
        verzoek.refresh_from_db()
        self.assertEqual(verzoek.status, ReloginRequest.STATUS_FULFILLED)
        self.assertEqual(verzoek.completed_at, verzoek.alts_done_at)

        # De sessie van de client mag nu weer overal heen.
        self.assertEqual(c.get(DASHBOARD).status_code, 200)

    def test_verkeerde_hash_telt_niet(self):
        verzoek, _c = self.in_alts_fase()
        req = self.request_voor(self.lid)
        r = verwerk_alt_token(req, token(self.alt1, "andere-hash"))
        self.assertEqual(r["Location"], reverse("forcerelogin:alts"))
        self.assertFalse(CharacterRelogin.objects.filter(request=verzoek).exists())

    def test_vreemd_character_telt_niet(self):
        verzoek, _c = self.in_alts_fase()
        req = self.request_voor(self.lid)
        verwerk_alt_token(req, token(self.vreemd, "h-vreemd"))
        self.assertFalse(CharacterRelogin.objects.filter(request=verzoek).exists())
        self.assertIn("hoort niet bij jouw account", " ".join(str(m) for m in req._messages))

    def test_main_telt_niet_als_alt(self):
        verzoek, _c = self.in_alts_fase()
        req = self.request_voor(self.lid)
        verwerk_alt_token(req, token(self.lid.profile.main_character, "h-main"))
        self.assertFalse(CharacterRelogin.objects.filter(request=verzoek).exists())
        self.assertIn("main", " ".join(str(m) for m in req._messages))

    def test_zonder_open_verzoek(self):
        req = self.request_voor(self.lid)
        r = verwerk_alt_token(req, token(self.alt1, "h-alt1"))
        self.assertEqual(r["Location"], reverse("authentication:dashboard"))
        c = client()
        c.force_login(self.lid)
        r = c.get(reverse("forcerelogin:alts"))
        self.assertEqual(r.status_code, 302)

    def test_intrekken_in_alts_fase(self):
        verzoek, c = self.in_alts_fase()
        cancel(self.lid, by=self.beheer)
        self.assertEqual(c.get(DASHBOARD).status_code, 200)
        verzoek.refresh_from_db()
        self.assertEqual(verzoek.status, ReloginRequest.STATUS_CANCELLED)

    def test_alt_van_account_gehaald(self):
        verzoek, c = self.in_alts_fase()
        verzoek.mark_alt_done(self.alt1)
        # De laatste open alt verdwijnt van het account → niets meer te doen.
        CharacterOwnership.objects.filter(character=self.alt2).delete()
        self.assertEqual(c.get(DASHBOARD).status_code, 200)
        verzoek.refresh_from_db()
        self.assertEqual(verzoek.status, ReloginRequest.STATUS_FULFILLED)

    def test_loginflow_en_alt_login_vrij(self):
        _verzoek, c = self.in_alts_fase()
        r = c.get(reverse("auth_sso_login"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("eveonline", r["Location"])
        r = c.get(reverse("forcerelogin:alt_login"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("eveonline", r["Location"])

    def test_beheerder_houdt_beheerscherm(self):
        CharacterOwnership.objects.create(user=self.beheer, character=self.beheer.profile.main_character, owner_hash="h-b")
        geef_character(self.beheer, "Beheer Alt", 9101301, "h-b1")
        force(self.beheer, by=self.beheer)
        c = client()
        c.force_login(self.beheer)
        self.assertEqual(c.get(reverse("forcerelogin:index")).status_code, 200)
        self.assertEqual(c.get(DASHBOARD).status_code, 302)
        # en kan zichzelf bevrijden
        c.post(reverse("forcerelogin:cancel", args=[self.beheer.pk]))
        self.assertEqual(c.get(DASHBOARD).status_code, 200)

    def test_lid_zonder_permissie_komt_niet_op_beheer(self):
        _verzoek, c = self.in_alts_fase()
        r = c.get(reverse("forcerelogin:index"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("forcerelogin:alts"))


class AltsInBeheerTests(AltsBasis):
    def test_lijst_toont_voortgang(self):
        verzoek, _c = self.in_alts_fase()
        verzoek.mark_alt_done(self.alt1)
        c = client()
        c.force_login(self.beheer)
        r = c.get(reverse("forcerelogin:index") + "?scope=pending")
        self.assertContains(r, "Lid Alfa")
        self.assertContains(r, "alts 1/2")
        self.assertContains(r, "Intrekken")

    def test_vlag_via_knop(self):
        c = client()
        c.force_login(self.beheer)
        c.post(reverse("forcerelogin:force", args=[self.lid.pk]), {"alts": "1"})
        self.assertTrue(ReloginRequest.objects.get(user=self.lid).include_alts)
        c.post(reverse("forcerelogin:force", args=[self.lid2.pk]))
        self.assertFalse(ReloginRequest.objects.get(user=self.lid2).include_alts)

    def test_vlag_via_bulk(self):
        c = client()
        c.force_login(self.beheer)
        c.post(reverse("forcerelogin:bulk"), {"mode": "selected", "user_id": [self.lid.pk], "alts": "1"})
        self.assertTrue(ReloginRequest.objects.get(user=self.lid).include_alts)
        c.post(reverse("forcerelogin:bulk"), {"mode": "selected", "user_id": [self.lid2.pk]})
        self.assertFalse(ReloginRequest.objects.get(user=self.lid2).include_alts)

    def test_notificatie_noemt_alts(self):
        from allianceauth.notifications.models import Notification
        c = client()
        c.force_login(self.beheer)
        c.post(reverse("forcerelogin:force", args=[self.lid.pk]), {"alts": "1"})
        self.assertIn("alts", Notification.objects.filter(user=self.lid).latest("timestamp").message)

    def test_geschiedenis_toont_alts_status(self):
        verzoek, _c = self.in_alts_fase()
        c = client()
        c.force_login(self.beheer)
        r = c.get(reverse("forcerelogin:index"))
        self.assertContains(r, "alts open")
        verzoek.mark_alt_done(self.alt1)
        verzoek.mark_alt_done(self.alt2)
        r = c.get(reverse("forcerelogin:index"))
        self.assertContains(r, "voldaan + alts")
