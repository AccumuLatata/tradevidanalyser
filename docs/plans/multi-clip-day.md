# Phase 1 — Mehrere Clips pro Tag und Pausen

**Status:** Plan. Keine Code-, Konfigurations-, Test- oder Datenänderung in diesem PR.
**Stand geprüft:** `main` @ `f44d864` (Coach, PR-27), nach dem zweiten Review des Trading Coach vom 06.10.2026 (Head `9e4b739`).
**Danach:** Review durch Accumu und den Trading Coach. Umsetzung erst als die PR-Serie unten, jeder PR mit eigenem Review.
**Nicht in diesem Plan:** Phase 2 (Analysen abschalten, umbauen oder einen Datenvertrag für den Trading Coach). Alle bestehenden Analysen, Ausgaben und Schemas bleiben inhaltlich, was sie sind. Geändert wird nur, was nötig ist, damit sie bei mehreren Clips pro Tag und bei Pausen nicht mehr still falsch sind.
**Entscheidungen:** CD1–CD14 in Abschnitt 6. Das sind nicht die gesperrten D1–D9 aus `docs/05_ROADMAP.md`. Roadmap-D4 (Tagesverlust 100 gegen 200) bleibt geparkt; `rules.yaml:4-5` und der Grundtext `daily_loss_limit_usd is unset (D4)` in `rules.py` meinen dieses Roadmap-D4. Zeile 6 von `rules.yaml` ist der Kommentar zu `flat_by`, nicht D4.
**Seriennummern:** PR-28…PR-36 setzen `docs/IMPLEMENTATION_PLAN.md` fort (zuletzt gelandet PR-27). Das sind keine GitHub-Pull-Request-Nummern. Die Nummern folgen der Merge-Reihenfolge. Die frühere Nummer PR-34 (Audit) ist jetzt PR-28.

---

## 1. Befund

Geprüft gegen den Code, nicht gegen die NAS. Unter `TVA_ROOT` liegen in diesem Repo keine KW38–KW40-Sessions und kein TradesViz-Export. Wo der Code die Ursache nicht hergibt, steht das als offene Frage, nicht als Annahme.

### 1.1 Lineares Alignment, und was „low“ heute nicht tut

Die Uhrzeit ist ein einzelner Strahl über die ganze Datei:

```text
wall = start_wallclock_vienna + video_t + offset_s + drift_s_per_h · video_t / 3600
```

Fit und Inverse stehen in `src/tradevidanalyser/align.py:223-237` (`compute_alignment`), `src/tradevidanalyser/evidence.py:127-141` (`wall_to_video_t`) und `src/tradevidanalyser/rules.py:689-701` (`_wall_to_video`). Der Fit ist Theil–Sen auf `y = (OCR-Uhr − Start) − video_t` (`align.py:44-69`, Messwerte `align.py:124-154`).

Ein pausierter Clip ist kein Strahl. OBS-Pause lässt eine Lücke in der Wanduhr und keine Lücke in der Datei. `y` kann dadurch nur steigen: die Wanduhr läuft, die Videozeit nicht. Die Videodauer ist die Summe der aufgenommenen Stücke, nicht `Wandende − Wandstart`. `pause_total = y_last − y_first` ist genau diese Differenz.

Was der Fit damit macht:

- Ein einzelner Ausreißer zieht den Fit nicht weg. `tests/test_align.py:124-140`: eine Uhr liegt 60 s daneben, Offset bleibt ≈ 1,8 s, der Residual bleibt > 50 s. Die Konfidenz hängt an der MAD um den Residual-Median (`align.py:72-92`), ein Minderheiten-Ausreißer senkt sie kaum.
- Eine Pause ist eine Stufe, kein einzelner Ausreißer. Liegen auf beiden Seiten viele Samples, dominieren die Paare über die Stufe, und Theil–Sen macht aus der Pause eine Drift. Liegen fast alle Samples auf einer Seite (typisch, siehe 1.2), sieht die Stufe aus wie der getestete Ausreißer: der Fit bleibt bei der langen Seite, die Konfidenz bleibt hoch, die kurze Seite wird falsch zugeordnet.
- Mehrere Pausen legen Start, Mitte und Ende auf verschiedene Stufen. Drei Proben ohne ein Paar innerhalb weniger Sekunden sind dann kein Drift-Fall, sondern die Pause selbst. Eine Regel, die genau dieses Muster `unverifiable` nennt, lässt den falschen Fit stehen.
- Es gibt keinen Sprung-Detektor. `Alignment` hat Offset, Drift, Konfidenz, Methode, Samples (`schema.py:36-51`). `invalid` gibt es nicht. `chapter_fill` steht im Literal und wird nirgends erzeugt.

Ohne OCR-Uhr fällt `align` auf den Dateinamen zurück: Offset 0, Drift 0, Konfidenz 0,6, Methode `filename` (`align.py:15`, `align.py:178-185`, `align.py:235-236`). `--manual-offset` setzt nur den Offset, Drift bleibt 0 (`align.py:188-202`). `tva align` hat kein `--force` (`cli.py:150-161`).

**Korrektur an der Kurzfassung.** Konfidenz unter 0,8 macht nicht alle zeitabhängigen Regeln unverifiable. `ALIGNMENT_LOW = 0.8` (`rules.py:63`) wird nur in `_alignment_too_low` (`rules.py:773-781`) gelesen, und das nur von:

- R-HOURLY (`rules.py:949-958`)
- R-ZONE (`rules.py:982-991`)
- R-TILT (`rules.py:1028-1038`)

R-DLL, R-MAX10, R-3L30, R-5M, R-REENTRY und R-CLOSE lesen die Alignment-Konfidenz nicht (`rules.py:396-621`). Evidence baut die Fenster auch bei niedriger Konfidenz und setzt nur `alignment: "low"` (`evidence.py:325-363`). `evidence.py:144-154` ist der Fallback, wenn `session.alignment` fehlt, nicht der Fensterbau. Ein stilles falsches Mapping bleibt also stehen. Genau das soll Phase 1 für **Pausen** abstellen, nicht für den bisherigen Dateinamen-Fallback eines durchgehenden Clips (Parität, Abschnitt 3.1).

`parse_clock` legt die Uhr auf den Kalendertag von `Start + video_t` und klappt nur, wenn die Abweichung größer als 12 h ist (`ocr.py:142-174`). Die KW40-Lücke von etwa 5,5 h liegt darunter und würde als Residual sichtbar, **wenn** Samples auf beiden Seiten existieren. Eine Lücke über 12 h kann vom Klapper verdeckt werden. Das ist eine Grenze des Detektors, nicht der KW40-Fall.

### 1.2 OCR sieht eine Pause heute meist nicht

`tva frames` ohne `--at` nimmt nur Chapter-Marker mit Offsets −5, −2, 0, +2, +5 s (`frames.py:27`, `frames.py:127-135`). `tva ocr` liest diese Frames. Ein Clip ohne Chapters hat keine Uhr-Samples, also den Dateinamen-Fallback. Ein Clip mit Chapters nur am Anfang hat Samples auf einer Seite der Pause. Audio sieht die Pause ebenfalls nicht: die ausgelassene Wandzeit liegt nicht in der Datei, der Ton ist entlang der Videozeit lückenlos. Stille im File ist Kommentar-Pause, nicht OBS-Pause. Audio ist deshalb kein Pausensignal.

`tva align` ist keine Bot-Run-Stufe (`docs/04_ARCHITECTURE.md:123`). `tva evidence` ohne vorheriges Align nimmt `effective_alignment()` und mappt mit dem Dateinamen (`evidence.py:144-154`, `evidence.py:317-337`).

Die Uhr hat im Code keine feste Auflösung. `_first_clock_match` nimmt zuerst eine Uhr mit Sekunden (`_CLOCK_COLON`, `_CLOCK_ANY`) und sonst `_CLOCK_HM` (`ocr.py:37`, Aufruf `ocr.py:447`). Fehlt die Sekundengruppe, wird die Sekunde 0 (`ocr.py:451`). Die Clock-ROI liegt bei x 0,90, y 0,96 (`layout.yaml:29`), klein genug für eine Taskleisten-Uhr, die oft nur HH:MM zeigt. Accumu hat zusätzlich eine große Uhr im Bild. Ob die Sekunden zeigt, steht nicht im Repo. Der Detektor deckt beide Formate ab (2.2).

### 1.3 Fills-Fenster, doppelt und zu kurz

`tva fills` behält Fills im geschlossenen Intervall `[Start − 30 min, Start + Dauer + 30 min]` (`fills.py:35`, `fills.py:122-128`, Vergleich `fills.py:131-142`). Start und Ende kommen aus Dateiname und `duration_s`, nicht aus dem Alignment. Das Ende wird in UTC addiert (`fills.py:126-127`), nicht auf einer Vienna-Zeit mit Sommerzeit. Jede Session schreibt ihr eigenes `fills.parquet` (`fills.py:649-738`, Schreiben bei `716-717`). Danach löscht `ingest_fills` Evidence, Regeln, Context, Debrief, Proposals und die Ledger-Zeile **nur dieser** Session (`fills.py:718-726`). Es gibt keine Zuordnung über Sessions. Der Ledger-Schlüssel ist `(session_id, tva_trade_id)` (`ledger.py:205`), dieselbe Execution in zwei Sessions ist zwei Trades. `drop_session` löscht nur Zeilen mit genau dieser `session_id` (`ledger.py:266-277`).

Beispiel aus dem Auftrag, nachgerechnet: `2026-09-30 09-02-21.mp4`, Dauer 51:48, Start 09:02:21 Vienna. Nominales Ende 09:54:09, plus 30 min Pad → **10:24:09**. Die Aussage „ab etwa 10:24 unbrauchbar“ ist dieses Fenster, nicht `Start + Dauer`. Die Datei ist 51:48 Video und deckt laut Auftrag etwa 09:02–14:42 Wandzeit ab. Das ist mehr Lücke als eine einzelne Pause: `pause_total` läge bei mehreren Stunden. Fills nach 10:24:09 fehlen in diesem `fills.parquet`. Fills davor können drin sein und werden mit dem linearen Strahl auf die Videozeit gelegt.

Kurze Clips: zwei Blöcke, deren Pads sich überlappen (Abstand der Kerne unter 60 min), teilen sich dieselben Fills. Evidence hängt dann an einem Trade, der in dem Clip nicht zu sehen ist.

Zwei Loader schreiben dasselbe Parquet. `load_fills` nimmt ThesisTester, wenn es installiert ist, sonst `fills_mirror` (`fills.py:298-308`). Das Parquet trägt den Loader nicht. Welche Quelle die 20 von 88 geschrieben hat, steht im Repo nicht.

### 1.4 Tagesregeln laufen pro Session

`build_rules_report` liest nur `sessions/<id>/trades.parquet` (`rules.py:1182-1209`) und bewertet den Katalog darauf (`rules.py:1079-1095`). R-MAX10 zählt Paare (Round-Turns, offene inklusive), nicht Executions (`rules.py:440-445`). R-DLL summiert `net_pnl_currency`, ersatzweise brutto mit `fees_unknown` (`rules.py:321-329`). Schwellen: `rules.yaml:8-18` (`daily_loss_limit_usd` ist null, R-DLL damit heute unverifiable; `max_trades_per_day: 10`; `flat_by` in Zeile 18).

Zehn Clips mit je drei Trades liefern zehnmal „3 ≤ 10, pass“. Dieselben zehn Sessions im Ledger zählen eine Tagesverletzung zehnmal mit, weil `rule_checks` pro `(session_id, rule)` liegt (`ledger.py:211-216`) und der Rollup Zeilen zählt (`ledger.py:565-586`).

### 1.5 Notion überschreibt die Tagesseite, und es gibt keinen Body

Titel ist `<D Mon YYYY> Session Debrief` aus dem Vienna-Kalendertag des Dateistarts (`publish.py:108-109`, `context.py:104-106`, `context.py:121-141`). `publish_session` sucht diese Seite und aktualisiert sie (`publish.py:789-794`). Der zweite Clip desselben Tages ersetzt Summaries und Learnings. Ein Titel pro Clip existiert nicht.

Was geschrieben wird, sind Properties: Titel, Summaries = ein Satz, Learnings = drei Zeilen, aus dem `debrief.json` **einer** Session (`publish.py:140-150`). `_patch_debrief` setzt nur `properties` (`publish.py:370-381`). Einen Seiten-Body mit Abschnitten schreibt Publish nicht. Die `page_id` liegt in `sessions/<id>/publish.json` (`publish.py:734-752`). `append_run_log` hängt bei jedem Publish eine Zeile an (`publish.py:396`, Aufruf `publish.py:796`). Das ist auch heute nicht idempotent.

### 1.6 Trades/Stunde

`hours = duration_s / 3600` pro Session (`ledger.py:507`, `ledger.py:530-533`), Rollup summiert `hours` (`ledger.py:667-701`), Rate ist `trades / hours` (`ledger.py:555-558`). Das ist die Summe der Dateilängen. Der Auftrag nennt das den Bug und will die Rate auf Handelszeit. Für einen pausierten Clip ist der Nenner zu klein. Für mehrere gültige Stop/Start-Clips ist der Nenner die aufgezeichnete Zeit ohne die Lücken dazwischen, also genau die heutige Summe, und der Zähler ist zu groß, sobald Fills doppelt im Ledger stehen. Eine geschätzte Wandspanne aus einem kaputten Alignment wäre eine weitere Verzerrung. Abschnitt 2.6 und CD3 legen fest, was stattdessen gilt. Die Summe der `duration_s` ist dort eine Option, nicht die Empfehlung.

### 1.7 Zusammenlegen der OBS-Teile

`discover_split_parts` (`ingest.py:97-159`): gleiche Dateiendung, gleiches Namenspräfix (`naming.py:49-55`), aufeinanderfolgende Starts, `|Startₙ₊₁ − Startₙ − Dauerₙ| ≤ 5 s` (`ingest.py:14`, `ingest.py:149-154`). `abs(gap) <= 5` stitched auch eine negative Lücke. Das ist der Auto-Split und muss bleiben, bis CD12 etwas anderes beschließt.

Zwei Korrekturen am Befund, nicht an der Schwelle:

