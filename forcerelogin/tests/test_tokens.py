"""ESI-tokens intrekken bij het forceren. Draaien vanuit myauth/:
manage.py test forcerelogin --keepdb --noinput

De echte AA-signalen staan aan: dat is juist het punt van deze tests. Wie
tokens wist zonder `validate_ownership` tegen te houden, sloopt de ownership
van het lid, en daarmee de main en de state.
"""

from unittest.mock import patch

from django.db.models.signals import post_delete
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

from allianceauth.authentication.models import CharacterOwnership
from esi.models import Token

from forcerelogin.models import ReloginRequest, force
from forcerelogin.signals import RELINK_KEY
from forcerelogin.tokens import relink_url, revoke_esi_tokens

from .test_alts import AltsBasis
from .test_forcerelogin import DASHBOARD, client


def geef_token(user, char, owner_hash) -> Token:
    """Een ESI-token zoals django-esi het na een SSO-rondje opslaat.

    De EveCharacter bestaat al, anders zou AA's `record_character_ownership`
    er eentje via ESI willen ophalen. Owner hash en user gelijk aan de
    bestaande ownership, want alles wat daarvan afwijkt ruimt AA op.
    """
    return Token.objects.create(
        user=user, character_id=char.character_id, character_name=char.character_name,
        character_owner_hash=owner_hash, access_token="access", refresh_token="refresh",
    )


class TokenBasis(AltsBasis):
    def setUp(self):
        super().setUp()
        self.t_main = geef_token(self.lid, self.lid.profile.main_character, "h-main")
        self.t_alt1 = geef_token(self.lid, self.alt1, "h-alt1")
        self.t_alt2 = geef_token(self.lid, self.alt2, "h-alt2")
        self.t_vreemd = geef_token(self.gast, self.vreemd, "h-vreemd")

    def tokens_van(self, user) -> int:
        return Token.objects.filter(user=user).count()

    def ownership_van(self, user) -> int:
        return CharacterOwnership.objects.filter(user=user).count()

    def main_van(self, user):
        user.profile.refresh_from_db()
        return user.profile.main_character_id


class IntrekkenTests(TokenBasis):
    def test_wist_alleen_de_tokens_van_dit_account(self):
        self.assertEqual(revoke_esi_tokens(self.lid), 3)
        self.assertEqual(self.tokens_van(self.lid), 0)
        self.assertEqual(self.tokens_van(self.gast), 1)

    def test_ownership_en_main_blijven_staan(self):
        main_id = self.main_van(self.lid)
        revoke_esi_tokens(self.lid)
        self.assertEqual(self.ownership_van(self.lid), 3)
        self.assertEqual(self.main_van(self.lid), main_id)

    def test_zonder_tokens_gebeurt_er_niets(self):
        Token.objects.filter(user=self.lid).delete()
        self.assertEqual(revoke_esi_tokens(self.lid), 0)

    def test_vangnet_herstelt_wat_aa_opruimt(self):
        """Lukt het loskoppelen niet, dan ruimt AA de ownership op — en zet de
        plugin het daarna terug. Hier nagebootst met een disconnect die faalt."""
        main_id = self.main_van(self.lid)
        with patch.object(post_delete, "disconnect", return_value=False):
            self.assertEqual(revoke_esi_tokens(self.lid), 3)
        self.assertEqual(self.tokens_van(self.lid), 0)
        self.assertEqual(self.ownership_van(self.lid), 3)
        self.assertEqual(self.main_van(self.lid), main_id)


