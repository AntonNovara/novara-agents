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

## Angebots-Generator (optional, `agents/quote_agent.py`)

Nur nötig, wenn der Betrieb auch den WhatsApp-Angebots-Generator nutzt:
`stundensatz_eur`, `anfahrtspauschale_eur`, `materialaufschlag_pct`
(Prozent-Aufschlag auf die bepreiste Lohnsumme, siehe CLAUDE.md-Abschnitt
"Angebots-Generator: Materialaufschlag-Bug behoben"). Alle drei bleiben
`null`/leer, solange der Betrieb sie nicht nennt — dann markiert der Agent
jede Position als "nach Aufwand" statt einen Preis zu schätzen.

## Google-Bewertungs-Filter (optional, `main.py GET/POST /r/{client_id}`)

- **`google_review_url`** — der ECHTE "Rezension schreiben"-Link aus dem
  Google-Unternehmensprofil des Betriebs (Google-Maps-Eintrag → "Rezensionen
  verwalten" → Link kopieren, Format meist `https://g.page/r/.../review`).
  Ohne diesen Wert bleibt die 4/5-Sterne-Weiterleitung deaktiviert.
- **`review_benachrichtigung_email`** — wohin die private 1-3-Sterne-
  Feedback-Mail geht. Bewusst eine EIGENE Adresse, nicht zwangsläufig
  `ansprechpartner`/`telefonnummer` — manche Betriebe wollen dafür ein
  eigenes Postfach.
- **Vor dem Ausrollen unbedingt mit dem Kunden besprechen:** dieser Filter
  lädt schlechte Bewertungen bewusst NICHT zur öffentlichen Abgabe auf
  Google ein ("Review Gating", gegen Googles eigene Richtlinien, in den USA
  per FTC-Regel verboten). Siehe CLAUDE.md, Abschnitt "Google-Bewertungs-
  Filter", für die volle Abwägung — das ist eine bewusste Kundenentscheidung,
  kein rein technisches Detail.

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