- Ein normaler OBS-Name `YYYY-MM-DD HH-MM-SS` hat das Präfix `""`. Alle solchen Dateien in **einem** Ordner gelten als gleiches Präfix. Getrennt wird nur durch die 5 s. Ein Stop/Start innerhalb von 5 s nach dem Ende der vorigen Datei wird zusammengelegt (`tests/test_ingest_and_pipeline.py:59-86`). Zwei Sessions desselben Tages mit Stunden Abstand bleiben zwei Sessions (`tests/test_ingest_and_pipeline.py:47-56`). Unterschiedliche Präfixe stitchen auch bei Abstand 0 nicht (`tests/test_ingest_and_pipeline.py:145-155`). Zwei Dateien mit gleichem Präfix und Abstand 0 sind **eine** Session. Der Grenzfall „Ende von A = Start von B“ als zwei Sessions entsteht bei gleichem Präfix nicht. Fixture M2 testet ihn deshalb nur über verschiedene Präfixe oder über einen Abstand über 5 s.
- Die Session-Id ist `YYYY-MM-DD_HHMMSS` ohne Präfix (`naming.py:45`). Zwei Dateien mit derselben Startsekunde und verschiedenem Text davor landen auf derselben Id. Der zweite Ingest mit anderer `sha256` ruft `invalidate_downstream` und überschreibt die Session (`ingest.py:42-56`). Das verwirft die erste Aufnahme ohne Fehler. Die Rohdatei bleibt unter `recordings/`, weil nach Dateinamen kopiert wird (`ingest.py:172-175`). Phase 1 ändert die Id nicht (CD8). Der Fehler ist der Default (2.9). Eine Auto-Split-Fortsetzung ist keine Kollision: `tests/test_ingest_and_pipeline.py:183-199` ingested denselben ersten Dateinamen erneut, `digest` bleibt die SHA des ersten Teils (`ingest.py:40`), nur `parts` wächst.

`watch` kopiert mit `shutil.copy2` (`watch.py:307`) und erhält damit `mtime`. Der Kommentar in `watch.py:23` sagt, dass FAT/`copy2` auf manche NAS-Mounts die mtime auf 2 s rundet. Ob `mtime` die OBS-Stopzeit ist, ist nicht geprüft. Der Audit darf `(mtime − Dateistart) − duration_s` als Hinweiszahl schreiben. Der Detektor benutzt sie nicht.

### 1.8 KW40: 20 von 88 Executions

Nicht nachgerechnet. Export und `fills.parquet` sind nicht im Repo, und dieser Plan liest die NAS nicht. Die Frage ist, warum nur 20 von 88 Executions in den **vorhandenen** `fills.parquet` stehen, nicht wie ein neu gerechnetes Fenster aussehen würde. Aus dem Code sind das die möglichen Ursachen, in dieser Reihenfolge. Keine davon wird hier als die Ursache festgelegt. Dass die CSV-Zeitstempel echtes UTC sind, ist **nicht** verifiziert (CD11). `docs/IMPLEMENTATION_PLAN.md:238` schreibt „TradesViz timestamps are UTC with explicit offset“. PR-28 setzt diese Zeile auf „unverifiziert, siehe CD11“, damit sie kein späterer Leser als Fakt nimmt. Alles Folgende über Fenster und DST gilt nur unter der UTC-Annahme.

1. **Fenster.** Es wird nur `[Start − 30 min, Ende + 30 min]` geschrieben. Ein Tagesexport gegen ein Session-Parquet, oder gegen die Vereinigung kurzer Fenster, verliert alles außerhalb. Für die pausierte Mittwoch-Datei endet das Fenster um 10:24:09. Das allein kann einen großen Fehlbetrag erklären.
2. **Vergleich gegen eine Datei statt gegen den Tag.** Jede Session hat ihr eigenes Parquet.
3. **Zeitzone, unverifiziert.** Der Loader verlangt einen expliziten Offset und speichert UTC (`fills_mirror.py:334-363`). Naive Zeiten werfen, sie werden nicht als Vienna geraten. Das Fenster rechnet den Dateistart von Vienna nach UTC. Ein erfolgreicher Lauf hat also Offsets gesehen. Ein falscher Offset **in der CSV** (etwa `+0000` auf einer New-York-Lokalzeit, oder `Z` statt `+02:00`) schiebt Fills aus dem Fenster. Das wäre eine Eigenschaft der Datei. Ob `+0000` echtes UTC ist, entscheidet CD11, nicht dieser Befund.
4. **`session_date` ist nicht der Vienna-Tag.** `trading_session_date` ist ETH 18:00 America/New_York (`fills_mirror.py:156-162`, geschrieben in `fills_mirror.py:190`). Der Fensterfilter benutzt den Zeitstempel, nicht dieses Datum. **Wenn** die Zeitstempel echtes UTC sind: im Sommer, Differenz 6 h, fällt das ETH-Datum auf den Vienna-Kalendertag; 18:00 New York ist dann 00:00 Vienna des nächsten Tages. KW38–KW40 2026 liegen ganz in dieser Sommerzeit (EU-Umstellung 2026 am 29. März und am 25. Oktober, US-Umstellung am 8. März und am 1. November). Auseinander laufen die Daten im Frühjahr vom 8. bis 29. März (drei Wochen) und im Herbst vom 25. Oktober bis 1. November (eine Woche), und dort nur von 23:00 bis 24:00 Vienna. NY-Morgenclips und der Audit-Zeitraum bis 4. Oktober sind davon nicht betroffen. Ein Join über `session_date` kann 20/88 in KW38–KW40 unter der UTC-Annahme nicht erzeugen. Ein Fehler, der sich am 25.10.2026 von 2 h auf 1 h ändert, ist in KW38–KW40 unsichtbar.
5. **Kein stilles Dedupe.** `fill_id` ist `tv:{Zeilenindex}:{spread}:{UTC-Sekunde}:{side}:{price}:{qty}` (`fills_mirror.py:312-324`). Dieselbe Execution bekommt bei anderer Zeilenreihenfolge eine andere Id. Der Filter wirft keine Dubletten weg. Ein Abgleich, der nur auf die Id geht, unterschätzt Treffer, wenn der Export neu sortiert wurde. Der Abgleich in PR-28 benutzt den Identitätsschlüssel aus 2.3, nicht `fill_id`.
6. **Parse-Fehler erklären keine Teilmenge.** Eine unlesbare Zeile wirft und bricht den Lauf ab (`fills_mirror.py:258-271`, nacktes `MNQ` ohne Kontraktmonat inklusive). Ein geschriebenes Parquet ist vollständig für die geparsten Zeilen. Manuelle Nicht-Futures bleiben im `fills.parquet` (geschrieben vor dem Pairing) und fehlen in `trades.parquet`, solange `--include-manual` nicht gesetzt ist.
7. **Quelle.** `load_fills` nimmt `import`, wenn ThesisTester installiert ist, sonst `mirror` (`fills.py:298-308`). `ingest_fills` ruft das auf (`fills.py:662`). `_import_load` (`fills.py:153-157`) ist nur der Import-Zweig, nicht die Aufrufstelle. Das Parquet speichert den Loader nicht. `venue` ist eine Spalte, die TVA pro Datei setzt (`fills.py:36`, `fills.py:101-106`), kein Konto aus der CSV. `FillRecord` hat kein Kontofeld (`fills_mirror.py:88-105`). Die Pflichtspalten der CSV auch nicht (`fills_mirror.py:53-69`). Nicht-MNQ-Zeilen bleiben im `fills.parquet`, solange der Parser sie kennt. Ob die beiden Loader verschiedene Zeilenmengen schreiben, ist eine offene Ursache, keine Annahme.

### 1.9 Altdaten und die laufende KW41

| ISO-Woche | Vienna-Tage |
|---|---|
| KW38 | 2026-09-14 … 2026-09-20 |
| KW39 | 2026-09-21 … 2026-09-27 |
| KW40 | 2026-09-28 … 2026-10-04 |
| KW41 | ab 2026-10-05 |

KW40-Mittwoch ist 2026-09-30. Welche Sessions pausiert sind, steht nicht im Repo. Der Audit markiert sie. Neu rechnen tut er nicht (CD7).

Ab KW41 nimmt Accumu pro Block einen Clip auf. Der heutige Code verarbeitet diese Tage schon: überlappende Pads, eine Notion-Seite pro Titel, Trades/Stunde aus der Summe der Dateilängen. Bis die Flags an sind, gilt der Betriebshinweis in 2.7.

### 1.10 Der Guard wäre heute umgehbar

Zwei Wege setzen ein gesetztes `invalid` still außer Kraft, wenn es nur in `session.alignment` liegt:

- Evidence ohne Align mappt über den Dateinamen (`evidence.py:144-154`). Der Guard läuft auf diesem Pfad nicht.
- `invalidate_downstream` setzt bei geänderter Aufnahme `alignment = None` (`store.py:257-264`), aufgerufen vom Re-Ingest (`ingest.py:56`). Der nächste Evidence-Lauf mappt wieder über den Dateinamen.

Deshalb liegt das Guard-Ergebnis nicht nur in `alignment.method` (2.2).

### 1.11 Proposals hängen an Evidence und an `tva_trade_id`

`build_proposals` liest `evidence.json` und die Trade-Ids und hängt Tags pro Trade an (`proposals.py:289-330`). Bestätigte Einträge merken sich `tva_trade_id` (`proposals.py:375-377`). Ein invalidierter Clip ohne Evidence verliert diese Trade-Proposals. Ein Tages-Pfad, der die Ids neu vergibt, lässt eine Bestätigung `session:T01` auf einen anderen Trade zeigen, wenn man sie stehen lässt. Transkript und Insights bleiben, die Proposals nicht als „aus dem Transkript“.

---

## 2. Teil 1 — Verhalten, das die Serie herstellt

Zwei Pfade, hart getrennt. Welcher gilt, wird **beim Lauf** entschieden, nicht endgültig beim ersten Clip.

**Clip-Menge eines Vienna-Tags D.** Jede Session, deren `nominal_start` auf D fällt. Sessions der Nachbartage, deren nominelles Intervall in D ragt, gehören nicht zur Clip-Menge. Sie werden nur für den Fill-Besitz gelesen (2.3).

**Legacy-Pfad.** Die Clip-Menge hat genau eine Session, und ihr Guard-Ergebnis ist nicht `suspected` (Flag aus, oder Ergebnis `clear` / `unverifiable`). Dann laufen Ingest, Alignment, Fills, Evidence, Regeln, Report, Publish, Ledger und Rollup durch den heutigen Code. Kein neues Feld in `session.json`, kein `days/`-Eintrag, kein anderer Nenner. Parität ist dieser Pfad. Der KW40-Mittwoch ist dieser Pfad nur, solange der Guard aus ist oder nicht `suspected` liefert.

**Tages-Pfad.** Zwei oder mehr Sessions in der Clip-Menge, oder mindestens eine mit `pause_check: suspected`. Dann gelten die Regeln unten.

**Flag aus und schon geschriebenes `invalid`.** Flag aus heißt: der Guard wird nicht ausgeführt und `pause_checks/` wird nicht gelesen. Steht in `session.alignment.method` schon `invalid`, unterdrücken Evidence und die uhrgemappten Regeln weiter. Der Dateinamen-Fallback gilt nur, wenn `alignment` fehlt oder eine andere Methode hat. Ein bekanntes `invalid` wieder linear zu mappen wäre das stille falsche Mapping, das Phase 1 abstellt.

In der Serie bleiben die Flags in Produktion aus, bis Accumu ein erlaubtes Set einschaltet (3.6) und der NAS-Paritätslauf bestanden ist (CD10). Aus heißt: Guard, Manifest, Besitz, Tagesregeln, Tages-Publish und die neue Rate laufen nicht. Ein schon geschriebenes `invalid` bleibt wirksam, wie der Absatz davor sagt. Die Id-Kollision ist davon ausgenommen: sie ist ab PR-36 ein Fehler, ohne dass ein Flag an sein muss (2.9).

Sortierung der Clips: UTC-Instant von `nominal_start`, dann `session_id`. Nicht der ISO-String. Als String läge `02:30+02:00` vor `02:10+01:00`.

`tva day build <YYYY-MM-DD> --executions <csv>` ist der einzige Schreibbefehl für `days/<date>/`. Pflicht sind `--executions` und die Optionen `--venue` und `--include-manual` (Default wie heute). `--reconcile-dir` ist optional. `--provider` ist optional und steht nur dann im Fingerprint, wenn es gesetzt ist. Ohne `--provider` ruft der Bau kein Modell und keinen Fake-Provider auf. Watch und Ingest rufen `day build` nicht auf. Ein späteres `tva fills` bricht laut ab, wenn die Identitätsschlüssel der für D relevanten Executions oder die Optionen vom letzten Bau abweichen. Die SHA der ganzen CSV allein macht den Tag nicht veraltet: ein neuer Export enthält die Vortage und hätte sonst jede frühere Woche neu gebaut.

Atomarität: nicht `rename` eines nicht leeren Verzeichnisses. Das ist auf Windows und SMB nicht atomar. Der Bau schreibt `days/<date>/builds/<ulid>/`, dann ersetzt `os.replace` die Zeigerdatei `days/<date>/current` (ein kleiner Text, der den Build-Namen nennt). Leser folgen dem Zeiger. Ein abgebrochener Bau lässt den vorigen Zeiger stehen.

### 2.1 Tagesobjekt, Fingerprint, Sperre

Neu, nur auf dem Tages-Pfad:

```text
TVA_ROOT/days/YYYY-MM-DD/current
TVA_ROOT/days/YYYY-MM-DD/builds/<ulid>/day.json
TVA_ROOT/days/YYYY-MM-DD/lock
```

`schema_version: "1"`. Session-Schema bleibt `"1"`. Der Tagesschlüssel ist der Vienna-Kalendertag von `nominal_start`, derselbe Tag wie Session-Id, Notion-Titel und Ledger-`session_date` (CD1). Nicht die ETH-`session_date`. `nominal_start` und `nominal_end` werden in UTC gerechnet (`start_utc + duration_s`, wie `fills.py:126-127`) und zur Anzeige nach Vienna gelegt. Addieren auf einer aware-Vienna-Zeit verschiebt das Ende um eine Stunde, wenn die Spanne die Umstellung am 25.10.2026 schneidet.

`day.json` enthält:

- `input_fingerprint`: kanonisches SHA-256 über die sortierte Clip-Liste `(session_id, recording.sha256, duration_s, part-sha256 in Reihenfolge)`, die sortierten Identitätsschlüssel aller Fills mit Vienna-Tag D und aller Fills in Kernen von `clips(D)`, `venue`, `include_manual`, der Pfad von `--reconcile-dir` oder leer, `--provider` oder leer, die aktiven Flag-Namen, `schema_version`, `app_version`. Die SHA der Executions-CSV steht daneben als `executions_sha256` und geht nicht in den Hash.
- die Clip-Liste unten.
- `outside`: Fill-Schlüssel mit Grund.
- `trades_outside_clips`: Anzahl, nicht die Rate.

Pro Clip:

| Feld | Bedeutung |
|---|---|
| `session_id`, `filename` | wie heute |
| `nominal_start`, `nominal_end` | UTC-Rechnung, Anzeige Vienna |
| `duration_s` | Mediendauer, nicht Wandspanne |
| `stitched_gap_s` | größte absolute Lücke innerhalb einer Auto-Split-Kette; leer, wenn keine Teile |
| `alignment_method`, `alignment_confidence`, `alignment_offset_s`, `alignment_drift_s_per_h` | Fit, bevor dieser Lauf `invalid` setzt. Ohne Invalidieren der aktuelle Fit. Quelle ist `pause_checks`, nicht ein neuer Fit |
| `pause_check` | Kopie aus `pause_checks`. Ist `TVA_PAUSE_GUARD` an und die Datei fehlt oder der Schlüssel `(recording_sha256, Teil-SHAs, duration_s)` passt nicht, bricht der Bau ab. Ist der Guard aus, steht hier `guard_off`, und der Fit bleibt. Ein `suspected` ohne Guard-Datei gibt es dann nicht |
| `pause_total_s` | `y_last − y_first` der akzeptierten Proben |
| `overlap` | andere Session-Ids, wenn Kerne sich überdecken; sonst leer |
| `near_boundary` | Fill-Schlüssel innerhalb ε der Kernkante (CD13). Besitz ändern sie nicht |

Geschrieben wird die Datei auf dem Tages-Pfad, sobald eines der Flags aus 3.6 den Bau verlangt. Ein einzelner Clip ohne `suspected` bekommt kein `days/`. Sein Guard-Ergebnis liegt trotzdem in `pause_checks/` (2.2).

Ein zusammengelegter Auto-Split ist **eine** Session und damit ein Clip. Die Teile stehen weiter in `recording.parts`.

**Veraltet.** Die Prüfung sitzt in `day_state(root, date)`, nicht in jedem CLI-Befehl einzeln. Sie liefert `legacy`, `current`, `stale` oder `missing`. `stale` oder `missing` auf einem Tages-Pfad wirft `DayStale` mit dem Text `Tag veraltet, tva day build ausführen`. Der Leser fällt nicht auf den Legacy-Stand der einzelnen Session zurück und baut nicht nebenbei neu. Nur `tva day build` ersetzt den Zeiger.

Aufgerufen wird `day_state` von den Store- und Ledger-Lesern, die Fakten eines Tages ausgeben: `compute_status` (`store.py:297`, der Status nennt den Tag `stale`), `ledger_summary`, `window_session_ids`, `window_trade_facts`, `tva coach` (`coach.py:174-200`), `tva serve` unter `/ledger/summary` (`serve.py:227-231`) und `/coach/latest` (`serve.py:233-238`), `tva report`, `tva context`, `tva proposals`, `tva rollup` mit und ohne `TVA_TRADING_HOURS`, dazu `tva fills`, `tva evidence`, `tva rules`, `tva ledger add` und `tva publish`. Eine Zusammenfassung, die den veralteten Tag still in Stunden oder Trades addiert, gibt es nicht. Der Fehler nennt die Tage.

Ein einzelner Clip, der noch nicht `suspected` ist, bleibt Legacy und hat kein `current`. Kommt der zweite Clip, ist der Tag ab diesem Lauf ein Tages-Pfad, und der nächste Leser bricht ab, bis gebaut wurde.

**Kaskade.** Reihenfolge, fest:

1. Bestätigte und abgelehnte Proposals jeder Session der Clip-Menge werden mit dem Fill-Identitätsschlüssel nach `days/<date>/builds/<ulid>/proposals_carry.json` kopiert. `_previous_status` liest genau die Session-Datei (`proposals.py:363-378`). `drop_proposals` löscht sie (`store.py:130-132`). Ohne die Kopie vor dem Löschen kann nichts umgehängt werden.
2. Danach löscht der Bau für jede Session der Clip-Menge: Evidence, Context, Debrief, Proposals, und die Ledger-Zeilen dieser Session (`drop_session`, `ledger.py:266-277`). Zusätzlich die Zeilen dieses Tages in `day_rollups` und `day_rule_checks` (2.4). `ingest_fills` allein tut das heute nicht (`fills.py:718-726`).
3. Neu geschrieben werden nur deterministische Artefakte: Fills, Trades, die fünf Tagesregeln, die Ledger-Zeilen, die Tageszeilen. Evidence, Context und Debrief bleiben gelöscht. `compute_status` zeigt sie `missing`. Die uhrgemappten und sprachgestützten Regeln der Session werden nicht aus dem alten Evidence neu erfunden. Sie bleiben `missing`, bis der Nutzer nach einem neuen Evidence-Lauf `tva rules` ausführt. Ein Bau mit dem Default `fake` (`cli.py:168`, `cli.py:193`) würde Grok-Ergebnisse still durch Fake ersetzen. Das passiert nicht. Ist `--provider` gesetzt, schreibt der Bau Evidence und Debrief mit diesem Provider, und der Name steht im Fingerprint.
4. Danach werden die Einträge aus `proposals_carry.json` umgehängt oder laut verworfen (2.3).

**Sperre.** `days/<date>/lock`, dasselbe Muster wie `try_acquire_lock` (`watch.py:184-204`): `O_EXCL`, Inhalt die PID, stehlen nur wenn die PID tot ist. Ein zweiter Lauf auf demselben Tag bricht laut ab. Die Sperre liegt um den ganzen Bau und um `tva publish` dieses Tages.

**Tage ohne Clip.** Fills an einem Vienna-Tag ohne Session erzeugen kein Tagesobjekt. Sie bleiben außerhalb von TVA, bis es einen Clip gibt. Das ist eine Grenze, kein stilles Zuordnen.

### 2.2 Pausen

Empfehlung: **nicht** stückweise ausrichten. Der Clip wird `alignment.method = "invalid"`, Konfidenz 0, Offset 0, Drift 0. Zeitabhängige Ausgaben werden unterdrückt. Begründung in CD5. `invalid` ist heute kein Wert von `AlignmentMethod` (`schema.py:36`). PR-29 nimmt den Wert ins Literal auf und schreibt ihn nicht. PR-30 schreibt ihn hinter dem Flag. `store.load_session` validiert (`store.py:267-268`).

Das Ergebnis steht in einer eigenen Datei, nicht nur in `session.json`:

```text
TVA_ROOT/pause_checks/<session_id>.json
```

Felder: `schema_version: "1"`, `session_id`, `recording_sha256`, die Teil-SHAs, `duration_s`, `pause_check`, `pause_total_s`, `clock_resolution_s` (1 oder 60 oder null), `ocr_provider`, `ocr_model`, die akzeptierten und die verworfenen Proben, `fit_before` (Methode, Offset, Drift, Konfidenz), `override` (`null` oder `force`). Der Schlüssel ist `(recording_sha256, Teil-SHAs in Reihenfolge, duration_s)`. `recording_sha256` allein ist die SHA des ersten Teils (`digest = dests[0][1]`, `ingest.py:40`) und ändert sich nicht, wenn ein Auto-Split-Teil dazukommt. Eine Datei zu einem anderen Schlüssel gilt als nicht vorhanden. Der Ordner liegt außerhalb von `sessions/`, damit die Byte-Gleichheit von L0 die Session-Artefakte trifft und das Guard-Ergebnis trotzdem bleibt.

`invalidate_downstream` löscht diese Datei mit (`store.py:230-264` wird in PR-30 erweitert). Ein Re-Ingest eines `suspected` Clips lässt Evidence verweigern, bis der Guard für den neuen Schlüssel gelaufen ist. Testfall dazu in PR-30.

`tva day build` liest diese Datei und öffnet das Video nicht. Neu proben tun `tva align` und, wenn die Datei für den aktuellen Schlüssel fehlt, `tva evidence` und `tva rules`. Dieselbe Funktion. Proben werden nicht gerechnet und danach verworfen.

Mit `TVA_PAUSE_GUARD` an gilt: `tva evidence` und `tva rules` ohne gültige `pause_checks`-Datei für den aktuellen Schlüssel führen den Guard selbst aus, wenn die Videodatei lesbar ist. Ist sie es nicht, brechen sie laut ab. Sie rufen `effective_alignment()` in diesem Zustand nicht auf. Mit Flag aus gilt 1.2 unverändert, außer dem schon geschriebenen `invalid` (oben). Die Unterdrückung von `invalid` liegt in PR-29, ohne Flag, damit ein Revert von PR-30 sie nicht mitnimmt (3.5).

Mit `TVA_PAUSE_GUARD` an und Provider `fake` bricht der Guard laut ab: `Guard braucht echten OCR-Provider`. `get_ocr_provider` fällt ohne `TVA_OCR_PROVIDER` auf `fake` (`ocr.py:132-136`). `FakeOcrProvider.read` liefert ohne Sidecar leeren Text und Konfidenz 0 (`ocr.py:86-95`). Null Proben wären `unverifiable`, der Fit bliebe, Evidence mappt linear. Das ist das stille Mapping, das Phase 1 abstellt. Tests dürfen Reads injizieren. Eine `pause_checks`-Datei mit `ocr_provider: fake` gilt außerhalb dieser Tests als nicht vorhanden.

Detektor. Primärsignal ist Dauer gegen Uhrzeit, nicht ein 2-gegen-1-Muster. Eine und mehrere Pausen sind monoton steigende `y`. Drei Proben an Start, Mitte und Ende verfehlen eine Mehrfach-Pause, weil keine zwei auf derselben Stufe liegen. Acht Proben und ein Nachbarframe fangen das.

- Zeiten: `inset = min(1 s, duration/4)`. Acht Punkte `t_i = inset + i · (duration − 2·inset) / 7`, `i = 0…7`. Nicht exakt 0 und nicht `duration − 0,5 s`: das sind oft Schwarz- oder Übergangsframes. Für Auto-Split-Sessions sucht der Seek über `media_for_time` (`frames.py:138-155`).
- Zu jedem `t_i` drei Reads: `t_i`, und die Nachbarn `t_i ± 2 s`, geklemmt in die Datei. Nichts davon geht nach `ocr.parquet` oder `frames/`.
- `y = (geparste Uhr − nominal_start) − video_t`, Sekunden, Uhr in Vienna wie `parse_clock`.
- Ein Read ist bestätigt, wenn mindestens ein Nachbar innerhalb der `AGREE_S` seiner Auflösung dasselbe `y` hat. Ein unbestätigter Read wird verworfen. Das ist der Schutz gegen eine einzelne falsch gelesene Uhr (etwa 360 s auf einer Sekunden-Uhr). Er invalidiert den Clip nicht.
- Auflösung des Reads: 1, wenn das Muster eine Sekundengruppe hat (`_CLOCK_COLON` oder `_CLOCK_ANY`). 60, wenn es `_CLOCK_HM` ist. `clock_resolution_s` der Datei ist 1, sobald mindestens zwei akzeptierte Proben Auflösung 1 haben. Sonst 60, sobald mindestens zwei akzeptierte Proben Auflösung 60 haben. Sonst gilt die Regel „weniger als zwei Proben“.
- Konstanten bei Auflösung 1: `AGREE_S = 5`, `PAUSE_CONFIRM_S = 30`. Nachbarn bei ±2 s können eine Sekunden-Uhr bestätigen. `PAUSE_CONFIRM_S` ist hier nicht 120. Die frühere Blindzone 31–120 s lässt eine echte Pause auf dem linearen Fit. Der Evidence-Vorlauf ist 180 s (`rules.py:59`). Eine Pause in dieser Größe verschiebt das Fenster um weniger als den Vorlauf und hängt die falsche Sprache an den Fill. Deshalb wird eine bestätigte monotone Pause ab 30 s `suspected`. Echte Uhr-Drift über eine Datei liegt darunter. Eine monotone Spreizung von 40 s ist eine Pausenfolge (M10), kein Drift.
- Konstanten bei Auflösung 60: `AGREE_S_HM = 60`, `PAUSE_CONFIRM_S_HM = 120`. Die Schwellen 5 s und 30 s gelten hier nicht. Eine HH:MM-Uhr setzt die Sekunde auf 0, also sägt `y` auf einem sauberen Clip zwischen 0 und 59 s. Mit 30 s wäre `pause_total` in etwa der Hälfte der Lagen über der Schwelle, und ein Abwärtsschritt über 5 s wäre `unverifiable`. Beides wäre falsch. Nachbarn bei ±2 s liegen meist in derselben Minute und bestätigen den quantisierten Wert. Sie machen aus HH:MM keine Sekunden. `pause_total` der Enden eines sauberen Clips bleibt unter 60 s, also unter 120 s: `clear`, nicht `suspected` (M16). Eine monotone Pause über 120 s wird `suspected` (M17, 300 s, und der KW40-Mittwoch mit mehreren Stunden). Die Blindzone auf einer Minuten-Uhr ist 31–120 s. Sie steht in Risiko 11.
- Die Alternative, jeden HH:MM-Read als unbestätigt zu verwerfen und den Clip `unverifiable` mit Grund `clock_resolution_minute` zu lassen, ist nicht die Regel. Hätte die große Uhr keine Sekunden, würde der Guard dann nie `suspected` setzen, auch nicht bei der mehrstündigen Pause. Beide Formate sind damit abgedeckt, ohne auf die Auskunft des Coach zu warten. Vor PR-30 sieht jemand einen echten Frame auf der NAS an und schreibt ins Audit-Protokoll, welches Muster die große Uhr und die ROI-Uhr treffen. `--sample-clocks` gibt es erst ab PR-30. Der Blick davor ist manuell.

Entscheidung, nachdem die Proben bereinigt sind. `AGREE_S` und `PAUSE_CONFIRM_S` in den Schritten sind die Konstanten der festgestellten Auflösung. Eine Bruch-Probe wird höchstens einmal neu gelesen. Sie setzt den Clip nicht auf `suspected`.

1. Unbestätigte Reads sind schon verworfen.
2. Der Start liegt mehr als `AGREE_S` über allen späteren akzeptierten Proben, das Ende mehr als `AGREE_S` unter allen früheren, oder eine mittlere Probe liegt mehr als `AGREE_S` außerhalb `[y_first, y_last]`: diese Probe wird verworfen und einmal am anderen Nachbar neu gelesen. Bestätigt der andere Nachbar sie und die Lage bleibt falsch, bleibt sie verworfen.
3. Weniger als zwei akzeptierte Proben → `unverifiable`. Der bisherige Fit bleibt. Ein Clip ohne lesbare Uhr wird nicht invalidiert.
4. In der verbleibenden Folge fällt ein Schritt um mehr als `AGREE_S` → `unverifiable`, nicht `suspected`. Kein Raten. Die Folge ließ sich durch Verwerfen der Bruch-Proben nicht monoton machen.
5. `pause_total = y_last − y_first` der akzeptierten Proben in Video-Reihenfolge. `pause_total ≤ PAUSE_CONFIRM_S` → `clear`.
6. `pause_total > PAUSE_CONFIRM_S` → `suspected`. Auf einer Sekunden-Uhr deckt das eine Pause (M5, 300 s) und mehrere Pausen (M13, 0 / 1 800 / 17 000 s). Auf einer Minuten-Uhr deckt es M17.