class ForceTests(TokenBasis):
    def test_force_met_tokens(self):
        verzoek, nieuw = force(self.lid, by=self.beheer, revoke_tokens=True)
        self.assertTrue(nieuw)
        self.assertTrue(verzoek.revoke_tokens)
        self.assertEqual(verzoek.tokens_revoked, 3)
        self.assertEqual(verzoek.zojuist_ingetrokken, 3)
        self.assertEqual(self.tokens_van(self.lid), 0)

    def test_force_zonder_tokens_laat_ze_staan(self):
        verzoek, _nieuw = force(self.lid, by=self.beheer)
        self.assertFalse(verzoek.revoke_tokens)
        self.assertEqual(verzoek.zojuist_ingetrokken, 0)
        self.assertEqual(self.tokens_van(self.lid), 3)

    def test_open_verzoek_krijgt_de_tokens_alsnog(self):
        eerste, _nieuw = force(self.lid, by=self.beheer)
        tweede, nieuw = force(self.lid, by=self.beheer, revoke_tokens=True)

        self.assertFalse(nieuw)
        self.assertEqual(tweede.pk, eerste.pk)
        self.assertEqual(self.tokens_van(self.lid), 0)
        eerste.refresh_from_db()
        self.assertTrue(eerste.revoke_tokens)
        self.assertEqual(eerste.tokens_revoked, 3)

    def test_tweede_ronde_telt_niet_dubbel(self):
        force(self.lid, by=self.beheer, revoke_tokens=True)
        verzoek, _nieuw = force(self.lid, by=self.beheer, revoke_tokens=True)
        self.assertEqual(verzoek.zojuist_ingetrokken, 0)
        self.assertEqual(verzoek.tokens_revoked, 3)


