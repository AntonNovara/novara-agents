# Betriebsprofile — Pilotprogramm

Ein Profil pro Pilotkunde, als `clients/<client_id>.json`. `_vorlage.json`
zeigt alle Felder mit Beispielwerten — beim Anlegen eines neuen Kunden diese
Datei kopieren, umbenennen und ausfüllen.

`client_id` ist der Teil, der im Widget-Embed-Code und in der URL landet
(`data-client="elektro-mueller"` → `clients/elektro-mueller.json`) —
klein geschrieben, keine Leerzeichen/Sonderzeichen, z. B. `elektro-mueller`.

## Aufnahmegespräch — die 10 Fragen (ca. 20–30 Min.)

Reihenfolge ist auch die sinnvolle Gesprächsreihenfolge:

1. **Firmenname** genau so, wie er im Chat erscheinen soll.
2. **Branche/Schwerpunkt** — falls "Elektrikerbetrieb" nicht exakt passt.
3. **Servicegebiet** — welche Bezirke/Umgebung, damit der Bot niemanden
   fälschlich vertröstet oder Anfragen außerhalb des Gebiets nicht abweist.
4. **Leistungen** — die 4–8 wichtigsten, kurz und konkret (keine Marketing-
   Sprache, das beantwortet später das LLM selbst im passenden Ton).
5. **Notdienst-Regel** — WANN ist es ein Notfall (Stichworte, die der Kunde
   selbst nennt: "Stromausfall", "Kurzschluss", "Brandgeruch", ...) UND was
   soll der Bot dann tun (Telefonnummer nennen? Sofort-Rückruf ankündigen?).
   Das ist das wichtigste Feld — ein falsch behandelter Notfall ist der
   teuerste mögliche Fehler.
6. **Buchungslink** — eigener Google-Calendar-Terminlink des Kunden. Falls
   noch keiner existiert: gemeinsam in unter 10 Minuten einen anlegen
   (calendar.google.com → Termine → "Buchungsseite"), nicht Novaras eigenen
   Link verwenden.
7. **Häufige Fragen** — 3–6 Fragen, die der Kunde selbst am öftesten hört
   (nicht raten!), mit der Antwort, die ER geben würde.
8. **Ansprechpartner + Telefonnummer** — für den Fall, dass der Bot an einen
   Menschen übergeben muss.
9. **Ton** — Standard ("freundlich, professionell, per Sie") passt fast
   immer; nur ändern, wenn der Kunde ausdrücklich etwas anderes will.
10. **Preise öffentlich nennen?** — Standard ist NEIN (`preise_oeffentlich:
    false`) — die meisten Handwerker wollen Preise nicht pauschal im Chat
    nennen. Falls JA: unter `preishinweise` genau die Regel eintragen (z. B.
    "Anfahrtspauschale 45€, ab da nach Aufwand").

## Nach dem Ausfüllen

1. JSON-Datei committen (`git add clients/<client_id>.json`) und pushen —
   löst automatisch ein Redeploy aus, danach ist das Profil live (Profile
   werden beim Prozessstart gecached, siehe `core/client_profiles.py`).
2. Widget-Snippet für die Kunden-Website:
   ```html
   <script src="https://novara-agents-production.up.railway.app/static/chat_widget.js"
           data-client="elektro-mueller"
           data-title="Elektro Müller GmbH"></script>
   ```
3. Kurz selbst im Browser testen (2–3 realistische Fragen inkl. einer
   Notdienst-Anfrage), bevor der Kunde es sieht.