`suspected` gilt für die **ganze** Datei. Drei oder acht Proben legen die Schnittstelle nicht auf die Sekunde. Ein teilgültiger Strahl wäre wieder ein stilles Mapping. `fit_before` steht in `pause_checks`. `ocr.parquet` bleibt.

`tva align` mit Flag an läuft den Guard zuerst.

- `suspected`: `session.alignment` wird `method=invalid`, Offset 0, Drift 0, Konfidenz 0. Der Theil–Sen-Fit wird nicht als Session-Alignment geschrieben.
- `clear` oder `unverifiable`: der Fit bleibt, wie `tva align` ihn heute schreibt.
- `--force` gibt es heute nicht (`cli.py:150-161`). PR-30 fügt es hinzu, und es gilt nur, solange `TVA_PAUSE_GUARD` an ist. Ist das Flag aus, ist `--force` ein Fehler. Es schreibt den Fit trotz `suspected` und setzt `pause_checks.override = force`. Die Kommandoausgabe sagt das. Evidence mappt dann, die uhrgemappten Regeln werden nicht `alignment_invalid`. Das ist eine sichtbare Ausnahme, kein Default.
- `--manual-offset` auf einer Session mit `suspected` und ohne `--force` schreibt nichts und endet mit Fehler. Mit `--force` gilt der Offset, Drift 0, `override = force`.

Unterdrückt, sobald `method == invalid` und `override` nicht `force` ist. Die Prüfung auf `invalid` steht vor `_alignment_too_low`, sonst wird aus Konfidenz 0 der Grund „alignment confidence below threshold“:

- `tva evidence`: kein Fenster, kein `wall_to_video_t`. Ein vorhandenes `evidence.json` wird gelöscht. `Evidence` hat keinen Session-Grund (`schema.py:93-99`). Der maschinenlesbare Grund ist `pause_checks.pause_check == suspected` zusammen mit `alignment.method == invalid`, und der Regelgrund unten. Kein neues Feld an `Evidence`.
- Die uhrgemappten Regeln R-HOURLY, R-ZONE, R-TILT, R-3C-CT, R-ARRIVAL: `unverifiable`, Grund `alignment_invalid`. Die ersten drei prüfen heute nur die Konfidenz. Die letzten zwei rechnen den Fill auf die Videozeit (`rules.py:880-942`) und würden mit Offset 0 falsch verletzt oder bestanden.
- R-PLAYBOOK und R-DEFINED bleiben in der Definition. Ohne Evidence-Trades ist `_spoken_trades` leer (`rules.py:670-673`) und der Grund der bestehende `absent speech` (`rules.py:807-808`).
- R-BIAS bleibt auswertbar. `_session_has_speech` ist wahr, sobald Session-Events da sind, auch ohne Evidence (`rules.py:662-667`). Bias-Events sind Videozeit. Kein `alignment_invalid` für R-BIAS.
- R-SLTP bleibt der bestehende Grund (`rules.py:945-946`), unabhängig von Evidence.
- Transkript und Insights bleiben. Die Videozeit darin ist die Dateizeit und stimmt auch bei einer Pause.
- Proposals bleiben nicht an der alten `tva_trade_id` hängen (1.11). Die Kaskade sichert bestätigte und abgelehnte Einträge vorher und hängt sie danach um oder verwirft sie laut (2.1). Sie löscht nicht zuerst und hängt dann aus einer leeren Datei um.

Nicht unterdrückt: die Fills selbst als Fakten mit ihrer eigenen Uhr. Ein invalidierter Clip beansprucht keine Fills (2.3). Die Tagesregeln zählen sie weiter (2.4).

### 2.3 Jeder Fill genau einmal

Gesperrt durch CD11. PR-32 wird nicht gemerged, bis `+0000` als echtes UTC bestätigt ist. Der Nachweis sind mindestens drei Fills, die im Video sichtbar sind, und ein Tag nach dem 25.10.2026. TVA liest keinen DOM. Was zählt, ist ein Frame oder Screenshot, auf dem die Uhr und der Fill (Zeit, Seite, Preis) zusammen sichtbar sind, manuell protokolliert in `days/audit.json` mit Session-Id, Videozeit und CSV-Zeitstempel. Accumu kann das Gate schriftlich aufheben. Dieselbe Aufhebung gilt in PR-28, hier, in CD11 und in Risiko 9. Sie steht im Audit-JSON und, falls danach gebaut wird, in `day.json` als `tz_assumption: waived`. Ohne diesen Vermerk bleibt PR-32 gesperrt. Bis dahin beschreibt dieser Abschnitt die Regel, der Code bleibt beim heutigen Fenster.

Der Vienna-Tag eines Zeitstempels ist `timestamp.astimezone(VIENNA).date()`, und nur dann, wenn CD11 die Zeitstempel als UTC bestätigt hat. Nicht `timestamp.date()` auf dem gespeicherten Wert, und nicht `FillRecord.session_date`.

Ein Clip hat einen Kern, wenn er nicht `suspected` ist (`pause_check` fehlt nur bei Flag aus; mit Flag an ist die Datei da und `clear` oder `unverifiable`) und `alignment.method` nicht `invalid` ist, oder `override == force`. Der Kern ist halboffen:

```text
[nominal_start, nominal_end)
```

aus Dateiname und Dauer, in UTC gerechnet, nicht aus Offset und Drift. Offset und Drift bleiben für Evidence **innerhalb** eines Clips, den der Fill schon besitzt. Sie entscheiden nicht den Besitz.

Ein `suspected` Clip ohne `--force` hat einen leeren Kern.

Algorithmus, eine Reihenfolge. `clips(D)` ist die Clip-Menge von D. `neighbors(D)` sind Sessions mit Start an D−1 oder D+1, deren nominelles Intervall D schneidet. `D` im Algorithmus ist der Vienna-Tag des Fill-Zeitstempels.

```text
owner(fill):
    D = vienna_date(fill.timestamp)
    cores = every non-empty core in clips(D) ∪ neighbors(D)
            whose interval contains fill.timestamp
    if cores is empty:
        reason = no_core if clips(D) has no core else
                 before_first if timestamp < first core start of clips(D) else
                 after_last if timestamp >= last core end of clips(D) else
                 gap
        if timestamp is within ε of any core edge of clips(D):
            mark near_boundary (does not change owner)
        return outside(D, reason)
    chosen = min(cores, key=(nominal_start_utc, session_id))
    record overlap on chosen when other cores remain
    return chosen, recorded on vienna_date(chosen.nominal_start)
```

`tva day build D` betrachtet jede CSV-Execution, deren Zeitstempel in einem Kern von `clips(D)` liegt, auch nach Mitternacht, und jede Execution mit Vienna-Tag D. Liefert `owner` einen Clip, dessen Start nicht auf D liegt, schreibt dieser Bau die Execution nicht. Der Bau des Starttags schreibt sie, weil sie in seinem Kern liegt. Ein Fill nach Mitternacht steht damit nur im Tag des Clip-Starts (M12) und nicht noch einmal als `outside` des Folgetags. Der Folgetag sieht den Clip über `neighbors` und gibt ihn an den Starttag zurück.

Gemischte Pfade über Mitternacht. Tag D ist Legacy, das Pad reicht 30 min über Mitternacht. Tag D+1 ist Tages-Pfad. Ein Fill nach Mitternacht, nach dem Kernende von D und noch im Legacy-Pad, steht heute im Parquet von D. D+1 nimmt ihn nicht als `outside`. Er wird `claimed_by_legacy_neighbor` gelistet und bleibt im Legacy-Parquet. Die Tagesregeln von D+1 zählen ihn nicht noch einmal.

Grenze: Zeitstempel gleich `nominal_end` von A und gleich `nominal_start` von B gehört zu B. Bei gleichem Präfix und Abstand ≤ 5 s gibt es dieses Paar nicht, weil A und B eine Session sind (1.7). Der Test dafür ist M2 mit verschiedenen Präfixen oder mit Abstand ≥ 6 s.

Heute ist das Fenster beidseitig geschlossen, inklusive Pad (`fills.py:140`). Auf dem Legacy-Pfad bleibt das so. Fills außerhalb dieses Pads auf einem Ein-Clip-Tag bleiben draußen (CD2). Ihre Anzahl `fills_outside_window` schreibt der Audit, nicht `session.json` und nicht `rules.json`.

Identität für „derselbe Fill“: `(timestamp UTC, side, price, qty, instrument, contract_month, contract_year, source_group_id)`. Nicht `fill_id` (1.8). Zwei echte Fills mit demselben Schlüssel in derselben Sekunde bleiben zwei Einträge. Der CSV-Zeilenindex ist nur der Tie-Break der Sortierung, keine Verschmelzung.

Pairing für die Tagesregeln läuft **einmal** über zugewiesene und `outside`-Fills. Ein Round-Turn, der in Clip A aufgeht und in Clip B zugeht, ist ein Trade. Das Session-`trades.parquet` enthält die Teilmenge, deren Entry-Fill der Session gehört, mit der Tages-`tva_trade_id`. Kein zweites Pairing auf der Teilmenge. Liegt der Exit außerhalb des Clips, endet das Evidence-Fenster am Clipende und trägt `exit_outside_clip`. Liegt der Entry `outside` und der Exit in einem Clip, gibt es kein Evidence-Fenster: Evidence hängt am Entry-Besitzer, und `outside` hat keinen.

`tva_trade_id` ist `T01…` in der Reihenfolge `(entry_timestamp, trade_id)` über **alle** Trades des Tages-Pairings, `outside` eingeschlossen (`fills.py:145-150` ist die heutige Sortierung, die Menge ist auf dem Tages-Pfad die Tagesmenge). Auf dem Legacy-Pfad ist die Menge dieselbe wie heute, die Ids auch.

Bestätigte und abgelehnte Proposals werden aus `proposals_carry.json` am Fill-Identitätsschlüssel geprüft, nicht an `T01` (2.1). Passt der Schlüssel und nur die Id hat sich geändert, wird der Eintrag umgehängt und der Bau nennt die alte und die neue Id. Passt der Schlüssel nicht, wird der Eintrag laut verworfen, nicht still auf einen anderen Trade gelegt (1.11). Die Session-Datei ist zu diesem Zeitpunkt schon gelöscht. Die Kopie ist die Quelle.

ε ist 10 s (CD13). `near_boundary` steht im Manifest. Es ändert den Besitzer nicht, solange CD13 bei der Empfehlung bleibt.

### 2.4 Regeln auf Tagesebene

Nur diese fünf Tagesregeln, und nur auf dem Tages-Pfad, über das eine Tages-Pairing, `outside` eingeschlossen. Das sind nicht die fünf uhrgemappten Regeln aus 2.2.

- R-DLL, R-MAX10, R-3L30, R-5M, R-REENTRY

Dieselbe Funktion wie heute (`rules.py:396-586`), andere Eingabemenge. Schwellen bleiben `rules.yaml`. R-DLL bleibt unverifiable, solange `daily_loss_limit_usd` null ist. R-MAX10 zählt weiter Round-Turns, nicht die 88 Executions.

Geschrieben nach `days/<date>/builds/<ulid>/rules.json`, einmal.

In jedem Session-`rules.json` dieses Tages werden dieselben fünf auf `unverifiable` gesetzt, Grund `evaluated on day YYYY-MM-DD`. Der Rollup zählt `violated` über die Tageszeilen, nicht noch einmal über diese Session-Zeilen.

**Ledger.** Keine Pseudo-Session `day:YYYY-MM-DD`. `is_safe_path_name` lässt den Doppelpunkt durch (`store.py:199-207`). `window_session_ids` filtert nur damit (`ledger.py:949-971`). Der Coach liest genau diese Ids (`coach.py:174-200`). `_latest_week` sortiert `sessions` (`ledger.py:715-718`). Eine Zeile `day:` würde Coach-Eingaben ändern und wäre unter Windows ein NTFS-Stream-Trenner. Stattdessen zwei neue Tabellen, die der Rollup ausdrücklich joint und die der Coach nicht liest:

- `day_rollups(day, trade_count, hours, trades_outside_clips, trades_per_hour_reason)`
- `day_rule_checks(day, rule, status, reason)` mit Primärschlüssel `(day, rule)`

`day` ist `YYYY-MM-DD`, ohne Doppelpunkt. Alte Ledger-Dateien bleiben lesbar: die neuen Tabellen fehlen, der Rollup behandelt das wie „kein Tages-Pfad“. Session-Zeilen und Session-Spalten bleiben, was sie sind. `drop_session` löscht die Tageszeilen nicht. Der Tagesbau und der Rollback von PR-33 löschen sie mit einem eigenen Kommando, das im PR-Text steht.

Akzeptanz: drei Clips, eine Tagesverletzung von R-MAX10 → Rollup `violated = 1`, nicht 0 und nicht 3. Die Session-Zeilen der fünf Regeln sind `unverifiable` und zählen nicht als zweiter Verstoß. Der Coach-Pack enthält die Tageszeile nicht.

R-CLOSE bleibt pro Session (CD9). `flat_by` ist null, die Regel ist heute unverifiable. `_record_session_date` rechnet den Dateistart nach America/New_York (`rules.py:1121-1125`), das Ledger nach Vienna (`ledger.py:506`, `context.py:121-141`). Solange `flat_by` null ist, ändert das keine Zahl. Phase 1 zieht R-CLOSE nicht auf den Vienna-Tag. Die uhrgemappten Regeln folgen 2.2. R-BIAS und R-SLTP bleiben, wie dort beschrieben.

### 2.5 Notion: eine Seite pro Tag

Empfehlung CD6, Inhalt CD14: **eine** Seite, Titel unverändert `<D Mon YYYY> Session Debrief`. Publish schreibt auch künftig nur Properties (`publish.py:370-381`), keinen Body mit Abschnitten.

Der Bug ist der viele Schreiber, nicht der Titel. `tva publish <irgendeine Session des Tages> --notion` nimmt die Tagessperre, liest `days/<date>/publish.json` und ist idempotent auf dieser `page_id`. Die Session-Datei `sessions/<id>/publish.json` wird auf dem Tages-Pfad nicht zur Quelle der `page_id`.