class ViewTests(TokenBasis):
    def beheerder(self):
        c = client()
        c.force_login(self.beheer)
        return c

    def test_vinkje_aan_trekt_tokens_in(self):
        c = self.beheerder()
        r = c.post(reverse("forcerelogin:force", args=[self.lid.pk]), {"alts": "1", "tokens": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.tokens_van(self.lid), 0)
        self.assertTrue(ReloginRequest.objects.get(user=self.lid).revoke_tokens)

    def test_vinkje_uit_laat_tokens_staan(self):
        c = self.beheerder()
        c.post(reverse("forcerelogin:force", args=[self.lid.pk]), {"alts": "1"})
        self.assertEqual(self.tokens_van(self.lid), 3)
        self.assertFalse(ReloginRequest.objects.get(user=self.lid).revoke_tokens)

    def test_bulk_met_vinkje(self):
        c = self.beheerder()
        r = c.post(reverse("forcerelogin:bulk"), {
            "mode": "selected", "user_id": [str(self.lid.pk)], "alts": "1", "tokens": "1",
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.tokens_van(self.lid), 0)
        self.assertEqual(self.tokens_van(self.gast), 1)

    def test_lijst_toont_tokens_badge(self):
        force(self.lid, by=self.beheer, revoke_tokens=True)
        r = self.beheerder().get(reverse("forcerelogin:index"))
        self.assertContains(r, "fr-badge-tokens")


class RelinkTests(TokenBasis):
    """Na afloop één keer naar CharLink, want de herlogin geeft alleen publicData terug."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.charlink = reverse("charlink:index")
        except NoReverseMatch:
            cls.charlink = None

    def setUp(self):
        super().setUp()
        if self.charlink is None:
            self.skipTest("CharLink niet geïnstalleerd")

    def test_na_login_zonder_alts(self):
        force(self.lid, by=self.beheer, include_alts=False, revoke_tokens=True)
        c = client()
        c.force_login(self.lid)
        self.assertTrue(c.session.get(RELINK_KEY))

        r = c.get(DASHBOARD, HTTP_ACCEPT="text/html,*/*")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], self.charlink)
        # Eén duwtje, geen gijzeling: de volgende pagina komt gewoon door.
        self.assertEqual(c.get(DASHBOARD, HTTP_ACCEPT="text/html,*/*").status_code, 200)

    def test_achtergrondcall_verbruikt_het_duwtje_niet(self):
        force(self.lid, by=self.beheer, include_alts=False, revoke_tokens=True)
        c = client()
        c.force_login(self.lid)

        r = c.get("/notifications/", HTTP_ACCEPT="application/json")
        self.assertNotEqual(r.status_code, 302)
        self.assertTrue(c.session.get(RELINK_KEY))

    def test_zonder_tokens_geen_omleiding(self):
        force(self.lid, by=self.beheer, include_alts=False)
        c = client()
        c.force_login(self.lid)
        self.assertIsNone(c.session.get(RELINK_KEY))
        self.assertEqual(c.get(DASHBOARD, HTTP_ACCEPT="text/html,*/*").status_code, 200)

    def test_na_de_laatste_alt(self):
        from types import SimpleNamespace

        from forcerelogin.views import verwerk_alt_token

        self.in_alts_fase_met_tokens()
        antwoord = None
        for char, hash_ in ((self.alt1, "h-alt1"), (self.alt2, "h-alt2")):
            req = self.request_voor(self.lid)
            antwoord = verwerk_alt_token(req, SimpleNamespace(
                character_id=char.character_id, character_name=char.character_name,
                character_owner_hash=hash_, pk=0,
            ))
        self.assertEqual(antwoord["Location"], self.charlink)

    def in_alts_fase_met_tokens(self):
        verzoek, _nieuw = force(self.lid, by=self.beheer, revoke_tokens=True)
        c = client()
        c.force_login(self.lid)
        verzoek.refresh_from_db()
        self.assertEqual(verzoek.status, ReloginRequest.STATUS_ALTS)
        return verzoek, c


class RelinkUrlTests(TestCase):
    def test_url_of_niets(self):
        doel = relink_url()
        self.assertTrue(doel is None or doel.startswith("/"))


class CharacterScanTests(TokenBasis):
    """Met ingetrokken toegang hoort een recruiter opnieuw naar de aanmelding
    te kijken, dus gaat die terug naar Nieuw. Character Scan is optioneel —
    staat de plugin er niet, dan slaan we deze tests over."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django.apps import apps
        cls.scan_aanwezig = apps.is_installed("characterscan")

    def setUp(self):
        super().setUp()
        if not self.scan_aanwezig:
            self.skipTest("Character Scan niet geinstalleerd")
        from characterscan.models import Recruit
        self.Recruit = Recruit
        self.r_main = Recruit.objects.create(
            eve_character=self.lid.profile.main_character,
            status=Recruit.Status.ACCEPTED, handled_by=self.beheer,
        )
        self.r_alt = Recruit.objects.create(
            eve_character=self.alt1, status=Recruit.Status.REJECTED, handled_by=self.beheer,
        )
        self.r_vreemd = Recruit.objects.create(
            eve_character=self.vreemd, status=Recruit.Status.ACCEPTED, handled_by=self.beheer,
        )

    def status_van(self, recruit) -> str:
        recruit.refresh_from_db()
        return recruit.status

    def test_aanmeldingen_gaan_terug_naar_nieuw(self):
        verzoek, _nieuw = force(self.lid, by=self.beheer, reason="spionageverdenking", revoke_tokens=True)

        self.assertEqual(verzoek.scan_heropend, 2)
        self.assertEqual(self.status_van(self.r_main), self.Recruit.Status.NEW)
        self.assertEqual(self.status_van(self.r_alt), self.Recruit.Status.NEW)
        self.r_main.refresh_from_db()
        self.assertIsNone(self.r_main.handled_by)

    def test_andere_accounts_blijven_met_rust(self):
        force(self.lid, by=self.beheer, revoke_tokens=True)
        self.assertEqual(self.status_van(self.r_vreemd), self.Recruit.Status.ACCEPTED)

    def test_logregel_vertelt_waarom(self):
        force(self.lid, by=self.beheer, reason="spionageverdenking", revoke_tokens=True)

        regel = self.r_main.log_entries.first()
        self.assertEqual(regel.action, "new")
        self.assertEqual(regel.actor, self.beheer)
        self.assertIn("ESI-tokens ingetrokken", regel.comment)
        self.assertIn("spionageverdenking", regel.comment)

    def test_zonder_tokens_verandert_er_niets(self):
        verzoek, _nieuw = force(self.lid, by=self.beheer)
        self.assertEqual(verzoek.scan_heropend, 0)
        self.assertEqual(self.status_van(self.r_main), self.Recruit.Status.ACCEPTED)

    def test_al_nieuw_krijgt_geen_tweede_logregel(self):
        self.r_main.status = self.Recruit.Status.NEW
        self.r_main.save(update_fields=["status"])

        verzoek, _nieuw = force(self.lid, by=self.beheer, revoke_tokens=True)

        self.assertEqual(verzoek.scan_heropend, 1)  # alleen de alt
        self.assertEqual(self.r_main.log_entries.count(), 0)
