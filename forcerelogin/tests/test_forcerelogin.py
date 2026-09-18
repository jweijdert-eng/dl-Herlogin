"""Draaien vanuit myauth/: manage.py test forcerelogin --keepdb --noinput

De middleware staat in de echte MIDDLEWARE (local.py), dus de test-client
loopt door precies dezelfde keten als een browser. HTTP_HOST=localhost omdat
ALLOWED_HOSTS dat alleen toestaat.
"""

import time

from django.test import Client, TestCase
from django.urls import reverse

from allianceauth.notifications.models import Notification
from allianceauth.tests.auth_utils import AuthUtils

from forcerelogin.models import ReloginRequest, cancel, force, fulfil, invalidate_pending, pending_map
from forcerelogin.signals import SESSION_KEY

DASHBOARD = "/dashboard/"


def client() -> Client:
    return Client(HTTP_HOST="localhost")


class Basis(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.lid = AuthUtils.create_user("fr_lid")
        AuthUtils.add_main_character_2(cls.lid, "Lid Alfa", 9101001, corp_id=98000001, corp_name="Testcorp")
        cls.beheer = AuthUtils.create_user("fr_beheer")
        AuthUtils.add_main_character_2(cls.beheer, "Beheer Bravo", 9101002, corp_id=98000001, corp_name="Testcorp")
        AuthUtils.add_permission_to_user_by_name("forcerelogin.basic_access", cls.beheer)
        cls.gast = AuthUtils.create_user("fr_gast")
        AuthUtils.add_main_character_2(cls.gast, "Gast Charlie", 9101003, corp_id=98000002, corp_name="Andere Corp")
        cls.lid2 = AuthUtils.create_user("fr_lid2")
        AuthUtils.add_main_character_2(cls.lid2, "Lid Delta", 9101004, corp_id=98000001, corp_name="Testcorp")
        # Staff zonder Herlogin-permissie: telt als admin, dus bulk slaat 'm over.
        cls.staf = AuthUtils.create_user("fr_staf")
        AuthUtils.add_main_character_2(cls.staf, "Staf Echo", 9101005, corp_id=98000001, corp_name="Testcorp")
        cls.staf.is_staff = True
        cls.staf.save()

    def setUp(self):
        invalidate_pending()

    @staticmethod
    def veroudert(c: Client, seconden: float = 100) -> None:
        """Zet de inlogstempel van de client-sessie terug in de tijd."""
        sessie = c.session
        sessie[SESSION_KEY] = time.time() - seconden
        sessie.save()

    @staticmethod
    def ingelogd(c: Client) -> bool:
        return "_auth_user_id" in c.session


class MiddlewareTests(Basis):
    def test_login_krijgt_stempel(self):
        c = client()
        c.force_login(self.lid)
        self.assertIn(SESSION_KEY, c.session)
        self.assertEqual(c.get(DASHBOARD).status_code, 200)

    def test_oude_sessie_wordt_beeindigd(self):
        c = client()
        c.force_login(self.lid)
        self.veroudert(c)

        force(self.lid, by=self.beheer, reason="test")
        r = c.get(DASHBOARD)

        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("auth_login_user"), r["Location"])
        self.assertIn("next=", r["Location"])
        self.assertFalse(self.ingelogd(c))
        # Nog niet voldaan: het lid heeft alleen nog maar de deur gezien.
        self.assertTrue(ReloginRequest.objects.get(user=self.lid).is_pending)

    def test_melding_op_loginpagina(self):
        c = client()
        c.force_login(self.lid)
        self.veroudert(c)
        force(self.lid)
        c.get(DASHBOARD)

        r = c.get(reverse("auth_login_user"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "uitgelogd door een beheerder")

    def test_sessie_zonder_stempel_geldt_als_oud(self):
        c = client()
        c.force_login(self.lid)
        sessie = c.session
        del sessie[SESSION_KEY]
        sessie.save()

        force(self.lid)
        self.assertEqual(c.get(DASHBOARD).status_code, 302)
        self.assertFalse(self.ingelogd(c))

    def test_opnieuw_inloggen_sluit_verzoek_af(self):
        verzoek, _nieuw = force(self.lid, by=self.beheer)
        time.sleep(0.01)

        c = client()
        c.force_login(self.lid)

        verzoek.refresh_from_db()
        self.assertEqual(verzoek.status, ReloginRequest.STATUS_FULFILLED)
        self.assertEqual(c.get(DASHBOARD).status_code, 200)
        self.assertNotIn(self.lid.pk, pending_map())

    def test_jongere_sessie_blijft_en_sluit_af(self):
        # Vangnet: het verzoek is ouder dan de sessie maar staat nog open
        # (bijv. het login-signaal liep niet) — niet uitloggen, wel afsluiten.
        c = client()
        c.force_login(self.lid)
        verzoek = ReloginRequest.objects.create(user=self.lid)
        ReloginRequest.objects.filter(pk=verzoek.pk).update(requested_at=verzoek.requested_at.replace(year=2000))
        invalidate_pending()

        self.assertEqual(c.get(DASHBOARD).status_code, 200)
        self.assertTrue(self.ingelogd(c))
        verzoek.refresh_from_db()
        self.assertEqual(verzoek.status, ReloginRequest.STATUS_FULFILLED)

    def test_intrekken_laat_sessie_met_rust(self):
        c = client()
        c.force_login(self.lid)
        self.veroudert(c)
        force(self.lid)
        cancel(self.lid, by=self.beheer)

        self.assertEqual(c.get(DASHBOARD).status_code, 200)
        self.assertTrue(self.ingelogd(c))
        self.assertEqual(ReloginRequest.objects.get(user=self.lid).status, ReloginRequest.STATUS_CANCELLED)

    def test_ander_lid_merkt_niets(self):
        c = client()
        c.force_login(self.gast)
        self.veroudert(c)
        force(self.lid)

        self.assertEqual(c.get(DASHBOARD).status_code, 200)
        self.assertTrue(self.ingelogd(c))

    def test_loginflow_wordt_niet_onderbroken(self):
        c = client()
        c.force_login(self.lid)
        self.veroudert(c)
        force(self.lid)

        r = c.get(reverse("auth_login_user"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.ingelogd(c))

        r = c.get(reverse("auth_sso_login"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("eveonline", r["Location"])
        self.assertTrue(self.ingelogd(c))

    def test_anoniem_ongemoeid(self):
        force(self.lid)
        r = client().get(reverse("auth_login_user"))
        self.assertEqual(r.status_code, 200)


class ModelTests(Basis):
    def test_force_maakt_geen_dubbel(self):
        eerste, nieuw1 = force(self.lid, reason="een")
        tweede, nieuw2 = force(self.lid, reason="twee")
        self.assertTrue(nieuw1)
        self.assertFalse(nieuw2)
        self.assertEqual(eerste.pk, tweede.pk)
        self.assertEqual(ReloginRequest.objects.filter(user=self.lid).count(), 1)

    def test_na_afsluiten_kan_nieuw_verzoek(self):
        force(self.lid)
        fulfil(self.lid)
        _verzoek, nieuw = force(self.lid)
        self.assertTrue(nieuw)
        self.assertEqual(ReloginRequest.objects.filter(user=self.lid).count(), 2)

    def test_pending_map_volgt_wijzigingen(self):
        self.assertNotIn(self.lid.pk, pending_map())
        force(self.lid)
        self.assertIn(self.lid.pk, pending_map())
        cancel(self.lid)
        self.assertNotIn(self.lid.pk, pending_map())

    def test_reden_wordt_afgekapt(self):
        verzoek, _nieuw = force(self.lid, reason="x" * 500)
        self.assertEqual(len(verzoek.reason), 200)


class ViewTests(Basis):
    def test_zonder_permissie_geen_toegang(self):
        c = client()
        c.force_login(self.gast)
        self.assertNotEqual(c.get(reverse("forcerelogin:index")).status_code, 200)
        r = c.post(reverse("forcerelogin:force", args=[self.lid.pk]), {"reason": "x"})
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("auth_login_user"), r["Location"])
        self.assertFalse(ReloginRequest.objects.filter(user=self.lid).exists())

    def test_overzicht(self):
        c = client()
        c.force_login(self.beheer)
        r = c.get(reverse("forcerelogin:index"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Lid Alfa")
        self.assertContains(r, "Gast Charlie")
        self.assertContains(r, "Forceer herlogin")

    def test_forceren_via_knop(self):
        c = client()
        c.force_login(self.beheer)
        r = c.post(
            reverse("forcerelogin:force", args=[self.lid.pk]),
            {"reason": "main gewisseld", "next": reverse("forcerelogin:index") + "?scope=pending"},
        )
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("forcerelogin:index") + "?scope=pending")

        verzoek = ReloginRequest.objects.get(user=self.lid)
        self.assertTrue(verzoek.is_pending)
        self.assertEqual(verzoek.requested_by, self.beheer)
        self.assertEqual(verzoek.reason, "main gewisseld")

        melding = Notification.objects.filter(user=self.lid).latest("timestamp")
        self.assertIn("main gewisseld", melding.message)

        r = c.get(reverse("forcerelogin:index") + "?scope=pending")
        self.assertContains(r, "Lid Alfa")
        self.assertNotContains(r, "Gast Charlie")
        self.assertContains(r, "Intrekken")
        self.assertContains(r, "main gewisseld")

    def test_tweede_keer_forceren_maakt_geen_dubbel(self):
        c = client()
        c.force_login(self.beheer)
        c.post(reverse("forcerelogin:force", args=[self.lid.pk]))
        c.post(reverse("forcerelogin:force", args=[self.lid.pk]))
        self.assertEqual(ReloginRequest.objects.filter(user=self.lid).count(), 1)
        self.assertEqual(Notification.objects.filter(user=self.lid).count(), 1)

    def test_intrekken_via_knop(self):
        force(self.lid, by=self.beheer)
        c = client()
        c.force_login(self.beheer)
        r = c.post(reverse("forcerelogin:cancel", args=[self.lid.pk]))
        self.assertEqual(r.status_code, 302)
        verzoek = ReloginRequest.objects.get(user=self.lid)
        self.assertEqual(verzoek.status, ReloginRequest.STATUS_CANCELLED)
        self.assertEqual(verzoek.cancelled_by, self.beheer)

    def test_alleen_post(self):
        c = client()
        c.force_login(self.beheer)
        self.assertEqual(c.get(reverse("forcerelogin:force", args=[self.lid.pk])).status_code, 405)
        self.assertEqual(c.get(reverse("forcerelogin:cancel", args=[self.lid.pk])).status_code, 405)

    def test_next_naar_buiten_wordt_genegeerd(self):
        c = client()
        c.force_login(self.beheer)
        r = c.post(reverse("forcerelogin:force", args=[self.lid.pk]), {"next": "https://evil.example/"})
        self.assertEqual(r["Location"], reverse("forcerelogin:index"))

    def test_zoeken_op_alt_en_corp(self):
        c = client()
        c.force_login(self.beheer)
        r = c.get(reverse("forcerelogin:index") + "?q=andere+corp")
        self.assertContains(r, "Gast Charlie")
        self.assertNotContains(r, "Lid Alfa")

    def test_geschiedenis(self):
        force(self.lid, by=self.beheer, reason="oud verzoek")
        fulfil(self.lid)
        c = client()
        c.force_login(self.beheer)
        r = c.get(reverse("forcerelogin:index"))
        self.assertContains(r, "oud verzoek")
        self.assertContains(r, "voldaan")


class BulkTests(Basis):
    """Bulk: aangevinkte rijen of alles wat het filter toont; admins nooit."""

    BULK = "forcerelogin:bulk"

    def beheer_client(self) -> Client:
        c = client()
        c.force_login(self.beheer)
        return c

    def test_admin_ids(self):
        from django.contrib.auth.models import Group
        from forcerelogin.views import admin_ids

        self.assertIn(self.beheer.pk, admin_ids())   # heeft de permissie zelf
        self.assertIn(self.staf.pk, admin_ids())     # is_staff
        self.assertNotIn(self.lid.pk, admin_ids())

        groep = Group.objects.create(name="fr_groep")
        groep.permissions.add(AuthUtils.get_permission_by_name("forcerelogin.basic_access"))
        self.lid2.groups.add(groep)
        self.assertIn(self.lid2.pk, admin_ids())     # via groep

        state = AuthUtils.create_state("fr_state", 500)
        state.permissions.add(AuthUtils.get_permission_by_name("forcerelogin.basic_access"))
        # Signalen uit: anders zet AA de state terug naar Guest, want het
        # character is geen lid van de nieuwe state.
        AuthUtils.assign_state(self.gast, state, disconnect_signals=True)
        self.assertIn(self.gast.pk, admin_ids())     # via AA-state

    def test_geselecteerde(self):
        c = self.beheer_client()
        r = c.post(reverse(self.BULK), {
            "mode": "selected", "user_id": [self.lid.pk, self.lid2.pk], "reason": "bulk",
            "next": reverse("forcerelogin:index") + "?corp=98000001",
        })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("forcerelogin:index") + "?corp=98000001")
        self.assertEqual(ReloginRequest.objects.pending().count(), 2)
        for lid in (self.lid, self.lid2):
            verzoek = ReloginRequest.objects.get(user=lid)
            self.assertEqual(verzoek.reason, "bulk")
            self.assertEqual(verzoek.requested_by, self.beheer)
            self.assertEqual(Notification.objects.filter(user=lid).count(), 1)

    def test_geselecteerde_slaat_admins_over(self):
        c = self.beheer_client()
        c.post(reverse(self.BULK), {"mode": "selected", "user_id": [self.lid.pk, self.staf.pk, self.beheer.pk]})
        self.assertEqual(set(ReloginRequest.objects.pending().values_list("user_id", flat=True)), {self.lid.pk})

    def test_hele_corp(self):
        c = self.beheer_client()
        r = c.post(reverse(self.BULK), {"mode": "shown", "scope": "all", "q": "", "corp": "98000001"}, follow=True)
        gedwongen = set(ReloginRequest.objects.pending().values_list("user_id", flat=True))
        # Lid Alfa + Lid Delta; Beheer (permissie) en Staf (is_staff) niet; Gast zit in een andere corp.
        self.assertEqual(gedwongen, {self.lid.pk, self.lid2.pk})
        tekst = " ".join(str(m) for m in r.context["messages"])
        self.assertIn("2 lid/leden", tekst)
        self.assertIn("2 admin(s) overgeslagen", tekst)

    def test_alle_getoonde_zonder_corp(self):
        c = self.beheer_client()
        c.post(reverse(self.BULK), {"mode": "shown", "scope": "all", "q": "", "corp": ""})
        gedwongen = set(ReloginRequest.objects.pending().values_list("user_id", flat=True))
        self.assertEqual(gedwongen, {self.lid.pk, self.lid2.pk, self.gast.pk})

    def test_getoonde_volgt_zoekfilter(self):
        c = self.beheer_client()
        c.post(reverse(self.BULK), {"mode": "shown", "scope": "all", "q": "delta", "corp": ""})
        self.assertEqual(set(ReloginRequest.objects.pending().values_list("user_id", flat=True)), {self.lid2.pk})

    def test_al_open_niet_dubbel(self):
        force(self.lid, by=self.beheer)
        c = self.beheer_client()
        r = c.post(reverse(self.BULK), {"mode": "shown", "scope": "all", "q": "", "corp": "98000001"}, follow=True)
        self.assertEqual(ReloginRequest.objects.filter(user=self.lid).count(), 1)
        self.assertEqual(ReloginRequest.objects.pending().count(), 2)
        self.assertIn("1 stond(en) al open", " ".join(str(m) for m in r.context["messages"]))

    def test_niets_geselecteerd(self):
        c = self.beheer_client()
        r = c.post(reverse(self.BULK), {"mode": "selected"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(ReloginRequest.objects.exists())

    def test_onbekende_mode(self):
        c = self.beheer_client()
        c.post(reverse(self.BULK), {"mode": "iedereen"})
        self.assertFalse(ReloginRequest.objects.exists())

    def test_zonder_permissie(self):
        c = client()
        c.force_login(self.gast)
        r = c.post(reverse(self.BULK), {"mode": "shown", "scope": "all", "q": "", "corp": ""})
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("auth_login_user"), r["Location"])
        self.assertFalse(ReloginRequest.objects.exists())

    def test_overzicht_met_corpfilter(self):
        c = self.beheer_client()
        r = c.get(reverse("forcerelogin:index") + "?corp=98000001")
        self.assertContains(r, "Lid Alfa")
        self.assertNotContains(r, "Gast Charlie")
        self.assertContains(r, "Forceer hele corp Testcorp (2)")
        # admins krijgen een label en geen vinkje
        self.assertContains(r, ">admin<", count=2)
        self.assertContains(r, f'name="user_id" value="{self.lid.pk}"')
        self.assertNotContains(r, f'name="user_id" value="{self.staf.pk}"')

    def test_overzicht_zonder_corpfilter(self):
        c = self.beheer_client()
        r = c.get(reverse("forcerelogin:index"))
        self.assertContains(r, "Forceer alle getoonde (3)")
        self.assertContains(r, "Alle corporaties")
        self.assertContains(r, "Andere Corp")