Properties, deterministisch, ohne neuen Prosa-Auftrag:

- Summaries: ein fester Satz aus dem Manifest. Er nennt die Zahl der Clips, die Zahl `suspected`, die fünf Tagesregeln als pass/violated/unverifiable, und `trades_outside_clips`. Kein Urteil.
- Learnings: die ersten drei nicht leeren Learning-Zeilen der Clip-Debriefs, sortiert nach `(nominal_start_utc, session_id)`. Fehlen sie, drei leere Zeilen, wie `_learning_lines` heute auffüllt (`publish.py:120-130`).

Ein invalidierter Clip trägt in diesem Satz das Wort `alignment_invalid` und keine Fill-auf-Videozeit-Aussage. Gibt es für den Titel schon eine Seite aus einem Clip-Publish, überschreibt der erste Tages-Publish sie. Das Ergebnis des Befehls sagt `replaced_existing_page: true`. KW38–KW40 werden nicht angefasst, solange CD7 beim Audit bleibt.

`append_run_log` hängt weiter bei jedem erfolgreichen Publish eine Zeile an (`publish.py:796`). Das bleibt so. Zwei gleichzeitige Publishes serialisiert die Tagessperre, damit nicht zwei Seiten entstehen.

Auf dem Legacy-Pfad ist der Payload `payload_from_debrief` wie heute (`publish.py:140-150`), Feld für Feld gleich.

### 2.6 Trades/Stunde

Zähler und Nenner haben dieselbe Basis. Ein Trade zählt im Zähler genau dann, wenn sein Entry-Fill einem berechtigten Kern gehört. `outside`- und `no_core`-Trades stehen in `trades_outside_clips` und nicht im Zähler. `day_rollups.trade_count` ist dieser Zähler. R-MAX10 zählt die weitere Menge inklusive `outside` (2.4) und liest `trade_count` nicht. Die außerhalb liegenden Trades stehen auch nicht in der `trades`-Tabelle des Ledgers: `stated_lab_agree is None` würde sie als unverifiable im Stated-vs-Lab-Tally zählen (`ledger.py:598-614`).

Der Nenner ist die Handelszeit aus CD3, Empfehlung (c): pro berechtigtem Clip die Spanne vom ersten eigenen Fill-Zeitstempel bis zum letzten, Vereinigung über überlappende Spannen, Summe. Ein `suspected` Clip trägt nicht bei. Eine Wandspanne aus dem kaputten Alignment wird nicht eingesetzt. Ein Clip mit weniger als zwei eigenen Fills hat die Spanne 0. Trades, deren Entry auf so einem Clip liegt, zählen nicht im Zähler. Sie stehen in `trades_span_zero`. Sonst erhöhte ein einzelner Entry, dessen Exit in einem anderen Clip liegt, die Rate, ohne den Nenner zu vergrößern.

`trades_per_hour = trade_count / hours`, wenn `hours > 0`. Sonst null. `_tph` gibt bei `hours <= 0` schon null zurück (`ledger.py:555-558`).

Gründe, ein Wert, Schlüssel fehlt in der Serialisierung, wenn null, damit L0 kein neues Feld sieht. Es gilt der erste zutreffende:

- `paused_clip`, wenn die Periode keinen berechtigten Kern hat. Die Rate ist null. `trades_outside_clips` trägt die Zahl daneben. M5 fällt hierher, nicht in eine Mischrate.
- `span_undefined`, wenn jeder berechtigte Entry auf einem Clip mit Spanne 0 liegt. Die Rate ist null. `trades_span_zero` trägt die Zahl.
- `span_zero_excluded`, wenn `trades_span_zero > 0` und die Spanne über 0 ist. Diese Trades sind nicht im Zähler.
- `outside_trades_present`, wenn `trades_outside_clips > 0` und die Spanne über 0 ist. Die Rate bleibt die Rate der berechtigten Trades mit positiver Spanne. Sie mischt die außerhalb liegenden Trades nicht in den Zähler.
- `mixed_basis`, wenn keiner der vorigen Gründe greift und `hours_basis` gleich `mixed` ist.

`hours_basis` ist ein eigenes Feld: `duration` auf einem reinen Legacy-Zeitraum, `fill_span` auf einem reinen Tages-Pfad, `mixed` wenn beides in derselben Periode liegt. Es fehlt in der Serialisierung, wenn der Zeitraum nur Legacy ist und `TVA_TRADING_HOURS` aus ist, damit L0 kein neues Feld sieht. Bei `mixed` bleibt `hours_basis` gesetzt, auch wenn der Grund `outside_trades_present` oder `span_zero_excluded` ist. Die Rate ist die Summe der Zähler geteilt durch die Summe der Nenner, jeder Tag mit seinem eigenen Nenner. Sie ist damit als gemischt gekennzeichnet und wird nicht als eine Definition von Handelszeit gelesen.

Der Rollup erkennt einen Tages-Pfad daran, dass `day_rollups` für dieses Datum eine Zeile hat. Dann nimmt er `trade_count` und `hours` von dort und lässt die Session-Zeilen dieses Datums aus Summe und Zählung der Rate heraus. Session-`hours` bleiben `duration_s / 3600`. Ein rohes `SUM(sessions.hours)` ist nicht die Rate. Ohne Zeile in `day_rollups` gilt die heutige Zählung: `COUNT(*)` der Trade-Zeilen und `SUM(hours)` (`ledger.py:667-701`). Auf L0 gibt es keine Tageszeile, die Zahl ist dieselbe wie heute. `day_state` läuft auch dann, wenn `TVA_TRADING_HOURS` aus ist (2.1). Ein veralteter Tag wird nicht in diese Summe genommen.

Die aligned Wandlänge `duration · (1 + drift/3600)` wird nicht benutzt.

### 2.7 Altdaten und die Zeit bis zum Einschalten

`tva day audit --from 2026-09-14 --to <Einschalttag>` ist lesend. Der Endtag ist ein Argument, nicht fest 2026-10-04. Der Default des ersten Laufs geht bis zum Tag des Laufs, damit KW41 und ein Tag nach dem 25.10.2026 dabei sind, wenn es die Sessions gibt.

- Schreibt höchstens `TVA_ROOT/days/audit.json`.
- Ändert kein `session.json`, kein Parquet, kein `ocr.parquet`, keine Notion-Seite, kein `pause_checks/`.
- Die Fill-Hälfte liest die **vorhandenen** `fills.parquet` und die CSV. Sie rechnet das Fenster nicht neu, um die 20 zu erklären. Klassen gegen den Identitätsschlüssel: `in_existing_parquet`, `session_without_fills_run`, `filtered_manual` (in `fills.parquet`, nicht in `trades.parquet`, weil nicht `--include-manual`), `other_instrument` (nicht MNQ/MES). `other_account` wird `not_in_schema` geschrieben: das Parquet hat kein Konto (1.8). `venue` wird pro Datei mitgeschrieben, das ist die eine Spalte aus `fills.py:36`. Zusätzlich, als eigene Zahlen und nicht als die 20/88-Antwort: `in_one`, `in_many`, `outside_all` auf dem heutigen `session_utc_window`. Diese Fensterzahlen und `fills_outside_window` tragen `tz_assumption: csv_offset_as_is`. Sie setzen den CSV-Offset voraus und wenden ihn nicht als UTC an. Eine schriftliche Aufhebung des Gates ist `tz_assumption: waived` (2.3).
- Loader: `unknown`, außer der Operator übergibt ihn. Das Parquet speichert ihn nicht (1.8). Die offene Ursache bleibt Import gegen Mirror.
- Verschiebungen, Vorzeichen fest: angenommene wahre Zeit = CSV-Zeitstempel + Δ, Δ in {−6, −5, −4, −2, −1, +1, +2} Stunden. Der Audit schreibt die Zähler. Er wendet Δ nicht an. −4 und −5 sind die New-York-Offsets, wenn eine Lokalzeit als `+0000` beschriftet wäre. −6 ist der Sommer-Abstand Vienna gegen New York, falls jemand Vienna als UTC beschriftet hat.
- `fills_outside_window`: pro Session die Executions der CSV außerhalb des heutigen Pads, als Zahl. Nicht in die Session geschrieben (CD2).
- `--sample-clocks` erst, wenn PR-30 da ist. Die Proben landen nur im Audit-JSON. Dieselbe Funktion wie der Guard, nur lesend. Zurückschreiben in die Session nicht. Vor PR-30 reicht ein manueller Blick auf einen echten Frame (2.2), notiert im Audit-Protokoll: große Uhr und ROI-Uhr, mit oder ohne Sekunden.
- `mtime`-Hinweis aus 1.7, Spalte `mtime_pause_hint_s`, mit dem Vermerk `unverified`. Kein `pause_check` daraus.
- Stitch-Abstände: pro zusammengelegter Kette `stitched_gap_s`, und die Abstände von aufeinanderfolgenden Dateien, die nicht zusammengelegt wurden. Das ist die Messung für CD12.
- Listet jede Session im Intervall. Erfindet keine. `2026-09-30_090221` ist dabei, wenn es sie gibt.
- Ohne `--executions` ist `fills_match: not_run`. Ohne Store ist der CI-Test synthetisch.

Bis Accumu die Flags einschaltet: an Tagen mit mehr als einem Clip kein `tva publish --notion`, und Trades/Stunde aus dem Rollup nicht als Handelszeit lesen. Beides ist der heutige Code und bleibt falsch, solange die Flags aus sind. Das Vierer-Set aus 3.6 lässt sich frühestens nach dem 25.10.2026 und der Bestätigung aus CD11 einschalten. Bis dahin gilt dieser Absatz für alle Mehrclip-Tage.

Neu rechnen ist `tva day build --from … --to … --dry-run`. Es schreibt einen Diff nach `days/audit-dry-run.json` und ändert keine Session. Der echte Bau sichert vorher den Zeiger-Build nach `days/<date>/backup/<ulid>/`. Rohvideos und die CSV bleiben unberührt. Das ist kein stiller Schritt in PR-28 (CD7).

### 2.8 Was unverändert bleibt

Transkript, Insights, Frames, Clips, VLM, Context, Proposals-Logik auf einem gültigen Clip mit unveränderten Ids, Coach-Prompt, Coach-Pack (keine Tageszeilen), `rules.yaml`-Schwellen, R-CLOSE, R-BIAS auf einem invalidierten Clip, sprachgestützte Regeln auf einem gültigen Clip, der 5-s-Stitch bis CD12, Session-Ids, das ±30-min-Fenster auf dem Legacy-Pfad. Neue Felder nur, wo Phase 1 sie selbst braucht: `pause_checks/` inklusive `clock_resolution_s` und `ocr_provider`, das Tagesobjekt, `invalid` im Literal, die Lücken `exit_outside_clip` und `near_boundary`, der Regelgrund `alignment_invalid`, der outside-Grund `no_core`, `claimed_by_legacy_neighbor`, die Tabellen `day_rollups` und `day_rule_checks`, `trades_per_hour_reason` und `hours_basis` wenn nicht null, `trades_span_zero`, `fills_outside_window` nur im Audit, `tz_assumption` im Audit.

### 2.9 Session-Id-Kollision

Der Fehler ist immer an. `TVA_ALLOW_SESSION_ID_OVERWRITE` ist der Notausgang, Default aus. Wahr ist `1`, `true`, `yes` oder `on`. Die Prüfung liegt vor `_copy_parts` (`ingest.py:38`) und damit vor `invalidate_downstream` (`ingest.py:56`), damit keine verwaiste Kopie unter `recordings/` entsteht.

| Fall | Erkennung | Verhalten |
|---|---|---|
| Auto-Split-Fortsetzung | `existing.recording.filename` gleich dem Namen des ersten Teils, und `existing.recording.sha256 == digest` (`ingest.py:40`), nur `parts` gewachsen | Überschreiben wie heute. `tests/test_ingest_and_pipeline.py:183-199` bleibt grün |
| Dieselbe Datei neu kopiert oder geändert | gleicher Dateiname, andere SHA des ersten Teils | heutiges Überschreiben, mit lauter Meldung |
| Echte Kollision | anderer Dateiname oder anderes Präfix, und andere SHA des ersten Teils | Fehler, nichts überschrieben, erste Session unverändert |

Mit `TVA_ALLOW_SESSION_ID_OVERWRITE` an wird die echte Kollision wie heute überschrieben (`ingest.py:42-56`). Das ist der Rollback, kein Default. M8 erwartet den Fehler ohne dieses Flag.

---

## 3. Parität, Determinismus, Schema

### 3.1 Parität

Fixture L0, synthetisch, in CI, ohne NAS: ein durchgehender Clip, keine Pause, Chapters optional, Executions alle im heutigen Fenster. Zwei Varianten: L0-ocr (Uhr-Fit) und L0-filename (keine Uhr, Dateinamen-Fallback).

PR-28 legt `tests/fixtures/l0_main_f44d864/` an: die Ausgaben dieser beiden Läufe mit dem Code von `main` @ `f44d864`. Jeder spätere PR vergleicht den Lauf mit allen bis dahin existierenden Flags an gegen diesen Snapshot, nicht nur gegen den neuen Code mit Flags aus. Flags aus bleibt zusätzlich grün gegen die bisherigen Tests, ohne angepasste Erwartungen.

Gleich sein müssen die Session-Artefakte: `session.json`, `fills.parquet`, `trades.parquet`, `evidence.json`, `rules.json`, `debrief.md`, `debrief.json`, Ledger-Zeilen dieser Session, Notion-Payload (Titel, Summaries, Learnings). `days/` existiert auf L0 nicht. `pause_checks/<id>.json` mit `pause_check: clear` ist erlaubt und gehört nicht zur Byte-Gleichheit. Es wird getrennt geprüft.

Vergleich: Parquet über `pyarrow.Table.equals` mit `check_metadata=False`, plus gleiche Spalten und Typen. JSON kanonisch (sortierte Schlüssel), nach Entfernen genau dieser Felder: `app_version`, Publish-`log_line`, Publish-`created`. Sonst nichts.

L0 deckt „ein Clip, Fills außerhalb des Pads“ nicht als Verhaltensänderung ab (CD2). Die Zahl `fills_outside_window` steht im Audit, nicht in den L0-Artefakten.

Echte Session: zwei unpausierte Ein-Datei-Sessions, eine mit OCR-Fit, eine mit Dateinamen-Fallback, ohne Medien (fills, trades, evidence, rules, insights, transcript, geschwärzt). Sie liegen auf der NAS, nicht in git. Begründung in Abschnitt 8. CI überspringt sie, gleiches Muster wie `docs/GOLDEN_EXCERPT.md`. Bevor irgendein Flag-Set aus 3.6 in Produktion an geht, ist der NAS-Paritätslauf beider Sessions bestanden und als Protokoll unter `TVA_ROOT` abgelegt: Datum, Commit, Ergebnis je Artefakt (CD10). Die Ids zu nennen reicht nicht.

