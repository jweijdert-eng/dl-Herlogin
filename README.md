# Herlogin

Alliance Auth-plugin waarmee je een lid **per account** dwingt om opnieuw in te
loggen via EVE SSO.

Handig als iemand van main gewisseld is, als je twijfelt of een sessie nog van
de juiste persoon is, of als je na een scope-wijziging wilt dat het lid weer
door de volledige SSO-flow gaat (AA controleert bij elke login opnieuw het
eigendom van het main character).

## Hoe het werkt

* Op **/forcerelogin/** staat een lijst van alle accounts met een main
  character, met corp, state en laatste login. Per rij een knop **Forceer
  herlogin** met een optioneel redenveld; een open verzoek kun je weer
  **Intrekken**.
* Een verzoek logt niemand direct uit. Een middleware kijkt bij elk verzoek van
  een ingelogd lid of er een open herlogin-verzoek voor dat lid is dat **jonger is
  dan de sessie**. Zo ja: sessie beëindigd, melding op de loginpagina en
  terug naar de pagina waar het lid was. Dat werkt daardoor ook voor sessies op
  andere apparaten en met elke sessie-backend.
* Zodra het lid opnieuw inlogt is de **main** klaar. Het lid krijgt bovendien
  een AA-notificatie met de reden, zodat na het inloggen duidelijk is waarom
  de sessie beëindigd was.
* Met **incl. alts** (standaard aan) is het verzoek dan nog niet voldaan: het
  lid komt op een pagina *Log je alts opnieuw in* en kan Auth verder niet
  gebruiken tot **elke alt van het account één keer via EVE SSO** geweest is.
  Bij zo'n token beoordeelt AA het eigendom van het character opnieuw, dus dit
  is de manier om een heel account (main én alts) te laten herbevestigen. De
  lijst toont de voortgang als *alts 2/6*. Zonder het vinkje is de main-login
  genoeg — handig voor een snelle sessie-reset.
* Met 🔑 **tokens intrekken** (standaard **uit**) gaan bij het forceren ook alle
  ESI-tokens van het account weg — zie hieronder.
* De **geschiedenis** (wie, wanneer, waarom, status) staat onderaan de pagina en
  in het admin-paneel (met een filter op status en een bulk-actie *intrekken*).

## ESI-tokens intrekken

Het vinkje 🔑 **tokens intrekken** gooit bij het forceren alle ESI-tokens van
het account weg: main én alts, van álle apps. Het staat standaard uit en vraagt
een bevestiging, want het lid kan dit zelf niet terugdraaien.

* **Wat er stilvalt:** Member Audit, CorpTools, Character Scan en alles wat
  verder ESI gebruikt, tot het lid z'n characters opnieuw koppelt. Heeft het
  lid een corp-token (director), dan valt de corp-brede audit dus ook stil.
* **Wat blijft staan:** het account zelf. De `CharacterOwnership`, het main
  character, de state en de groepen blijven ongemoeid — AA ruimt bij het
  weggooien van het laatste token normaal het eigendom op, en daarmee de main
  en de state (lid zakt naar Guest en vliegt uit Discord). Dat gedrag wordt
  tijdens het wissen tegengehouden, met een herstelstap als vangnet.
* **Opnieuw koppelen gaat via CharLink**, niet via deze plugin: de herlogin
  vraagt alleen `publicData`. Daarom belandt het lid na de herlogin (en na de
  alts-stap) één keer op CharLink, met een melding erbij. Eén duwtje, geen
  gijzeling — de volgende pagina komt gewoon door.
* Staat **Character Scan** (`aa-characterscan`) ernaast, dan gaan de
  aanmeldingen van dat account terug naar **Nieuw**: met ingetrokken toegang
  hoort een recruiter er opnieuw naar te kijken in plaats van op een oordeel
  van maanden geleden te leunen. De aanmelding en haar logboek blijven staan;
  er komt een regel *Heropend* bij met de reden erin. Is de plugin niet
  geinstalleerd, dan gebeurt er niets — Herlogin hangt er niet van af.
* Er komt niemand *nieuw* in de Character Scan te staan: alleen bestaande
  aanmeldingen gaan terug naar Nieuw. Een lid kan zichzelf wel aanmelden door
  bij het opnieuw koppelen in CharLink het vinkje *Character Scan* aan te
  zetten.
* Er gaat geen revoke-call naar CCP: een refresh token is alleen bruikbaar met
  de client-secret van jouw installatie, dus de rij hier weggooien ís het
  intrekken.
* De lijst en de geschiedenis tonen 🔑 met het aantal ingetrokken tokens.

## Bulk en hele corp

* Vink leden aan en klik **Forceer geselecteerde**; het redenveld in de
  bulk-balk geldt dan voor allemaal.
* Kies een corporatie in het filter en de rode knop wordt **Forceer hele corp
  <naam> (n)** — zonder corp-filter is het **Forceer alle getoonde (n)**, wat
  precies de rijen zijn die het huidige filter en zoekveld tonen. Het getal is
  wat er echt geraakt wordt; er komt een bevestigingsvraag.
* **Admins worden bij bulk altijd overgeslagen**: superusers, staff en iedereen
  die zelf de Herlogin-permissie heeft (rechtstreeks, via een groep of via een
  AA-state) — dus ook jijzelf. Ze staan in de lijst met een blauw *admin*-label
  en zonder vinkje. Wil je zo iemand toch laten herloggen, dan kan dat per rij
  met de gewone knop.
* Wie al een open verzoek heeft wordt niet nog eens aangemaakt; de melding
  achteraf zegt hoeveel leden geraakt zijn, hoeveel al open stonden en hoeveel
  admins overgeslagen zijn. Staat 🔑 aan, dan worden de tokens van zo'n open
  verzoek alsnog ingetrokken en telt de melding ze mee.
* Het vinkje 🔑 **tokens intrekken** geldt ook voor bulk. Een hele corp
  tegelijk betekent dat al die leden opnieuw moeten koppelen in CharLink —
  bevestig die vraag dus bewust.

Sessies die al bestonden vóór de plugin geïnstalleerd werd hebben geen
inlogstempel en gelden als "oud": een verzoek voor zo'n account werkt dus ook
gewoon.

## Installatie

```bash
pip install git+https://github.com/jweijdert-eng/dl-Herlogin.git
```

Daarna in `local.py`:

```python
INSTALLED_APPS += ['forcerelogin']
MIDDLEWARE += ['forcerelogin.middleware.ForceReloginMiddleware']
```

en:

```bash
python manage.py migrate forcerelogin
python manage.py collectstatic
```

Herstart webserver **en** worker in dezelfde beweging als het installeren —
AA maakt het menu-item aan bij de eerste sync en een draaiend proces dat de
app nog niet kent klapt anders op het menu.

## Permissies

| Permissie                    | Wat het geeft                                      |
|------------------------------|----------------------------------------------------|
| `forcerelogin.basic_access`  | Menu-item, het overzicht, forceren en intrekken.   |

Geef dit alleen aan leiding/recruiters: wie dit heeft kan elk account uit z'n
sessie gooien.

## Wat het niet doet

* Zonder het vinkje 🔑 blijven bestaande **ESI-tokens** staan; alleen de
  Auth-sessie vervalt. De alts-stap vraagt alleen `publicData` (net als de
  login) en vervangt geen tokens van andere apps.
* Het lid wordt niet uitgelogd bij EVE zelf. Is het lid daar nog ingelogd, dan is de
  SSO-flow één klik — het gaat erom dat AA de login opnieuw verwerkt.
* Kan een lid een alt niet meer inloggen (character verkocht of gebiomassed,
  maar nog aan het account gekoppeld), dan blijft het verzoek open. Een
  beheerder trekt het dan in; wie zelf de Herlogin-permissie heeft houdt
  daarvoor tijdens de alts-fase toegang tot het beheerscherm.