Zusätzlich, ohne Kontodaten, committet PR-30 `tests/fixtures/shapes_no_account/`: zwei Auto-Split-Teile, eine CSV mit Offsets `+0000` und `+0200`, Uhren im Format HH:MM:SS und HH:MM. CI sieht damit die Strukturen, die der NAS-Lauf prüft, ohne eine echte Session einzuchecken.

### 3.2 Weitere Fixtures

Alle synthetisch. Die Tabelle ist der Endzustand mit den Flags, die der Fall braucht. Ein früherer PR prüft nur seinen Anteil (Abschnitt 4). Außer wo der Flag genannt ist:

| Id | Aufbau | Erwartung |
|---|---|---|
| M1 | viele kurze Clips, überlappende Pads, Kerne ohne Überlapp | jeder Fill in genau einem Parquet oder in `outside`; Rate-Zähler zählt ihn einmal |
| M2a | Fill = `nominal_end` von A, B startet ≥ 6 s später, anderes Präfix oder Abstand über 5 s | `outside`, Grund `gap` |
| M2b | Fill = `nominal_start` von B, zwei Sessions | gehört zu B |
| M2c | Fill = `nominal_start` von A | gehört zu A |
| M2d | verschiedene Präfixe, Abstand 0, Fill auf der gemeinsamen Kante | zwei Sessions, Fill gehört zu B |
| M3 | zwei Teile, Abstand ≤ 5 s, Präfix gleich | eine Session, wie `test_split_parts_become_one_session` |
| M4 | zwei Dateien, Abstand 6 s, Präfix gleich | zwei Sessions |
| M5 | eine Datei, Sekunden-Uhr, bestätigte monotone Stufe 300 s | `suspected`, `pause_checks` mit dem Schlüssel aus 2.2, `method invalid`, keine Evidence-Fenster, Fills `no_core`, Tagesregeln zählen sie |
| M6 | Sekunden-Uhr, eine Probe 60 s daneben, Nachbar bestätigt sie nicht, oder sie bricht die Monotonie | Probe verworfen, nicht `suspected`, Fit unverändert |
| M7 | Fills in der Lücke zwischen zwei Kernen | einmal `outside` Grund `gap`, in den Tagesregeln, in keiner Session-Evidence |
| M8 | dieselbe Startsekunde, Präfix verschieden, andere SHA des ersten Teils | Fehler, erste Session unverändert, keine Kopie. Mit `TVA_ALLOW_SESSION_ID_OVERWRITE`: heutiges Überschreiben |
| M9 | drei Clips, R-3L30 über Clipgrenzen | eine Verletzung in `day_rule_checks`, Rollup-`violated` 1, Coach-Pack ohne diese Zeile |
| M10 | Sekunden-Uhr, bestätigte monotone Spreizung 40 s | `suspected`. Das ist eine Pausenfolge, kein Drift |
| M11 | zwei bestätigte Proben, `y` steigt um 300 s | `suspected` |
| M12 | Fill nach Mitternacht im Kern eines Clips vom Vortag | nur im Tag des Clip-Starts |
| M13 | bestätigte Stufen 0 / 1 800 / 17 000 s | `suspected` |
| M14 | drei Clips, einmal alle vorhanden dann bauen, einmal Clip für Clip mit `tva fills` dazwischen dann bauen | identische `day.json`. Nach Clip 2 ohne Neubau: Fehler `Tag veraltet`, kein stilles Ergebnis |
| M15 | Fill 5 s vor `nominal_start` | `outside` und `near_boundary`, nicht dem Clip zugeordnet |
| M16 | sauberer Clip, nur HH:MM-Uhr | nicht `suspected`. `clock_resolution_s` 60, `clear` |
| M17 | HH:MM-Uhr, bestätigte monotone Pause 300 s | `suspected`, `clock_resolution_s` 60 |
| M18 | eine Woche mit einem Legacy-Tag und einem Tages-Pfad-Tag | `hours_basis: mixed`, Grund `mixed_basis`, wenn kein anderer Grund greift |
| M19 | ein Clip mit einem Fill und ein Clip mit einer echten Spanne | der einzelne Fill nicht im Zähler, `trades_span_zero` 1 |
| M20 | Provider `fake`, Guard an, keine injizierten Reads | lauter Abbruch, kein `pause_checks` mit `ocr_provider: fake` |

### 3.3 Determinismus

Gleiche Sessions, gleiche CSV, gleiche `pause_checks`, gleiches Optionen-Set. M14 ist der Test. Kein Zufall. Die einzige Uhr, die der Vergleich streicht, steht in 3.1. Sortierung ist der UTC-Instant (Abschnitt 2).

### 3.4 Keine stillen Fallbacks

| Lage | Sichtbar | Nicht |
|---|---|---|
| monotone bestätigte Pause über der Schwelle der Auflösung | `suspected`, Methode `invalid`, Datei in `pause_checks` mit `clock_resolution_s` | Fit stehen lassen, Wandende schätzen, „gültig bis zur Stufe“ |
| HH:MM-Uhr, `pause_total` unter 120 s | `clear`, Auflösung 60 | mit der 30-s-Schwelle `suspected` setzen |
| eine Probe ohne Nachbar-Bestätigung oder gegen die Monotonie | Probe verworfen, neu gelesen | den Clip `invalid` setzen |
| weniger als zwei akzeptierte Proben | `unverifiable`, Fit bleibt, Datei wird trotzdem geschrieben | Clip invalidieren, Ergebnis verwerfen |
| kein `pause_checks` für den aktuellen Schlüssel, Flag an | Guard läuft, oder lauter Abbruch | `effective_alignment()` |
| Guard an, OCR-Provider `fake` | lauter Abbruch | `unverifiable` und linear mappen |
| Fingerprint stimmt nicht | Fehler `Tag veraltet` | alter Stand, oder still neu bauen |
| Fill in keinem Kern | `outside` mit Grund, bei leerem Tag `no_core` | dem nächsten Clip zuschlagen |
| Fill innerhalb ε der Kante | `near_boundary`, Besitzer unverändert | ε still als Pad benutzen |
| überlappende Kerne | ein Besitzer, `overlap` im Manifest | beide Parquets |
| `outside`-Trades und berechtigte Kerne | Rate nur aus den Kernen, Grund `outside_trades_present`, Zähler `trades_outside_clips` | Mischrate |
| Nenner 0 | `trades_per_hour: null` mit Grund | Dauer oder OCR-Spanne einsetzen |
| 20/88 ohne CSV | `fills_match: not_run` | eine Ursache nennen |
| Id-Kollision, anderer Dateiname und andere SHA | Ingest-Fehler, vor dem Kopieren | Überschreiben |
| Auto-Split-Fortsetzung, gleicher erster Name und gleiche SHA | Session wächst um `parts` | als Kollision abbrechen |
| ungültige Flag-Kombination | Abbruch beim Start | die Teilmenge laufen lassen |

### 3.5 Schema und Migration

- Session-`schema_version` bleibt `"1"`. PR-29 nimmt `invalid` in `AlignmentMethod` auf (`schema.py:36`) und in die Methodenliste in `docs/04_ARCHITECTURE.md` und `docs/IMPLEMENTATION_PLAN.md`. Alte `session.json` bleiben gültig. Ein Leser ohne das Literal wirft in `load_session`.
- `pause_checks` und `day.json` haben ihre eigene `"1"`.
- `trades_per_hour_reason` fehlt in der Serialisierung, wenn es null ist.
- Kein Umbau von `ocr.parquet`, Audio, Video. Kein neues Feld an `Evidence`.
- Ledger-Spalten der Session-Zeilen unverändert. Die Tageswerte sind neue Tabellen. Alte Dateien ohne diese Tabellen bleiben lesbar.
- Bestehende Session-Ordner werden nicht umgeschrieben, solange das jeweilige Flag aus ist. PR-30 schreibt bei Flag an `alignment.method` und `pause_checks`. Flag aus löscht beides nicht.
- Die leseseitige Unterdrückung von `method == invalid` (kein Evidence-Fenster, uhrgemappte Regeln `alignment_invalid`) liegt in PR-29, ohne Flag. Ein Git-Revert von PR-30 lässt PR-29 stehen, und `invalid` wird weiter nicht linear gemappt. Ein Revert von PR-29 ist erst erlaubt, wenn kein `invalid` mehr im Store liegt. Das steht im PR-Text. PR-30 schreibt den Wert. PR-29 liest ihn und unterdrückt.
- KW41 und ältere Mehrclip-Tage: erst `--dry-run`, dann Sicherung, dann Bau (2.7). Rohdaten bleiben.

### 3.6 Flags und Rollback

Ungesetzt oder leer ist aus. Wahr ist `1`, `true`, `yes` oder `on`, wie `serve_media_enabled` (`serve.py:50`). Nicht der Stil von `TVA_EXTRACT_PROVIDER`.

| Flag | PR | An |
|---|---|---|
| `TVA_PAUSE_GUARD` | 30 | Detektor, `pause_checks`, Schreiben von `invalid`, `--force` nur solange dieses Flag an ist |
| `TVA_DAY_MANIFEST` | 31 | `day.json`, Fingerprint, Sperre, Veraltet-Abbruch |
| `TVA_EXCLUSIVE_FILLS` | 32 | Besitz. Merge erst nach CD11 |
| `TVA_DAY_RULES` | 33 | fünf Tagesregeln, `day_rule_checks` |
| `TVA_DAY_PUBLISH` | 34 | eine Seite, Properties aus CD14 |
| `TVA_TRADING_HOURS` | 35 | Rate aus 2.6 |
| `TVA_ALLOW_SESSION_ID_OVERWRITE` | 36 | Notausgang. Default aus, der Kollisionsfehler ist dann an |

`TVA_ALLOW_SESSION_ID_OVERWRITE` ist orthogonal. Es darf zu jeder Zeile unten dazukommen. Es ist keine eigene erlaubte Menge, die andere Flags ausschließt.

Erlaubte Kombinationen. Alles andere bricht beim Start des Befehls laut ab.

| Set | Rollback |
|---|---|
| alle aus | nichts zu tun. Der Kollisionsfehler aus PR-36 ist trotzdem an, solange der Notausgang aus ist |
| nur `TVA_PAUSE_GUARD`, oder mit `TVA_DAY_MANIFEST` | Flag aus. `pause_checks` löschen ist ein genanntes Kommando, kein Nebenprodukt. `invalid` bleibt lesbar und unterdrückt (PR-29). Zurück zum Fit nur mit `tva align --force`, und nur solange `TVA_PAUSE_GUARD` an ist |
| nur `TVA_DAY_MANIFEST` | Flag aus. `days/` löschen ist das Kommando |
| `TVA_PAUSE_GUARD` + `TVA_DAY_MANIFEST` + die vier `TVA_EXCLUSIVE_FILLS`, `TVA_DAY_RULES`, `TVA_DAY_PUBLISH`, `TVA_TRADING_HOURS`, alle vier zusammen | Die vier Flags gemeinsam aus, nicht einzeln. Danach aufräumen, in dieser Reihenfolge: `day_rollups` und `day_rule_checks` dieses Laufs löschen, Kommando in PR-33. Dann `tva fills` einmal mit den vier Flags aus, bevor irgendetwas anderes liest. Notion-Seite bleibt, der nächste Legacy-Publish überschreibt sie |

`TVA_DAY_PUBLISH` ohne `TVA_DAY_RULES`, oder `TVA_TRADING_HOURS` ohne `TVA_EXCLUSIVE_FILLS` und `TVA_PAUSE_GUARD`, oder eines der vier ohne die anderen drei, ist keine erlaubte Zeile. Ein Zwischenschritt, der nur eines der vier ausmacht, ist ebenfalls keine erlaubte Zeile.

Bevor irgendeines dieser Sets in Produktion an geht, ist der NAS-Paritätslauf aus CD10 bestanden und das Protokoll unter `TVA_ROOT` abgelegt.

### 3.7 Grenzen

Lesen von Videos für die Proben und für den Audit ist erlaubt. Schreiben nicht: nicht auf die NAS-Videos, nicht außerhalb `TVA_ROOT` (`sessions/`, `days/`, `pause_checks/`, `ledger/`), nicht nach `/musiclabel`. Der Audit schreibt keine Session-Artefakte.

Ein Dateiname `2026-10-25 02:00` bis `02:59` ist in Europe/Vienna mehrdeutig. `parse_obs_filename` setzt `tzinfo` ohne `fold` (`naming.py:43-44`), Python nimmt `fold=0`, die erste Stunde. Der Markt ist zu. Phase 1 löst das nicht auf und nennt es als Grenze.

---

## 4. PR-Serie

Jeder PR ist einzeln mergebar. Wo er ein Flag hat, ist der Default aus, und mit Flag aus ist der bisherige Teststand grün. L0 mit Flag an wird gegen `tests/fixtures/l0_main_f44d864/` geprüft (3.1), sobald dieser Snapshot existiert. Review durch einen eigenen Agenten, Checkliste am Ende. Nicht auf automatischen Bugbot warten.

Reihenfolge ist die Abhängigkeit. Der Fill-Audit braucht die Pausenfunktion nicht und kommt zuerst, weil sein Ergebnis 2.3 und CD11 ändern kann. Das Vierer-Set lässt sich frühestens nach dem 25.10.2026 und der Bestätigung aus CD11 einschalten. Bis dahin gilt die Übergangsregel aus 2.7 für alle Mehrclip-Tage.

Abbildung der Nummern aus dem vorigen Planstand: altes PR-28 (Manifest) → PR-31, altes PR-29 (Guard) → PR-29 plus PR-30, altes PR-34 (Audit) → PR-28, altes PR-30 (Fills) → PR-32, altes PR-31 (Regeln) → PR-33, altes PR-32 (Notion) → PR-34, altes PR-33 (Rate) → PR-35.

### PR-28 — Audit der vorhandenen Fills

- Abhängigkeit: keine.
- Flag: keiner. Nur die Audit-Datei.
- Tun: die Fill-Hälfte von 2.7, inklusive Verschiebungen, Klassen gegen vorhandene Parquets, `fills_outside_window`, `tz_assumption: csv_offset_as_is`, Stitch-Abstände, `mtime`-Hinweis. Kein `--sample-clocks` (das kommt mit PR-30). Ein manueller Blick auf die Uhr wird im Protokoll notiert (2.2). `docs/IMPLEMENTATION_PLAN.md:238` wird auf „unverifiziert, siehe CD11“ gesetzt.
- Tests: synthetische Mischung innen, in zwei vorhandenen Parquets, außerhalb, manuell, anderes Instrument, fehlende CSV → `not_run`. Keine Session-Datei ändert mtime. Vorzeichen der Verschiebung ist assertiert: wahre Zeit = CSV + Δ.
- Akzeptanz: Accumu kann CD4 und CD11 daran entscheiden. Ohne NAS ist CI synthetisch.
- Rollback: Datei löschen.
- **Gate:** PR-32 mergt nicht, bevor CD11 auf diesem Protokoll bestätigt ist. Accumu kann das Gate schriftlich aufheben. Die Aufhebung steht als `tz_assumption: waived` im Audit-JSON (2.3). Ohne den Vermerk bleibt das Gate zu. Liegt der Fehlbetrag der 20/88 am Fenster oder an der Doppelzählung, beschreibt 2.3 den Besitz weiter. Liegt er woanders, wird CD4 neu vorgelegt, bevor PR-32 mergt.

### PR-29 — Literal `invalid`, nur Leser

- Abhängigkeit: keine. Liegt vor PR-30.
- Flag: keiner. Kein Schreiber.
- Tun: `invalid` in `AlignmentMethod` (`schema.py:36`) und in den beiden Doku-Zeilen, die das Literal aufzählen. Ein `session.json` mit `method: invalid` lädt. Nichts schreibt den Wert. Die leseseitige Unterdrückung aus 2.2 liegt hier, ohne Flag: kein Evidence-Fenster, die fünf uhrgemappten Regeln `alignment_invalid`. `effective_alignment` mappt `invalid` nicht auf den Dateinamen.
- Tests: rundes Laden und Speichern. Eine Session mit `method: invalid` hat keine Evidence-Fenster. L0 gegen den Snapshot, bytegleich, weil nichts geschrieben wird.
- Rollback: nur wenn kein Store-`invalid` existiert.

### PR-30 — Pausen-Guard

- Abhängigkeit: PR-29.
- Flag: `TVA_PAUSE_GUARD`.
- Tun: Abschnitt 2.2, inklusive `--force` und der Verweigerung von `--manual-offset` ohne `--force`. `invalidate_downstream` löscht `pause_checks`. Evidence und Regeln ohne passende `sha256` laufen den Guard oder brechen ab. Die fünf uhrgemappten Regeln `alignment_invalid`. R-BIAS bleibt. `unverifiable` und `clear` werden geschrieben, auch für einen einzelnen Clip, und lassen den Fit in Ruhe.
- Tests: M5, M6, M10, M11, M13, M16, M17, M20, L0 mit Guard an (`pause_checks` ist `clear`, `clock_resolution_s` gesetzt, Session-Artefakte wie der Snapshot). Re-Ingest eines `suspected` Clips: Evidence bricht ab, bis der Guard für den neuen Schlüssel gelaufen ist. `shapes_no_account` aus 3.1.
- Akzeptanz: ein durchgehender Clip ohne Stufe bleibt auf den Session-Artefakten feldgleich. Ein `suspected` Clip, auch als einzige Datei, hat `pause_checks`, `method invalid` und keine Evidence-Fenster. `day.json` erzeugt dieser PR noch nicht. Der Tages-Pfad für Regeln und Besitz kommt mit PR-31 und PR-32. Bis dahin unterdrückt der Guard nur das Zeit-Mapping.
- Rollback: 3.6. Der Audit setzt nichts auf `invalid`.

### PR-31 — Manifest, Fingerprint, Sperre

- Abhängigkeit: PR-30, weil `pause_check` aus `pause_checks` kopiert wird. Ohne Guard-Datei bricht der Bau ab, wenn der Guard-Flag an ist. Mit Guard-Flag aus und genau einer Session schreibt dieser PR kein `days/`.
- Flag: `TVA_DAY_MANIFEST`.
- Tun: Abschnitt 2.1 ohne Fill-Besitz und ohne Regeln. `day_state` in den Lesern. Veraltet-Abbruch. Die Kaskade sichert `proposals_carry.json`, löscht Evidence, Context und Debrief und lässt sie `missing`. Sie schreibt sie nicht mit `fake` neu. Regeln und Fills füllen die folgenden PRs.
- Tests: zwei Tage bleiben getrennt. M3 eine Session, M4 zwei. M14 die identischen Ausgaben und der Fehler ohne Neubau. L0 schreibt kein `days/`. Sortierung über einen synthetischen DST-String, der als Text falsch läge.
- Akzeptanz: Session-Ordner von L0 wie der Snapshot. Flag aus: kein `days/`.
- Rollback: Flag aus, `days/` des Laufs löschen, Kommando im PR.

### PR-32 — Fill-Besitz

- Abhängigkeit: PR-31, Gate PR-28 / CD11.
- Flag: `TVA_EXCLUSIVE_FILLS`. Nur im Vierer-Set aus 3.6.
- Tun: Abschnitt 2.3. `tva fills` auf einer Session des Tages schreibt den ganzen Tag oder bricht ab, wenn die Identitätsschlüssel der Tages-Executions oder die Optionen nicht zum letzten Bau passen. Eine neue SHA der ganzen CSV allein bricht nicht ab. Legacy-Fenster unberührt. `claimed_by_legacy_neighbor` aus 2.3.
- Tests: M1, M2a–M2d, M5 (Clip beansprucht nichts), M7, M12, M15, L0 Parquets wie der Snapshot.
- Akzeptanz: keine Fill-Identität in mehr als einem Session-Parquet desselben Tages. `outside` vollständig. Kaskade hat Evidence der anderen Sessions gelöscht.
- Rollback: 3.6. Kein stilles Mischparquet.

### PR-33 — Tagesregeln und Ledger

- Abhängigkeit: PR-32.
- Flag: `TVA_DAY_RULES`. Nur im Vierer-Set.
- Tun: Abschnitt 2.4. Tabellen `day_rollups` und `day_rule_checks`. Kein `day:`-Schlüssel.
- Tests: M7 zählt in R-MAX10. M9. Rollup-`violated` 1. Coach-`window_session_ids` enthält die Tages-Id nicht, weil es keine gibt. R-CLOSE und R-PLAYBOOK auf einem gültigen Clip unverändert. L0 `rules.json` wie der Snapshot. R-DLL weiter unverifiable bei null-Limit. Bestätigte Proposal wird umgehängt, wenn der Fill-Schlüssel passt, und verworfen, wenn nicht.
- Akzeptanz: die fünf Tagesregeln im Session-JSON sind `unverifiable` mit Tagesgrund. Die uhrgemappten Regeln sind das nicht, außer 2.2 setzt sie auf `alignment_invalid`. `tva rules` auf einer Session des Tages schreibt beides oder bricht veraltet ab.
- Rollback: Flag aus, Tageszeilen dieses Laufs löschen, Kommando im PR, nicht als Nebenwirkung eines anderen Befehls.

### PR-34 — Eine Notion-Seite

- Abhängigkeit: PR-33.
- Flag: `TVA_DAY_PUBLISH`. Nur im Vierer-Set.
- Tun: Abschnitt 2.5. Properties, keine neue Prosa, `page_id` unter `days/<date>/`.
- Tests: zwei Sessions, zwei Publishes, eine `page_id`, zweiter Payload ist nicht der Clip-Debrief. L0 Payload wie der Snapshot. Flag aus: bisheriges Überschreiben bleibt getestet. Gleichzeitige Publishes: einer bekommt die Sperre.
- Akzeptanz: Titelbyte gleich dem heutigen `debrief_title`. Summaries ist der feste Satz. Erster Tages-Publish auf eine bestehende Clip-Seite setzt `replaced_existing_page`.
- Rollback: Flag aus. Die Seite bleibt. Der nächste Legacy-Publish überschreibt sie. Das steht im PR.

### PR-35 — Trades/Stunde

- Abhängigkeit: PR-32 für den Besitz, PR-30 für `suspected`, PR-33 für die Tabelle. Im Vierer-Set.
- Flag: `TVA_TRADING_HOURS`.
- Tun: Abschnitt 2.6, Empfehlung CD3 (c), bis Accumu anders entscheidet.
- Tests: L0 Rate gleich, Schlüssel `trades_per_hour_reason` und `hours_basis` fehlen. M1 Zähler ohne Doppelte. M5 Nenner ohne die kurze Dauer, Rate null, Grund `paused_clip`. M7: outside-Trades nicht im Zähler, Grund `outside_trades_present`, keine Mischrate. M18 `hours_basis: mixed`. M19 `trades_span_zero`. Session-`hours` bleiben `duration_s / 3600`.
- Akzeptanz: auf L0 dieselbe Rate wie der Snapshot. Auf M5 nicht die heutige überhöhte Rate.
- Rollback: Flag aus, Rollup neu. Alte Formel, weil `day_rollups.hours` nicht mehr gelesen wird.

### PR-36 — Laute Id-Kollision

- Abhängigkeit: keine. Darf vor oder nach den anderen mergen.
- Flag: `TVA_ALLOW_SESSION_ID_OVERWRITE`, Default aus. Der Fehler ist damit an, sobald der PR gemerged ist.
- Tun: Abschnitt 2.9. Prüfung vor `_copy_parts`.
- Tests: M8 ohne Flag ist der Fehler. Mit dem Notausgang das heutige Überschreiben. `test_new_split_part_invalidates_transcript` bleibt grün.
- Rollback: Notausgang an. Kein Daten-Rewrite.

### Review-Checkliste, jeder PR

Ein separater Agent, nicht der Autor. Er hakt nur ab, was er im Diff sieht.

1. Flag default aus, und mit Flag aus ist der bisherige Teststand grün, ohne angepasste Erwartungen. PR-36 ist die Ausnahme: der Kollisionsfehler ist ohne Flag an, der Notausgang ist aus. Der bisherige Teststand bleibt grün, weil eine Auto-Split-Fortsetzung keine Kollision ist.
2. L0 mit Flag an gegen den Snapshot von `main` @ `f44d864`, Ausnahmen nur die benannte Feldliste in 3.1.
3. Kein Schreiben außerhalb `sessions/`, `days/`, `pause_checks/`, `ledger/` unter `TVA_ROOT`. Kein Video-Write. Kein `/musiclabel`.
4. Kein neuer Fallback, der bei fehlender Probe, fehlender CSV, veraltetem Fingerprint oder überlappendem Kern einen Wert erfindet.
5. Sortierung nach UTC-Instant. Test mit vertauschter Ingest-Reihenfolge und mit M14, wo der PR Tageszustand schreibt.
6. Alte `session.json` lädt. Session-`schema_version` bleibt `"1"`, außer der PR begründet eine Anhebung und migriert lesend. `invalid` wird nur geschrieben, wenn PR-29 schon gemerged ist.
7. Keine Änderung an Prompts, Coach-Eingaben, Proposals-Logik auf gültigen Clips, `rules.yaml`, R-CLOSE, R-BIAS, Sprachregeln auf gültigen Clips. `window_session_ids` sieht keine Tages-Id.
8. Phase 2 kommt im Diff nicht vor: kein Datenvertrag, kein Abschalten einer Analyse, die nicht in 2.2 als Unterdrückung bei `invalid` genannt ist. Kein neuer Modell-Auftrag für die Notion-Seite.
9. Ein einzelner `suspected` Clip hat `pause_checks` und `method invalid`. M5, M10 und M13 sind `suspected` auf einer Sekunden-Uhr. M16 ist es nicht. M17 ist es. M6 ist es nicht. M14 bricht ohne Neubau ab. M20 bricht bei `fake` ab. M8 bricht ohne den Notausgang ab.

---

## 5. Risiken

1. **Parität nur auf L0 und dem Snapshot.** Ein realer Ein-Clip-Tag mit Fills im 30-min-Pad bleibt auf dem Legacy-Pfad in den Regeln unsichtbar. Die Zahl steht im Audit (CD2). Ein `unverifiable` bleibt falsch im Fit und ist in `pause_checks` sichtbar, auch ohne `days/`.
2. **Acht Proben finden die Summe, nicht die Sekunde.** Stückweise Evidence auf pausierten Altbändern ist eine eigene spätere Serie, nicht Phase 1 und nicht Phase 2. KW40-Mittwoch bleibt für Fill-zu-Videozeit unbenutzbar, bis jemand CD5 auf Stückweise stellt. Eine falsch gelesene Uhr, die der Nachbar bestätigt, kann `pause_total` heben. Das ist das Restrisiko des Detektors. Sie invalidiert den Clip nicht, wenn der Nachbar widerspricht.
3. **5 s Stitch.** Bis CD12 bleibt die Schwelle. Wer zwei Blöcke mit gleichem Präfix will, wartet länger als 5 s. Der Audit misst die echten Abstände, bevor die Schwelle fällt.
4. **Ledger-Tabellen.** Ein vergessener Rollback lässt `day_rule_checks` stehen. PR-33 beschreibt das Löschen. Der Review prüft, dass der Rollup die Session-Zeilen der fünf Tagesregeln nicht als `violated` zählt und die Rate nicht aus Session-`hours` plus Tageszeile addiert.
5. **Halbes Flag-Set.** 3.6 bricht ungültige Kombinationen ab. Die vier Tages-Pfad-Flags gehen gemeinsam aus. Ein Parquet vom Tages-Pfad und Regeln vom Legacy-Pfad bleiben möglich, wenn danach `tva fills` nicht neu läuft. Die Aufräum-Reihenfolge steht in 3.6.
6. **DST und ETH-Datum.** Der Tagesschlüssel ist Vienna. Ein Join außen auf `session_date` kann in der Herbstwoche 25.10.–01.11.2026 und in den drei Frühlingswochen 08.–29.03. von 23:00 bis 24:00 Vienna danebenliegen. KW38–KW40 liegt davor. Der Audit berichtet beide Daten. Die Sortierung und `nominal_end` rechnen in UTC.
7. **Notion-Seite existiert schon.** Der erste Tages-Publish überschreibt sie und sagt das im Ergebnis. Wer den alten Text behalten will, kopiert ihn vorher. Es wird keine zweite Seite angelegt. KW38–KW40 bleiben unberührt, bis CD7 anders lautet.
8. **Proben lesen die NAS.** Nur lesen, nur die genannten Frames, nur wenn der Guard oder `--sample-clocks` an ist. Ein fehlgeschlagenes OCR der Uhr ist eine verworfene Probe, kein erfundener Offset.
9. **CSV-Zeitzone.** Bis CD11 ist 2.3 nicht im Code. Ein Merge von PR-32 davor weist fast jeden Fill dem falschen Kern zu, sobald `+0000` keine UTC-Zeit ist. Das Pad von 30 min verdeckt denselben Fehler heute teilweise. Die schriftliche Aufhebung ist nur `tz_assumption: waived` (2.3). Ohne den Vermerk bleibt das Gate zu. Das Vierer-Set wartet mindestens bis nach dem 25.10.2026.
10. **KW41 läuft schon.** Bis zum Einschalten bleiben Mehrclip-Tage im heutigen Code falsch. 2.7 sagt, was man bis dahin nicht als wahr liest.
11. **Minuten-Uhr.** Auf Auflösung 60 bleibt eine Pause von 31–120 s auf dem linearen Fit. Das ist die Blindzone von `PAUSE_CONFIRM_S_HM`. Eine Sekunden-Uhr hat diese Blindzone nicht. Der manuelle Blick vor PR-30 sagt, welche der beiden Uhren im Bild Sekunden hat.

---

## 6. Offene Entscheidungen

CD1–CD14. Nicht die D1–D9 aus `docs/05_ROADMAP.md`. Jede mit Empfehlung. Die Empfehlung gilt, wenn Accumu nicht widerspricht. Ein Widerspruch ändert den betroffenen PR, nicht die schon gemergten Vorgänger, solange die Reihenfolge eingehalten wird.

### CD1. Tagesschlüssel

Vienna-Kalendertag von `nominal_start`, nicht ETH 18:00 New York.

Empfehlung: Vienna. Das ist Session-Id, Notion-Titel und Ledger-Datum. Unter der Annahme echtes UTC ist das ETH-Datum im Sommer derselbe Kalendertag. Die Abweichung ist 23:00–24:00 Vienna, im Frühjahr 8.–29. März 2026 (drei Wochen) und im Herbst 25. Oktober–1. November 2026 (eine Woche). Diese Aufnahmen der NY-Morgenstunden trifft das nicht. Fills werden über den Zeitstempel zugeordnet, nicht über `session_date`. Die Annahme UTC ist CD11, nicht diese Entscheidung.

### CD2. Fills außerhalb des Pads auf einem einzelnen Clip

Die Kurzfassung sagt, Fills außerhalb der Clips zählen in den Tagesregeln. Auf einem einzelnen durchgehenden Clip ist „außerhalb“ heute alles jenseits des 30-min-Pads, und die Session-Regeln zählen es nicht. Beides gleichzeitig geht nicht mit Parität.

Empfehlung: Legacy-Pfad lässt sie in den Session-Regeln draußen. Der Audit nennt `fills_outside_window`, außerhalb der L0-Artefakte. Der Tages-Pfad (mehrere Clips oder ein `suspected` Clip) zählt sie in den Tagesregeln. L0 bleibt feldgleich.

Nicht gebaut, solange diese Empfehlung gilt: Tagesregeln auch für den Ein-Clip-Tag nur nach `days/` schreiben, ohne Session-Artefakte anzufassen. Das wäre die vollständige Zählung ohne Paritätsbruch. Accumu kann das wählen. Dann bekommt auch der Ein-Clip-Tag ein `days/`, und die Session-`rules.json` der fünf Regeln wird `unverifiable` mit Tagesgrund. Das ist eine Verhaltensänderung der Session-Datei und braucht einen eigenen Satz in 3.1.

### CD3. Was der Nenner von Trades/Stunde ist

Der Auftrag nennt die Summe der Cliplängen den Bug und will die Rate auf Handelszeit. Option (a) ist genau diese Formel. Sie ist nicht die Empfehlung.

Zähler und Nenner bleiben in jeder Option auf derselben Basis: nur Trades, deren Entry einem berechtigten Kern gehört. `outside` und `no_core` stehen daneben. Ein `suspected` Clip trägt zum Nenner nicht bei.

- (a) Summe der `duration_s` der berechtigten Clips. Das ist der heutige Nenner, ohne die Doppelzählung im Zähler. Der Auftrag nennt das den Bug.
- (b) Erster bis letzter Fill des Tages, Lücken zwischen Blöcken inklusive. Die Rate wäre niedriger. Auf L0 ungleich `duration_s / 3600`, sobald der Clip länger ist als der Fill-Span. Parität bricht.
- (c) Vereinigung der Spannen [erster eigener Fill, letzter eigener Fill] je berechtigtem Clip. Lücken zwischen Stop/Start zählen nicht. Würde (c) auch auf dem Legacy-Pfad gelten, wäre die L0-Rate ungleich dem Snapshot, sobald der Fill-Span kürzer ist als die Datei. Deshalb bleibt der Legacy-Pfad bei `duration_s / 3600`, und nur der Tages-Pfad nimmt (c). L0 ist Legacy, die Rate bleibt gleich.
- (d) Geplante Handelszeit aus einem Trading-Plan. Den Plan gibt es in TVA nicht. Nicht gebaut.

Empfehlung: (c) auf dem Tages-Pfad, Legacy unverändert. (a) wäre der Auftrag als Empfehlung wiederholt. (b) zählt Pausen zwischen Blöcken als Handelszeit. (d) hat keine Quelle.

### CD4. Ursache von 20/88

Offen, bis PR-28 die Klassen gegen die vorhandenen Parquets geschrieben hat. Keine Zeitzonen-Korrektur, kein anderes Fenster und kein Dedupe-Fix in PR-32, der nur diese Zahl erklären soll. Das ist nicht das geparkte Roadmap-D4 zum Tagesverlust. PR-32 setzt den Besitz um, wie in 2.3 beschrieben, erst nach CD11. Gestoppt wird zusätzlich, wenn der Audit zeigt, dass der Fehlbetrag nicht das Fenster und nicht die Doppelzählung ist, sondern etwas, das 2.3 nicht trifft (ein durchgängig falscher CSV-Offset, oder der Loader). Dann wird CD4 neu vorgelegt, bevor PR-32 mergt.

### CD5. Pause: invalidieren oder stückweise

Empfehlung: invalidieren, ganze Datei, Zeitmapping aus.

Stückweise bräuchte Schnittstellen auf die Sekunde, ein anderes Alignment-Objekt als der eine Strahl, und alle Leser von `wall_to_video_t` (`evidence.py:127`, `rules.py:689`). Acht Proben liefern die Summe, nicht den Schnitt. Ein Suchlauf über die Datei wäre eine zweite Uhr. Transkripte bleiben benutzbar, weil sie in Videozeit sind. Fill-zu-Videozeit auf KW40-Mittwoch bleibt aus, bis eine spätere Serie das ausdrücklich will. Diese Serie ist weder Phase 1 noch Phase 2.

### CD6. Notion

Empfehlung: eine Seite, bisheriger Titel, ein Schreiber, Properties aus dem Tag. Nicht ein Titel pro Clip mit Uhrzeit. Nicht ein Body mit Abschnitten: den schreibt der Code heute nicht (`publish.py:370-381`).

### CD7. KW38–KW40 und KW41 neu rechnen

Empfehlung: nur Audit. Markierung steht im Audit-JSON und, ab PR-30, in `pause_checks` für Läufe mit Flag, nicht als stilles Umschreiben alter Ordner. Neu transkribieren, neu alignen oder Notion überschreiben erst, wenn Accumu das nach dem Audit sagt. Das ist ein eigener Auftrag. `--dry-run` und die Sicherung in 2.7 sind der Weg, kein stiller Schritt in PR-28.

Bis dahin an Mehrclip-Tagen kein `publish --notion` und die Rollup-Rate nicht als Handelszeit lesen.

### CD8. Session-Id und 5-s-Stitch

Empfehlung: Ids nicht ändern. Die Kollision ist ein Fehler (2.9), kein Überschreiben. Der Notausgang ist `TVA_ALLOW_SESSION_ID_OVERWRITE`. Die Stitch-Schwelle ändert dieser Punkt nicht. Das ist CD12. Ein Präfix in der Id wäre eine Migration aller bestehenden Ordner und bricht jeden Pfad, der `YYYY-MM-DD_HHMMSS` erwartet.

### CD9. R-CLOSE

Empfehlung: pro Session lassen. `flat_by` ist null, die Regel ist unverifiable. Sie in den Tageskatalog zu ziehen würde eine Regel anfassen, die der Auftrag nicht nennt, ohne heute eine andere Zahl zu erzeugen.

### CD10. Welche echten Sessions die NAS-Parität sind

Offen für den Namen. Der Inhalt ist festgelegt: eine unpausierte Session mit OCR-Fit, eine mit Dateinamen-Fallback, ohne Medien, geschwärzt, auf der NAS. CI läuft ohne sie. Bevor ein Flag-Set aus 3.6 in Produktion an geht, ist der Paritätslauf beider Sessions bestanden. Das Protokoll liegt unter `TVA_ROOT`: Datum, Commit, Ergebnis je Artefakt. Die Ids zu nennen reicht nicht.

### CD11. TradesViz-Zeitzone

Unverifiziert. Die CSV kann `+0000` tragen. Ob das echtes UTC ist, weiß der Code nicht. Er speichert den Offset, den er sieht (`fills_mirror.py:334-363`), und prüft ihn nicht gegen das Video.

Empfehlung: PR-32 bleibt gesperrt, bis an mindestens drei Fills, die im Video sichtbar sind, und an einem Tag nach dem 25.10.2026 bestätigt ist, dass `+0000` echtes UTC ist. Der Nachweis ist ein Frame oder Screenshot mit Uhr und Fill, protokolliert in `days/audit.json` (2.3). TVA liest keinen DOM. Accumu kann das Gate schriftlich aufheben. Die Aufhebung ist `tz_assumption: waived` im Audit-JSON und, falls danach gebaut wird, in `day.json`. Dieselbe Regel gilt in 2.3, PR-28 und Risiko 9. Ohne den Vermerk bleibt das Gate zu. Der Audit berichtet die Verschiebungen aus 2.7 und wendet sie nicht an. CD1, das Fenster und der Besitz gelten bis dahin nur als Regel unter Vorbehalt.

### CD12. Stitch-Schwelle

Empfehlung: PR-28 misst die Abstände. Auto-Split wird unter 1 s erwartet, Stop/Start darüber. Liegt das so, wird `SPLIT_GAP_S` von 5 auf 1,5 gesenkt, in einem eigenen PR nach diesem Plan, nicht still in PR-31. Bis zur Messung bleibt 5 s. `stitched_gap_s` steht im Manifest, sobald es den Bau gibt.

### CD13. Besitz-Toleranz an der Clipkante

Die PC-Uhr und der OBS-Start können um Sekunden neben der Börsenuhr liegen. Der OCR-Offset misst genau das und entscheidet den Besitz bewusst nicht (2.3). Ein Fill wenige Sekunden vor `nominal_start` wird `outside` und verliert seine Evidence.

Empfehlung: ε = 10 s, nur als `near_boundary` im Manifest ausweisen. Nicht als Pad zum Kern schlagen. Ein stilles Pad wäre wieder eine Zuordnung ohne Regel. M15 hält das fest. Accumu kann ε später zum Besitz machen. Dann ist das ein eigener PR und ein neuer Paritätssatz, weil L0-Fills knapp außerhalb des Kerns die Session wechseln können. Auf dem Legacy-Pfad bleibt das 30-min-Pad.

### CD14. Woher Summaries und Learnings der Tagesseite kommen

Empfehlung: der feste Satz und die ersten drei Learning-Zeilen aus 2.5. Kein `tva report --day` und kein Modell. Ein Modell-Auftrag wäre eine neue interpretierende Ausgabe und liegt in Phase 2.

---

## 7. Nicht in diesem Plan

Phase 2, erst wenn Phase 1 gemerged ist und der Tages-Pfad auf einem echten Mehrclip-Tag gelaufen ist: ob TVA Analysen abschaltet, umbaut, oder dem Trading Coach einen Datenvertrag gibt (Transkript, Manifest, Uhrzeit nur wenn verifiziert, Vollständigkeitszahlen, keine Urteile). Fakten-Spezifikation und die zweite, unabhängige Rechnung aus dem Rohexport gehören dorthin. Sie sind hier nicht entworfen, damit eine Abweichung nach Phase 1 nur eine Ursache hat.

Nicht hier: Stückweise-Alignment (CD5), die Senkung der Stitch-Schwelle vor der Messung (CD12), ε als Besitz (CD13), Tagesregeln auf dem Ein-Clip-Tag (die Alternative in CD2).

---

## 8. Abweichungen vom Review

Zwei Punkte, beide aus dem ersten Review. Das zweite Review hat sie akzeptiert. Die Abweichung zur Id-Kollision ist zurückgezogen: 2.9 macht den Fehler zum Default und unterscheidet die Auto-Split-Fortsetzung. Die Punkte N1–N15 des zweiten Reviews stehen in den Abschnitten oben.

### 8.1 Echte Session-Artefakte nicht nach git (W8, zweiter Satz)

Der Review will eine geschwärzte echte Session ohne Medien als CI-Fixture einchecken. `docs/GOLDEN_EXCERPT.md:3-4` legt den geschwärzten Ausschnitt auf die NAS, „never in git“. Roadmap D8 (`docs/05_ROADMAP.md:161`) sperrt echtes Band gegen ein Übungskonto; die Ablage „nicht im Repository“ steht im Golden-Rezept, nicht in D8. Fills und Trades einer echten Session sind Kontobewegung, auch ohne Video.

Stattdessen: der synthetische Snapshot von `main` @ `f44d864` wird eingecheckt (3.1), dazu die kontolose Struktur-Fixture `shapes_no_account`. Die beiden echten Sessions bleiben auf der NAS. CI überspringt sie. Bevor ein Flag-Set in Produktion an geht, ist der NAS-Lauf bestanden und das Protokoll unter `TVA_ROOT` abgelegt (CD10). Der Vergleich (Parquet ohne Metadaten, JSON ohne die benannte Feldliste) gilt für beide.

### 8.2 Der Fill-Audit läuft nicht vor dem Merge dieses Plans (B5, letzter Satz)

Der Review nennt als bessere Variante, den lesenden Abgleich noch vor dem Merge dieses Plans auf der NAS auszuführen. Dieser PR ändert nur das Plan-Dokument. Im Repository liegen kein Export und keine `fills.parquet` (Abschnitt 1). Ein NAS-Lauf wäre eine Datenaktion, die der Auftrag für diesen PR ausschließt.

Stattdessen ist der Fill-Audit der erste Umsetzungs-PR (PR-28) und das Gate vor PR-32. Sein Ergebnis kann 2.3 und CD11 noch ändern. Der Merge des Plans wartet nicht auf einen Lauf, den dieses Repository nicht enthält. Die schriftliche Aufhebung des Gates ist überall dieselbe: `tz_assumption: waived` (2.3).
