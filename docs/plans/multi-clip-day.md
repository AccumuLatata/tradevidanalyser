# Phase 1 — Mehrere Clips pro Tag und Pausen

**Status:** Plan. Keine Code-, Konfigurations-, Test- oder Datenänderung in diesem PR.
**Stand geprüft:** `main` (Coach, PR-27).
**Danach:** Review durch Accumu und den Trading Coach. Umsetzung erst als die PR-Serie unten, jeder PR mit eigenem Review.
**Nicht in diesem Plan:** Phase 2 (Analysen abschalten, umbauen oder einen Datenvertrag für den Trading Coach). Alle bestehenden Analysen, Ausgaben und Schemas bleiben inhaltlich, was sie sind. Geändert wird nur, was nötig ist, damit sie bei mehreren Clips pro Tag und bei Pausen nicht mehr still falsch sind.
**Entscheidungen:** CD1–CD10 in Abschnitt 6. Das sind nicht die gesperrten D1–D9 aus `docs/05_ROADMAP.md`. Roadmap-D4 (Tagesverlust 100 gegen 200) bleibt geparkt; `rules.yaml` und der Grundtext `daily_loss_limit_usd is unset (D4)` in `rules.py` meinen dieses Roadmap-D4.
**Seriennummern:** PR-28…PR-34 setzen `docs/IMPLEMENTATION_PLAN.md` fort (zuletzt gelandet PR-27). Das sind keine GitHub-Pull-Request-Nummern.

---

## 1. Befund

Geprüft gegen den Code, nicht gegen die NAS. Unter `TVA_ROOT` liegen in diesem Repo keine KW38–KW40-Sessions und kein TradesViz-Export. Wo der Code die Ursache nicht hergibt, steht das als offene Frage, nicht als Annahme.

### 1.1 Lineares Alignment, und was „low“ heute nicht tut

Die Uhrzeit ist ein einzelner Strahl über die ganze Datei:

```text
wall = start_wallclock_vienna + video_t + offset_s + drift_s_per_h · video_t / 3600
```

Fit und Inverse stehen in `src/tradevidanalyser/align.py:223-237` (`compute_alignment`), `src/tradevidanalyser/evidence.py:127-141` (`wall_to_video_t`) und `src/tradevidanalyser/rules.py:689-701` (`_wall_to_video`). Der Fit ist Theil–Sen auf `y = (OCR-Uhr − Start) − video_t` (`align.py:44-69`, Messwerte `align.py:124-154`).

Ein pausierter Clip ist kein Strahl. OBS-Pause lässt eine Lücke in der Wanduhr und keine Lücke in der Datei. Die Videodauer ist die Summe der aufgenommenen Stücke, nicht `Wandende − Wandstart`.

Was der Fit damit macht:

- Ein einzelner Ausreißer zieht den Fit nicht weg. `tests/test_align.py:124-140`: eine Uhr liegt 60 s daneben, Offset bleibt ≈ 1,8 s, der Residual bleibt > 50 s. Die Konfidenz hängt an der MAD um den Residual-Median (`align.py:72-92`), ein Minderheiten-Ausreißer senkt sie kaum.
- Eine Pause ist eine Stufe, kein einzelner Ausreißer. Liegen auf beiden Seiten viele Samples, dominieren die Paare über die Stufe, und Theil–Sen macht aus der Pause eine Drift. Liegen fast alle Samples auf einer Seite (typisch, siehe 1.2), sieht die Stufe aus wie der getestete Ausreißer: der Fit bleibt bei der langen Seite, die Konfidenz bleibt hoch, die kurze Seite wird falsch zugeordnet.
- Es gibt keinen Sprung-Detektor. `Alignment` hat Offset, Drift, Konfidenz, Methode, Samples (`schema.py:36-51`). `invalid` gibt es nicht. `chapter_fill` steht im Literal und wird nirgends erzeugt.

Ohne OCR-Uhr fällt `align` auf den Dateinamen zurück: Offset 0, Drift 0, Konfidenz 0,6, Methode `filename` (`align.py:15`, `align.py:178-185`, `align.py:235-236`). `--manual-offset` setzt nur den Offset, Drift bleibt 0 (`align.py:188-202`).

**Korrektur an der Kurzfassung.** Konfidenz unter 0,8 macht nicht alle zeitabhängigen Regeln unverifiable. `ALIGNMENT_LOW = 0.8` (`rules.py:63`) wird nur in `_alignment_too_low` (`rules.py:773-781`) gelesen, und das nur von:

- R-HOURLY (`rules.py:949-958`)
- R-ZONE (`rules.py:982-991`)
- R-TILT (`rules.py:1028-1038`)

R-DLL, R-MAX10, R-3L30, R-5M, R-REENTRY und R-CLOSE lesen die Alignment-Konfidenz nicht (`rules.py:396-621`). Evidence baut die Fenster auch bei niedriger Konfidenz und setzt nur `alignment: "low"` (`evidence.py:325-363`). `evidence.py:144-154` ist der Fallback, wenn `session.alignment` fehlt, nicht der Fensterbau. Ein stilles falsches Mapping bleibt also stehen. Genau das soll Phase 1 für **Pausen** abstellen, nicht für den bisherigen Dateinamen-Fallback eines durchgehenden Clips (Parität, Abschnitt 3.1).

`parse_clock` legt die Uhr auf den Kalendertag von `Start + video_t` und klappt nur, wenn die Abweichung größer als 12 h ist (`ocr.py:142-174`). Die KW40-Lücke von etwa 5,5 h liegt darunter und würde als Residual sichtbar, **wenn** Samples auf beiden Seiten existieren. Eine Lücke über 12 h kann vom Klapper verdeckt werden. Das ist eine Grenze des Detektors, nicht der KW40-Fall.

### 1.2 OCR sieht eine Pause heute meist nicht

`tva frames` ohne `--at` nimmt nur Chapter-Marker mit Offsets −5, −2, 0, +2, +5 s (`frames.py:27`, `frames.py:127-135`). `tva ocr` liest diese Frames. Ein Clip ohne Chapters hat keine Uhr-Samples, also den Dateinamen-Fallback. Ein Clip mit Chapters nur am Anfang hat Samples auf einer Seite der Pause. Audio sieht die Pause ebenfalls nicht: die ausgelassene Wandzeit liegt nicht in der Datei, der Ton ist entlang der Videozeit lückenlos. Stille im File ist Kommentar-Pause, nicht OBS-Pause. Audio ist deshalb kein Pausensignal.

### 1.3 Fills-Fenster, doppelt und zu kurz

`tva fills` behält Fills im geschlossenen Intervall `[Start − 30 min, Start + Dauer + 30 min]` (`fills.py:35`, `fills.py:122-128`, Vergleich `fills.py:131-142`). Start und Ende kommen aus Dateiname und `duration_s`, nicht aus dem Alignment. Jede Session schreibt ihr eigenes `fills.parquet` (`fills.py:649-738`, Schreiben bei `716-717`). Es gibt keine Zuordnung über Sessions. Der Ledger-Schlüssel ist `(session_id, tva_trade_id)` (`ledger.py:205`), dieselbe Execution in zwei Sessions ist zwei Trades.

Beispiel aus dem Auftrag, nachgerechnet: `2026-09-30 09-02-21.mp4`, Dauer 51:48, Start 09:02:21 Vienna. Nominales Ende 09:54:09, plus 30 min Pad → **10:24:09**. Die Aussage „ab etwa 10:24 unbrauchbar“ ist dieses Fenster, nicht `Start + Dauer`. Fills danach fehlen in diesem `fills.parquet`. Fills davor können drin sein und werden mit dem linearen Strahl auf die Videozeit gelegt. Beides ist falsch, sobald in der Datei eine Pause steckt. Ob der Strahl vor der Pause zufällig noch stimmt, hängt von den OCR-Samples ab (1.1) und wird nicht geprüft.

Kurze Clips: zwei Blöcke, deren Pads sich überlappen (Abstand der Kerne unter 60 min), teilen sich dieselben Fills. Evidence hängt dann an einem Trade, der in dem Clip nicht zu sehen ist.

### 1.4 Tagesregeln laufen pro Session

`build_rules_report` liest nur `sessions/<id>/trades.parquet` (`rules.py:1182-1209`) und bewertet den Katalog darauf (`rules.py:1079-1095`). R-MAX10 zählt Paare (Round-Turns, offene inklusive), nicht Executions (`rules.py:440-445`). R-DLL summiert `net_pnl_currency`, ersatzweise brutto mit `fees_unknown` (`rules.py:321-329`). Schwellen: `rules.yaml:7-17` (`daily_loss_limit_usd` ist null, R-DLL damit heute unverifiable; `max_trades_per_day: 10`).

Zehn Clips mit je drei Trades liefern zehnmal „3 ≤ 10, pass“. Dieselben zehn Sessions im Ledger zählen eine Tagesverletzung zehnmal mit, weil `rule_checks` pro `(session_id, rule)` liegt (`ledger.py:211-216`) und der Rollup Zeilen zählt.

### 1.5 Notion überschreibt die Tagesseite

Titel ist `<D Mon YYYY> Session Debrief` aus dem Vienna-Kalendertag des Dateistarts (`publish.py:108-109`, `context.py:104-106`, `context.py:121-141`). `publish_session` sucht diese Seite und aktualisiert sie (`publish.py:788-793`). Der zweite Clip desselben Tages ersetzt Summaries und Learnings. Ein Titel pro Clip existiert nicht.

### 1.6 Trades/Stunde

`hours = duration_s / 3600` pro Session (`ledger.py:507`, `ledger.py:530-533`), Rollup summiert `hours` (`ledger.py:668-701`), Rate ist `trades / hours` (`ledger.py:555-558`). Das ist die Summe der Dateilängen. Für einen pausierten Clip ist der Nenner zu klein. Für mehrere gültige Stop/Start-Clips ist der Nenner die aufgezeichnete Zeit ohne die Lücken dazwischen. Der Zähler ist zu groß, sobald Fills doppelt im Ledger stehen. Beides verzerrt die Rate. Eine geschätzte Wandspanne aus einem kaputten Alignment wäre eine dritte Verzerrung. Abschnitt 2.6 legt fest, was stattdessen gilt.

### 1.7 Zusammenlegen der OBS-Teile

`discover_split_parts` (`ingest.py:97-159`): gleiche Dateiendung, gleiches Namenspräfix (`naming.py:49-55`), aufeinanderfolgende Starts, `|Startₙ₊₁ − Startₙ − Dauerₙ| ≤ 5 s` (`ingest.py:14`, `ingest.py:149-154`). Das ist der Auto-Split und muss bleiben.

Zwei Korrekturen:

- Ein normaler OBS-Name `YYYY-MM-DD HH-MM-SS` hat das Präfix `""`. Alle solchen Dateien in **einem** Ordner gelten als gleiches Präfix. Getrennt wird nur durch die 5 s (`ingest.py:14`, `ingest.py:149-154`). Ein Stop/Start innerhalb von 5 s nach dem Ende der vorigen Datei wird zusammengelegt (`tests/test_ingest_and_pipeline.py:59-86`). Zwei Sessions desselben Tages mit Stunden Abstand bleiben zwei Sessions (`tests/test_ingest_and_pipeline.py:47-56`). Den Abstand von 6 s deckt der Bestand nicht ab; das ist Fixture M4. Unterschiedliche Präfixe stitchen auch bei Abstand 0 nicht (`tests/test_ingest_and_pipeline.py:145-155`); das ist die Präfix-Regel, nicht die 5-s-Grenze.
- Die Session-Id ist `YYYY-MM-DD_HHMMSS` ohne Präfix (`naming.py:45`). Zwei Dateien mit derselben Startsekunde und verschiedenem Text davor landen auf derselben Id. Das ist ein bestehendes Kollisionsverhalten, kein Pausen-Bug. Phase 1 ändert die Id nicht (Entscheidung CD8).

### 1.8 KW40: 20 von 88 Executions

Nicht nachgerechnet. Export und `fills.parquet` sind nicht im Repo, und dieser Plan liest die NAS nicht. Aus dem Code sind das die möglichen Ursachen, in dieser Reihenfolge. Keine davon wird hier als die Ursache festgelegt.

1. **Fenster.** Es wird nur `[Start − 30 min, Ende + 30 min]` geschrieben. Ein Tagesexport gegen ein Session-Parquet, oder gegen die Vereinigung kurzer Fenster, verliert alles außerhalb. Für die pausierte Mittwoch-Datei endet das Fenster um 10:24:09. Das allein kann einen großen Fehlbetrag erklären.
2. **Vergleich gegen eine Datei statt gegen den Tag.** Jede Session hat ihr eigenes Parquet.
3. **Zeitzone, aber nicht als stiller TVA-Fallback.** Der Loader verlangt einen expliziten Offset und speichert UTC (`fills_mirror.py:334-363`). Naive Zeiten werfen, sie werden nicht als Vienna geraten. Das Fenster rechnet den Dateistart von Vienna nach UTC. Ein erfolgreicher Lauf hat also Offsets gesehen. Ein falscher Offset **in der CSV** (etwa `Z` statt `+02:00`) schiebt Fills um 1–2 h aus dem Fenster. Das wäre eine Eigenschaft der Datei, kein zweiter Uhr-Pfad im Code.
4. **`session_date` ist nicht der Vienna-Tag, erklärt KW38–KW40 aber nicht.** `trading_session_date` ist ETH 18:00 America/New_York (`fills_mirror.py:156-162`, geschrieben in `fills_mirror.py:190`). Der Fensterfilter benutzt den Zeitstempel, nicht dieses Datum. Im Sommer, Differenz 6 h, fällt das ETH-Datum auf den Vienna-Kalendertag; 18:00 New York ist dann 00:00 Vienna des nächsten Tages. KW38–KW40 2026 liegen ganz in dieser Sommerzeit (EU-Umstellung 2026 am 29. März und am 25. Oktober, US-Umstellung am 8. März und am 1. November). Auseinander laufen die Daten nur in den Wochen dazwischen, und dort nur von 23:00 bis 24:00 Vienna: ein Fill in dieser Stunde hat den nächsten ETH-Tag und denselben Vienna-Tag. NY-Morgenclips und der Audit-Zeitraum bis 4. Oktober sind davon nicht betroffen. Ein Join über `session_date` kann 20/88 in KW38–KW40 nicht erzeugen.
5. **Kein stilles Dedupe.** `fill_id` ist `tv:{Zeilenindex}:{spread}:{UTC-Sekunde}:{side}:{price}:{qty}` (`fills_mirror.py:312-324`). Dieselbe Execution bekommt bei anderer Zeilenreihenfolge eine andere Id. Der Filter wirft keine Dubletten weg. Ein Abgleich, der nur auf die Id geht, unterschätzt Treffer, wenn der Export neu sortiert wurde.
6. **Parse-Fehler erklären keine Teilmenge.** Eine unlesbare Zeile wirft und bricht den Lauf ab (`fills_mirror.py:258-271`, nacktes `MNQ` ohne Kontraktmonat inklusive). Ein geschriebenes Parquet ist vollständig für die geparsten Zeilen. Manuelle Nicht-Futures bleiben im `fills.parquet` (geschrieben vor dem Pairing) und fehlen in `trades.parquet`, solange `--include-manual` nicht gesetzt ist.

Der Audit-Befehl in PR-34 zählt jede Execution in genau eine Klasse (`in_one`, `in_many`, `outside_all`) und berichtet zusätzlich, wie die Zähler bei einer Verschiebung von +1 h und +2 h aussähen. Er wendet keine Verschiebung an. Erst dieses Protokoll entscheidet CD4. Bis dahin baut PR-30 das Fenster nicht „auf Verdacht Zeitzone“ um.

### 1.9 Altdaten KW38–KW40

| ISO-Woche | Vienna-Tage |
|---|---|
| KW38 | 2026-09-14 … 2026-09-20 |
| KW39 | 2026-09-21 … 2026-09-27 |
| KW40 | 2026-09-28 … 2026-10-04 |

KW40-Mittwoch ist 2026-09-30. Welche anderen Sessions pausiert sind, steht nicht im Repo. Der Audit markiert sie. Neu rechnen tut er nicht (CD7).

---

## 2. Teil 1 — Verhalten, das die Serie herstellt

Zwei Pfade, hart getrennt.

**Legacy-Pfad.** Der Vienna-Tag hat genau eine Session, und der Guard hat sie nicht auf `suspected` gesetzt (Flag aus, oder Ergebnis `clear` / `unverifiable`). Dann laufen Ingest, Alignment, Fills, Evidence, Regeln, Report, Publish, Ledger und Rollup durch den heutigen Code. Kein neues Feld in `session.json`, kein `days/`-Eintrag, kein anderer Nenner. Parität ist dieser Pfad, nicht ein Abgleich, der zufällig gleich aussieht. Der KW40-Mittwoch ist dieser Pfad nur, solange der Guard aus ist. Sobald er `suspected` liefert, ist es der Tages-Pfad, auch bei einer einzigen Datei.

**Tages-Pfad.** Zwei oder mehr Sessions an dem Vienna-Tag, oder mindestens eine Session mit `pause_check: suspected`. Dann gelten die Regeln unten. In der Serie bleiben die Flags in Produktion aus, bis Accumu ein reviewtes Set einschaltet. Aus heißt: dieser Code-Pfad läuft nicht. Die Flags von PR-28 und PR-29 dürfen einzeln an sein. 30, 31, 32 und 33 sind ein Satz (Risiko 5).

Der Tages-Pfad ist eine reine Funktion aus den Session-Ordnern, der Executions-CSV und den Pausen-Proben. Sortierung `(start_wallclock_vienna, session_id)`. Kein Watch-Zeitpunkt, keine Verarbeitungsreihenfolge. `tva day build <YYYY-MM-DD>` ersetzt `days/<date>/` atomar (temp, dann rename) und, sobald das jeweilige Flag an ist, die Fill-, Regel- und Ledger-Ergebnisse dieses Tages. Watch und Ingest rufen das in dieser Serie nicht auf. Ein späterer Clip macht ein vorhandenes `days/<date>/` nicht von selbst neu; veraltet ist es, bis `tva day build` läuft.

Dieselben Befehle, die heute eine Session schreiben, dürfen bei angeschaltetem Flag auf einem Tages-Pfad nicht daneben den Legacy-Stand schreiben:

- `tva align` führt den Guard aus, wenn `TVA_PAUSE_GUARD` an ist. `suspected` lässt den Theil–Sen-Fit nicht als `session.alignment` stehen. Ein späteres `tva align` ohne `--force` setzt `invalid` nicht zurück.
- `tva fills` auf irgendeiner Session des Tages schreibt den Besitz aus 2.3 für den ganzen Tag, nicht das ±30-min-Fenster dieser einen Datei.
- `tva evidence` und `tva rules` auf irgendeiner Session des Tages schreiben die unterdrückten Fenster bzw. die Tagesregeln und die Session-Zeilen aus 2.2 und 2.4.
- `tva ledger add` einer Clip-Session legt die Tageszeile mit an, sobald `TVA_DAY_RULES` oder `TVA_TRADING_HOURS` an ist.

Publish bleibt opt-in über `--notion`. `tva align --force` ist der einzige Weg, `invalid` im laufenden Code wieder zum Fit zu machen; der PR-29-Text beschreibt ihn. Flag aus macht den Guard stumm, schreibt aber ein schon gesetztes `invalid` nicht zurück.

### 2.1 Tagesobjekt

Neu, nur auf dem Tages-Pfad:

```text
TVA_ROOT/days/YYYY-MM-DD/day.json
```

`schema_version: "1"`. Session-Schema bleibt `"1"`. Der Tagesschlüssel ist der Vienna-Kalendertag von `start_wallclock_vienna`, derselbe Tag wie Session-Id, Notion-Titel und Ledger-`session_date` (CD1). Nicht die ETH-`session_date`.

Geschrieben wird die Datei, wenn `TVA_DAY_MANIFEST` an ist und der Tag mindestens zwei Sessions hat, oder wenn `TVA_PAUSE_GUARD` an ist und mindestens eine Session `suspected` ist, oder wenn `TVA_EXCLUSIVE_FILLS`, `TVA_DAY_RULES` oder `TVA_TRADING_HOURS` an ist und der Tag ein Tages-Pfad ist. Der Guard-Fall gilt auch für genau eine Datei. Ein einzelner Clip ohne `suspected` bekommt kein `days/`. `outside` und `overlap` stehen in dieser Datei; fehlt sie, legt der Fill-Besitz sie an.

Pro Clip im Manifest:

| Feld | Bedeutung |
|---|---|
| `session_id`, `filename` | wie heute |
| `nominal_start`, `nominal_end` | Dateistart und Dateistart + `duration_s`, Vienna |
| `duration_s` | Mediendauer, nicht Wandspanne |
| `alignment_method`, `alignment_confidence`, `alignment_offset_s`, `alignment_drift_s_per_h` | Fit, bevor dieser Lauf `invalid` setzt. Ohne Invalidieren der aktuelle Fit |
| `pause_check` | `clear`, `suspected` oder `unverifiable`. Fehlt, wenn der Guard nicht gelaufen ist. `unverifiable` heißt: der Guard hat gemessen und nicht entschieden |
| `pause_evidence` | die Proben, die den Status tragen; leer wenn unverifiable oder der Guard nicht gelaufen ist |
| `overlap` | andere Session-Ids, wenn Kerne sich überdecken; sonst leer |

Am Tag, nicht am Clip: `outside`, eine Liste von Fill-Schlüsseln mit Grund. Sonst keine Felder.

Ein zusammengelegter Auto-Split ist **eine** Session und damit ein Clip. Die Teile stehen weiter in `recording.parts`.

### 2.2 Pausen

Empfehlung: **nicht** stückweise ausrichten. Der Clip wird `alignment.method = "invalid"`, Konfidenz 0, Offset 0, Drift 0. Zeitabhängige Ausgaben werden unterdrückt. Begründung in CD5. `invalid` ist heute kein Wert von `AlignmentMethod` (`schema.py:36`). PR-29 erweitert das Literal, bevor irgendetwas `invalid` schreibt. `store.load_session` validiert sonst und wirft (`store.py:267-268`).

Detektor, drei Proben, nicht die Chapter-OCR. Drei Proben an Start, Mitte und Ende sehen eine einzelne Pause immer als 2-gegen-1: die Mitte liegt auf einer Seite der Stufe. Die Formulierung „eine Stufe, die nicht an einer einzelnen Probe hängt“ beschreibt dieses Muster nicht und würde jede echte Pause als Ausreißer verwerfen. Es gibt mit drei Proben kein Muster, das beide Seiten mit je zwei Proben bestätigt. `suspected` ist deshalb genau der große 2-gegen-1-Sprung.

- Videozeiten `0`, `duration/2`, `max(duration − 0,5 s, 0)`, gelesen von der Datei. Ergebnisse nur ins Manifest, nie nach `ocr.parquet` oder `frames/`. Ein Schreiben dorthin änderte den Theil–Sen-Fit und bräche die Parität, sobald jemand den Legacy-Clip später neu aligned. Bekommt der Tag kein Manifest (2.1), werden die Proben verworfen und nichts geschrieben.
- `y = (geparste Uhr − Dateistart) − video_t`, in Sekunden. Die Uhr ist Vienna, wie `parse_clock`.
- Konstanten: `PAUSE_JUMP_S = 30`, `AGREE_S = 5`, `PAUSE_CONFIRM_S = 120`. 30 s ist die Streuung, unter der der Clip `clear` bleibt. 5 s ist „dieselbe Stufe“. 120 s trennt den getesteten 60-s-Ausreißer (`test_align.py:124-140`) von einer Pause in der Größenordnung von Minuten. Eine 2-gegen-1-Lücke dazwischen bleibt `unverifiable`; das ist die Lücke des Detektors, nicht ein stiller Fit.

Es gilt die erste zutreffende Zeile.

- Weniger als zwei erfolgreiche Proben → `unverifiable`. Der Legacy-Fit bleibt. Ein Clip ohne lesbare Uhr wird nicht pauschal invalidiert. Steht der Clip in einem Manifest, steht `unverifiable` dort. Ein einzelner Clip, der nicht `suspected` ist, bekommt kein Manifest; der Audit ist dann der Ort, der das sagt.
- `max(y) − min(y) ≤ 30` → `clear`.
- Genau zwei erfolgreiche Proben und Spreizung über 30 s → `unverifiable`, nicht `suspected`. Zwei Proben sagen nicht, welche stimmt.
- Drei Proben, nächstes Paar innerhalb von 5 s, die dritte höchstens 120 s von deren Median weg → `unverifiable`. Das ist M6 (60 s). Der Fit bleibt.
- Drei Proben, nächstes Paar innerhalb von 5 s, die dritte mehr als 120 s von deren Median weg → `suspected`. Das ist M5 (300 s) und der einzige Weg zu `suspected`.
- Drei Proben, kein Paar innerhalb von 5 s → `unverifiable`, nicht `suspected`. Eine monotone Drift über die Datei ist keine Stufe. Der Fit bleibt.

`suspected` gilt für die **ganze** Datei. Wir schneiden nicht „gültig bis zur Stufe“, weil drei Proben die Schnittstelle nicht auf die Sekunde legen und ein teilgültiger Strahl wieder ein stilles Mapping wäre. Der Fit, den der Guard ersetzt, steht vorher im Manifest (`alignment_offset_s`, `alignment_drift_s_per_h` inklusive). `ocr.parquet` bleibt, ein späteres `--force` kann den Fit daraus wiederholen.

Unterdrückt, sobald `method == "invalid"`. Die Prüfung auf `invalid` steht vor `_alignment_too_low`, sonst wird aus Konfidenz 0 der Grund „alignment confidence below threshold“:

- `tva evidence`: kein Fenster, kein `wall_to_video_t`. Ein vorhandenes `evidence.json` wird gelöscht. `Evidence` hat keinen Session-Grund (`schema.py:93-99`); der maschinenlesbare Grund ist `alignment.method == "invalid"` und der Regelgrund unten. Kein neues Feld an `Evidence`, sonst bricht die Feldgleichheit von L0.
- Die uhrgemappten Regeln R-HOURLY, R-ZONE, R-TILT, R-3C-CT, R-ARRIVAL: `unverifiable`, Grund `alignment_invalid`. Die ersten drei prüfen heute nur die Konfidenz; die letzten zwei rechnen den Fill auf die Videozeit (`rules.py:880-942`) und würden mit Offset 0 falsch verletzt oder bestanden.
- R-PLAYBOOK und R-DEFINED bleiben in der Definition. Ohne Evidence-Trades ist `_spoken_trades` leer (`rules.py:670-673`) und der Grund der bestehende `absent speech` (`rules.py:807-808`).
- R-BIAS bleibt auswertbar. `_session_has_speech` ist wahr, sobald Session-Events da sind, auch ohne Evidence (`rules.py:662-667`). Bias-Events sind Videozeit und brauchen die Wanduhr nicht. Kein `alignment_invalid` für R-BIAS.
- R-SLTP bleibt der bestehende Grund (`rules.py:944-946`), unabhängig von Evidence.
- Transkript, Insights, Proposals aus dem Transkript: bleiben. Die Videozeit darin ist die Dateizeit und stimmt auch bei einer Pause.

Nicht unterdrückt: die Fills selbst als Fakten mit ihrer eigenen Uhr. Ein invalidierter Clip **beansprucht** nur keine Fills (2.3). Die Tagesregeln zählen sie weiter (2.4).

### 2.3 Jeder Fill genau einmal

Universum auf dem Tages-Pfad, jeder Fill in höchstens einem Tag. Der Vienna-Tag eines Zeitstempels ist `timestamp.astimezone(VIENNA).date()`. Nicht `timestamp.date()` auf dem gespeicherten UTC-Wert, und nicht `FillRecord.session_date`.

1. Liegt der Fill in genau einem Kern, gehört er zum Vienna-Tag von `nominal_start` dieses Clips, auch wenn der Zeitstempel auf dem nächsten Kalendertag liegt.
2. Liegt er in Kernen von Clips verschiedener Tage, gewinnt das kleinere `(start, session_id)`. Der andere Tag nimmt ihn nicht noch einmal auf.
3. Liegt er in keinem Kern, gehört er zum Vienna-Kalendertag seines Zeitstempels.

Kein ±30-min-Pad als Besitz. Ein Fill knapp nach Mitternacht im Kern eines Clips, der vor Mitternacht startet, steht dadurch nicht zusätzlich im Universum des nächsten Tages. `tva day build` eines Tages liest deshalb auch Sessions der Nachbartage, deren nominelles Intervall in diesen Tag ragt. Sonst sähe der Folgetag den Kern nicht und legte denselben Fill als `outside` ab.

Ein Clip hat diesen Kern, wenn er nicht `suspected` ist: `pause_check` fehlt, ist `clear` oder ist `unverifiable`, und `alignment.method` ist nicht `invalid`.

```text
[nominal_start, nominal_end)
```

halboffen, aus Dateiname und Dauer, nicht aus Offset und Drift. Der Dateistart ist die OBS-Startzeit und für einen Clip ohne Pause die richtige Wandzeit. Offset und Drift bleiben, was sie heute sind, für Evidence **innerhalb** eines Clips, den der Fill schon besitzt. Sie entscheiden nicht, wem der Fill gehört. Sonst würde ein schlechter Fit den Besitz verschieben.

Ein Clip mit `suspected` hat einen bekannten Start und ein unbekanntes Wandende. Sein Kern ist leer. Er bekommt keine Fills.

Besitz, in dieser Reihenfolge, stabil sortiert:

1. Liegt der Zeitstempel in genau einem Kern → dieser Clip.
2. Liegt er in mehreren Kernen (überlappende Aufnahmen) → die Session mit dem kleineren `(start, session_id)`. Das Manifest führt `overlap` mit den anderen Ids. Kein zweites Parquet.
3. Liegt er in keinem Kern → `outside`, genau ein Eintrag. Hat der Tag keinen Kern, ist der Grund `no_core`. Sonst `before_first` vor dem ersten Kern, `after_last` ab `nominal_end` des letzten Kerns, dazwischen `gap`. Ein `suspected` Clip ist kein Kern: ein Fill in seiner nominalen Spanne ist `gap` oder `no_core`, nicht ihm zugeordnet.

Grenze: Zeitstempel gleich `nominal_end` von A und gleich `nominal_start` von B gehört zu B (halboffen). Zeitstempel gleich `nominal_end` des letzten Clips, ohne nächsten Kern, ist `outside`. Heute ist das Fenster beidseitig geschlossen; auf dem Legacy-Pfad (ein Clip, keine Pause) bleibt das so, inklusive Pad. Der Grenzfall-Test für halboffen liegt auf dem Tages-Pfad.

Identität für „derselbe Fill“: `(timestamp UTC, side, price, qty, instrument, contract_month, contract_year, source_group_id)`. Nicht `fill_id`, weil die den CSV-Zeilenindex enthält (1.8). Zwei echte Fills mit demselben Schlüssel in derselben Sekunde bleiben zwei Einträge und werden über den Zeilenindex nur als Tie-Break sortiert, nicht verschmolzen.

Pairing für die Tagesregeln läuft **einmal** über die Vereinigung aus zugewiesenen und `outside`-Fills. Ein Round-Turn, der in Clip A aufgeht und in Clip B zugeht, ist ein Trade. Das Session-`trades.parquet` auf dem Tages-Pfad enthält die Teilmenge dieser Tages-Trades, deren Entry-Fill der Session gehört, mit der **Tages**-`tva_trade_id`. Kein zweites Pairing auf der Teilmenge: das machte den Trade in A offen und in B herrenlos. Liegt der Exit außerhalb des Clips, endet das Evidence-Fenster am Clipende und trägt die Lücke `exit_outside_clip`. Das ist ein neues Gap-String, kein neues Urteil.

`tva_trade_id` bleibt `T01…` in der Reihenfolge `(entry_timestamp, trade_id)` (`fills.py:145-150`). Auf dem Legacy-Pfad ist die Menge dieselbe wie heute, die Ids auch.

### 2.4 Regeln auf Tagesebene

Nur diese fünf Tagesregeln, und nur auf dem Tages-Pfad, über das eine Tages-Pairing, `outside` eingeschlossen. Das sind nicht die fünf uhrgemappten Regeln aus 2.2.

- R-DLL, R-MAX10, R-3L30, R-5M, R-REENTRY

Dieselbe Funktion wie heute (`rules.py:396-586`), andere Eingabemenge. Schwellen bleiben `rules.yaml`. R-DLL bleibt unverifiable, solange `daily_loss_limit_usd` null ist. R-MAX10 zählt weiter Round-Turns, nicht die 88 Executions.

Geschrieben nach `days/<date>/rules.json`, einmal.

In jedem Session-`rules.json` dieses Tages werden dieselben fünf auf `unverifiable` gesetzt, Grund `evaluated on day YYYY-MM-DD`. Der Rollup zählt `violated` über `rule_checks` (`ledger.py:565-586`). Unverifiable erhöht `violated` nicht. Akzeptanz: drei Clips, eine Tagesverletzung von R-MAX10 → Rollup `violated = 1`, nicht 0 und nicht 3. Die drei Session-Zeilen dürfen dabei als unverifiable mitzählen; das ist kein zweiter Verstoß.

Die Ledger-Id `day:YYYY-MM-DD` ist nur ein Schlüssel in `ledger.duckdb`. Kein Verzeichnis unter `sessions/`. `is_safe_path_name` lässt den Doppelpunkt durch (`store.py:199-207`); der Code gibt diese Id nicht an `load_session`.

R-CLOSE bleibt pro Session (CD9). `flat_by` ist null, die Regel ist heute unverifiable. `_record_session_date` rechnet den Dateistart nach America/New_York (`rules.py:1121-1125`), das Ledger nach Vienna (`ledger.py:506`, `context.py:121-141`). Solange `flat_by` null ist, ändert das keine Zahl. Phase 1 zieht R-CLOSE nicht auf den Vienna-Tag. Die uhrgemappten Regeln folgen 2.2. R-BIAS und R-SLTP bleiben, wie dort beschrieben.

### 2.5 Notion: eine Seite pro Tag

Empfehlung CD6: **eine** Seite, Titel unverändert `<D Mon YYYY> Session Debrief`.

Der Bug ist der viele Schreiber, nicht der Titel. Ein Titel pro Clip würde die Journal-Konvention verlassen und zehn Seiten erzeugen. `tva publish <irgendeine Session des Tages> --notion` schreibt die Tagesseite und ist idempotent (dieselbe `page_id`). Ein zweiter Clip ersetzt den Body nicht durch seinen eigenen Debrief. Auf dem Legacy-Pfad ist der Payload `payload_from_debrief` wie heute (`publish.py:140-150`), Feld für Feld gleich.

Der Tages-Body hält die bisherige Abschnittsreihenfolge. Fakten aus Manifest, Tagesregeln und den Clip-Transkripten. Ein invalidierter Clip erscheint als Lücke `alignment_invalid`, ohne Fill-auf-Videozeit-Sätze. Kein neuer Prosa-Auftrag an das Modell.

### 2.6 Trades/Stunde auf Handelszeit

Zwei Fehler, getrennt.

**Zähler.** Eindeutige Tages-Trades (Entry-Fill-Identität aus 2.3), nicht die Summe der Session-Zähler. `outside`-Trades stehen nicht in der `trades`-Tabelle: sie haben kein gesprochenes Setup, und `stated_lab_agree is None` würde dort als unverifiable in den Stated-vs-Lab-Tally laufen (`ledger.py:598-614`).

**Nenner.** Aufgezeichnete Zeit der Clips, die Fills besitzen dürfen: Summe ihrer `duration_s`. Lücken zwischen Stop/Start-Blöcken zählen nicht. Ein `suspected` Clip zählt **nicht** mit seiner zu kurzen Mediendauer mit, und es wird keine Wandspanne aus dem kaputten Alignment eingesetzt. Enthält die Periode nur solche Clips, ist `trades_per_hour` null und der Grund `paused_clip` steht im Rollup. Ein geratener Nenner ist schlimmer als null.

Der heutige Rollup macht beides falsch, wenn man nur eine Zeile `day:` dazulegt. `_summarize_ids` summiert `sessions.hours` und zählt Zeilen in `trades` (`ledger.py:667-700`), nicht `sessions.trade_count`. Clip-Stunden plus Tages-Stunden wären der doppelte Nenner. Clip-Trades plus eine Kopie auf der Tageszeile wären der doppelte Zähler. Die `sessions`-Zahl würde die Tageszeile als weitere Session zählen.

Deshalb, nur mit `TVA_TRADING_HOURS` und nur auf dem Tages-Pfad:

- Jede Clip-Zeile dieses Tages hat `hours = 0`. Die Zeile `day:YYYY-MM-DD` hat `hours` = Summe der berechtigten `duration_s` / 3600, und `trade_count` = eindeutige Tages-Trades inklusive `outside`.
- Der Rollup nimmt die Stunden weiter als `SUM(hours)`. Das ist dann nur die Tageszeile.
- Der Zähler ist `SUM(trade_count)` der `day:`-Zeilen plus `SUM(trade_count)` der Sessions, die keinem Tages-Pfad angehören. Clip-`trade_count` eines Tages-Pfads zählt nicht mit. Ohne `day:`-Zeile bleibt es bei der heutigen Zählung der Trade-Zeilen; auf L0 ist das dieselbe Zahl.
- `sessions` im Rollup zählt Ids ohne Präfix `day:`.
- `trades_per_hour_reason` ist ein optionales Feld an `LedgerPeriod` und `LedgerSummary`. Serialisiert fehlt der Schlüssel, wenn er null ist. L0 hat den Schlüssel nicht. Wert `paused_clip`, wenn der Nenner nur deshalb 0 ist. `_tph` gibt bei `hours <= 0` schon `None` zurück (`ledger.py:555-558`); der Grund ist das neue Feld, kein erfundener Nenner.

Auf dem Legacy-Pfad bleibt `hours = duration_s / 3600` exakt, auch wenn Drift ≠ 0. Die aligned Wandlänge `duration · (1 + drift/3600)` wird nicht benutzt. Es gibt keine `day:`-Zeile.

Das ist „Handelszeit“ als Zeit, in der OBS einen gültigen Block aufgenommen hat. Die Spanne vom ersten Fill bis zum letzten Fill (inklusive Pausen zwischen Blöcken) ist die Alternative in CD3 und wird nicht gebaut, solange CD3 bei der Empfehlung bleibt.

### 2.7 Altdaten

`tva day audit --from 2026-09-14 --to 2026-10-04` ist lesend.

- Schreibt höchstens `TVA_ROOT/days/audit-kw38-40.json`.
- Ändert kein `session.json`, kein Parquet, kein `ocr.parquet`, keine Notion-Seite.
- Proben, falls `--sample-clocks` gesetzt ist, stehen nur im Audit-JSON. Drei Frames lesen ist erlaubt. Zurückschreiben in die Session nicht.
- Ohne `--executions` ist `fills_match: not_run`.
- Mit CSV: die Klassen aus 1.8, auf dem **heutigen** Fenster, plus die +1 h / +2 h-Gegenrechnung als Zahl, nicht als Korrektur.
- Listet jede Session der drei Wochen mit `pause_check`. Erfindet keine Session, die nicht im Store liegt. `2026-09-30_090221` ist dabei, wenn es sie gibt.

Neu rechnen ist kein Schritt dieser Serie (CD7).

### 2.8 Was unverändert bleibt

Transkript, Insights, Frames, Clips, VLM, Context, Proposals, Coach-Prompt, `rules.yaml`-Schwellen, R-CLOSE, R-BIAS auf einem invalidierten Clip (Videozeit), sprachgestützte Regeln auf einem gültigen Clip, der 5-s-Stitch, Session-Ids, das ±30-min-Fenster auf dem Legacy-Pfad. Neue Felder nur: das Tagesobjekt inklusive `pause_check` und der Fit-Kopie, `alignment.method = "invalid"` nur wenn der Guard eine Pause gesetzt hat, das Gap `exit_outside_clip`, der Regelgrund `alignment_invalid`, der outside-Grund `no_core`, die Ledger-Id `day:YYYY-MM-DD` und `trades_per_hour_reason`, wenn es nicht null ist.

---

## 3. Parität, Determinismus, Schema

### 3.1 Parität

Fixture L0, synthetisch, in CI, ohne NAS: ein durchgehender Clip, keine Pause, Chapters optional, Executions alle im heutigen Fenster. Pipeline mit allen Flags aus und mit allen Flags an. Gleich sein müssen, byte- oder feldgleich, Laufzeitstempel ausgenommen:

`session.json`, `fills.parquet`, `trades.parquet`, `evidence.json`, `rules.json`, `debrief.md`, `debrief.json`, Ledger-Zeilen dieser Session, Notion-Payload (Titel, Summaries, Learnings).

`days/` existiert auf L0 nicht.

L0 deckt den Fall „ein Clip, aber Fills außerhalb des Pads“ bewusst nicht als Verhaltensänderung ab. Diese Fills bleiben draußen, wie heute. Sie zählen erst auf dem Tages-Pfad (CD2).

Echte Session: Accumu nennt eine unpausierte Ein-Datei-Session, die schon Fills, Regeln und Debrief hat (CD10). Der Test überspringt, wenn sie fehlt, gleiches Muster wie `docs/GOLDEN_EXCERPT.md`. CI hängt nicht daran.

### 3.2 Weitere Fixtures

Alle synthetisch, Flags an, Tages-Pfad:

| Id | Aufbau | Erwartung |
|---|---|---|
| M1 | viele kurze Clips, überlappende Pads, Kerne ohne Überlapp | jeder Fill in genau einem Parquet oder in `outside`; Ledger-Zähler einmal, nicht die Summe der Session-Zähler |
| M2 | Fill genau auf `nominal_end` | gehört zum nächsten Clip; ohne nächsten Clip `outside` |
| M3 | zwei Teile, Abstand ≤ 5 s, Präfix gleich | eine Session, wie `test_split_parts_become_one_session` |
| M4 | zwei Dateien, Abstand 6 s | zwei Sessions, nicht verschmolzen |
| M5 | eine Datei, Stufe 300 s, zwei Proben einig, die dritte daneben | `suspected`, `day.json` existiert, `method invalid`, keine Evidence-Fenster, Fills nicht diesem Clip zugeordnet, Tagesregeln zählen sie |
| M6 | eine Probe 60 s daneben, zwei einig | `unverifiable`, nicht `suspected`, Fit unverändert, kein `days/` |
| M7 | Tag mit Fills in der Lücke zwischen Clips | einmal `outside`, in R-MAX10 / R-DLL der Tagesdatei, in keiner Session-Evidence |
| M8 | zwei Dateien, dieselbe Startsekunde, Präfix verschieden | dieselbe Id, zweiter Ingest überschreibt; der Test wird rot, wenn die Id das Präfix aufnimmt; siehe CD8 |
| M9 | drei Clips, R-3L30 über Clipgrenzen | eine Verletzung im Tages-`rules.json`, Rollup-`violated` 1 |
| M10 | drei Proben auf einer Rampe, Spreizung 40 s, kein Paar innerhalb 5 s | `unverifiable`, nicht `suspected`, Fit unverändert |
| M11 | nur zwei erfolgreiche Proben, 300 s auseinander | `unverifiable`, nicht `suspected` |
| M12 | Fill nach Mitternacht im Kern eines Clips vom Vortag | nur im Tag des Clip-Starts, nicht noch einmal im Folgetag |

### 3.3 Determinismus

Gleiche Sessions, gleiche CSV, gleiches Manifest. Tests bauen den Tag in beiden Ingest-Reihenfolgen und vergleichen `day.json`. Kein Zufall, keine Uhr außer den Zeitstempeln, die der Paritätsvergleich ausnimmt (`publish` Laufzeitzeile, `created`-Flags, wo sie heute schon Laufzeit sind).

### 3.4 Keine stillen Fallbacks

| Lage | Sichtbar | Nicht |
|---|---|---|
| 2-gegen-1, Lücke über 120 s | `suspected`, Methode `invalid` | Strahl weiterverwenden, Wandende schätzen, „gültig bis zur Stufe“ |
| 2-gegen-1, Lücke 31–120 s, darunter der 60-s-Ausreißer | `unverifiable`, Fit bleibt | als Pause invalidieren oder die zwei Proben zur neuen Uhr machen |
| Rampe, kein Paar innerhalb 5 s | `unverifiable`, Fit bleibt | als Pause invalidieren |
| genau zwei Proben, uneinig | `unverifiable` | `suspected` raten |
| keine Proben | `unverifiable`, Legacy-Fit bleibt | Clip invalidieren |
| Fill in keinem Kern | `outside` mit Grund, bei leerem Tag `no_core` | dem nächsten Clip zuschlagen |
| überlappende Kerne | ein Besitzer, `overlap` im Manifest | beide Parquets |
| Nenner nur aus Pausen-Clips | `trades_per_hour: null`, Grund `paused_clip` | Dauer oder OCR-Spanne einsetzen |
| 20/88 ohne CSV | `fills_match: not_run` | eine Ursache nennen |

### 3.5 Schema und Migration

- Session-`schema_version` bleibt `"1"`. PR-29 nimmt `invalid` in `AlignmentMethod` auf (`schema.py:36`) und in die Methodenliste in `docs/04_ARCHITECTURE.md` und `docs/IMPLEMENTATION_PLAN.md`. Alte `session.json` bleiben gültig: sie enthalten den neuen Wert nicht. Ein Leser ohne dieses Literal wirft in `load_session`.
- `trades_per_hour_reason` ist optional und fehlt in der Serialisierung, wenn es null ist. L0 gewinnt dadurch keinen Schlüssel.
- Tagesdatei hat ihre eigene `"1"`. Kein Umbau von `ocr.parquet`, Audio, Video, kein neues Feld an `Evidence`.
- Ledger-Spalten auf dem Legacy-Pfad unverändert. Die Tages-Regelzeile ist eine zusätzliche Zeile mit neuer `session_id` `day:YYYY-MM-DD`, nicht eine neue Spalte in den bestehenden Session-Zeilen. Alte Ledger-Dateien bleiben lesbar. Rollback des Flags schreibt diese Zeile nicht mehr; die Zeile zu löschen ist Teil des Rollback-Schritts in PR-31, nicht ein stilles Liegenlassen.
- Bestehende Session-Ordner werden von keinem PR dieser Serie umgeschrieben, solange der jeweilige Flag aus ist. PR-29 ist die Ausnahme, sobald sein Flag an ist: er setzt `alignment.method`. Flag aus wieder auszuschalten lässt diese Dateien auf `invalid` stehen.

### 3.6 Flags und Rollback

Umgebungsvariablen, Default aus: unset oder leer ist aus, wahr ist `1`. Nicht der Stil von `TVA_EXTRACT_PROVIDER`, dessen Leerstelle der Fake-Provider ist. Dasselbe Muster wie `TVA_SERVE_MEDIA` (`serve.py:50`).

| Flag | PR | An |
|---|---|---|
| `TVA_DAY_MANIFEST` | 28 | `day.json` bei ≥ 2 Sessions, ohne Proben |
| `TVA_PAUSE_GUARD` | 29 | Proben; `day.json` auch bei einer `suspected` Session; `invalid` |
| `TVA_EXCLUSIVE_FILLS` | 30 | Besitz, ein Parquet; `tva fills` schreibt den Tag |
| `TVA_DAY_RULES` | 31 | die fünf Tagesregeln, nicht die uhrgemappten |
| `TVA_DAY_PUBLISH` | 32 | ein Schreiber |
| `TVA_TRADING_HOURS` | 33 | Zähler und Nenner, Clip-`hours` auf 0 |

Rollback einer Stufe heißt: Variable unset. Der gemergte Code bleibt und liest `invalid`. Ein Git-Rollback von PR-29 kann diese `session.json` nicht laden, bis `alignment.method` wieder ein alter Wert ist. Dafür muss `tva align --force` laufen, solange der neue Code noch da ist, oder die Datei wird von Hand auf den Fit aus dem Manifest gesetzt. „Neu alignen nach dem Rollback“ geht nicht: der alte Code wirft in `load_session`, bevor er schreibt.

PR-30: Flag aus hebt geschriebene Parquets nicht an. PR-31: die Zeile `day:YYYY-MM-DD` wird beim Rollback gelöscht, als Kommando im PR-Text, nicht nebenbei. PR-33: Flag aus, Rollup neu, Clip-`hours` wieder `duration_s / 3600`.

### 3.7 Grenzen

Lesen von Videos für die drei Proben und für den Audit ist erlaubt. Schreiben nicht: nicht auf die NAS-Videos, nicht außerhalb `TVA_ROOT` (`sessions/`, `days/`, `ledger/`), nicht nach `/musiclabel`. Der Audit schreibt keine Session-Artefakte.

---

## 4. PR-Serie

Jeder PR ist einzeln mergebar, Flag default aus, und enthält die Parität L0 mit **allen bis dahin existierenden** Flags an gegen Flags aus. Review durch einen eigenen Agenten, Checkliste am Ende. Nicht auf automatischen Bugbot warten. Kein PR ändert Verhalten, solange sein Flag aus ist.

Reihenfolge ist die Abhängigkeit. PR-34 sitzt vor PR-30 mit Absicht: die 20/88-Klassen auf dem heutigen Fenster, bevor das Fenster einen neuen Besitzer bekommt. Liefert der Audit einen Fehlbetrag, den 2.3 nicht trifft, bleibt PR-30 stehen, bis CD4 neu entschieden ist.

### PR-28 — Manifest

- Abhängigkeit: keine.
- Flag: `TVA_DAY_MANIFEST`.
- Tun: Vienna-Tag aus vorhandenen Sessions, `day.json` nur bei ≥ 2 Sessions. Kein Fill, keine Regel, kein Publish.
- Tests: zwei Tage bleiben getrennt; Reihenfolge des Ingest egal; M3 bleibt eine Session; M4 bleibt zwei; L0 schreibt kein `days/`.
- Akzeptanz: Session-Ordner von L0 bytegleich. Flag aus: kein `days/`.
- Rollback: Flag aus.

### PR-29 — Pausen-Guard

- Abhängigkeit: PR-28.
- Flag: `TVA_PAUSE_GUARD`.
- Tun: Literal `invalid`. Drei Proben wie 2.2, nicht ins `ocr.parquet`. `suspected` → `method invalid`, auch wenn der Tag nur diese eine Session hat: dann entsteht `day.json`. Evidence-Fenster weg, die fünf uhrgemappten Regeln `alignment_invalid`. R-BIAS bleibt. `unverifiable` und `clear` lassen den Fit in Ruhe. `tva align` ohne `--force` überschreibt `invalid` nicht mit Theil–Sen.
- Tests: M5, M6, M10, M11, L0 mit Guard an.
- Akzeptanz: ein durchgehender Clip ohne Stufe bleibt feldgleich. Der Tag hat eine Session und ist nicht `suspected`: kein Manifest, Proben werden gerechnet und verworfen, nichts geschrieben. Ein `suspected` Clip, auch als einzige Datei des Tages, hat `day.json`, `method invalid` und keine Evidence-Fenster.
- Rollback: Flag aus lässt schon gesetztes `invalid` stehen. Zurück zum Fit nur mit `tva align --force`, solange dieser Code lädt. Ein Git-Rollback ohne diese Umschreibung kann die Session nicht öffnen (3.6). Der Audit setzt nichts auf `invalid`.

### PR-34 — Audit KW38–KW40 und 20/88

- Abhängigkeit: PR-29 (dieselbe Probenfunktion, nur lesend).
- Flag: keiner. Der Befehl schreibt nur die Audit-Datei.
- Tun: Abschnitt 2.7. Klassen auf dem heutigen `session_utc_window`.
- Tests: synthetische Mischung aus innen, in zwei Fenstern, außerhalb; fehlende CSV → `not_run`; keine Session-Datei ändert mtime.
- Akzeptanz: Accumu kann die Datei lesen und CD4 entscheiden. Ohne Store auf der NAS ist der CI-Test synthetisch.
- Rollback: Datei löschen. Nichts sonst ist entstanden.
- **Gate:** PR-30 wird nicht gemerged, bevor das Audit-Protokoll vorliegt oder Accumu den Gate schriftlich aufhebt. Liegt es vor und der Fehlbetrag ist das Fenster oder die Doppelzählung, mergt PR-30 wie in 2.3. Liegt der Fehlbetrag woanders, gilt der Stopp aus CD4.

### PR-30 — Fill-Besitz

- Abhängigkeit: PR-28, PR-29, Gate PR-34.
- Flag: `TVA_EXCLUSIVE_FILLS`.
- Tun: Abschnitt 2.3. Legacy-Fenster unberührt.
- Tests: M1, M2, M5 (eine Datei, Clip beansprucht nichts, Fills sind `no_core` und in den Tagesregeln), M7, M12, L0 Parquets gleich.
- Akzeptanz: keine `fill`-Identität in mehr als einem Session-Parquet desselben Tages. `outside` vollständig.
- Rollback: Flag aus. Session-Parquets, die der Tages-Pfad schon geschrieben hat, werden von diesem PR nicht automatisch auf den alten Stand gehoben. Der PR-Text sagt: Rollback vor dem nächsten `tva fills`, oder `tva fills` einmal auf dem Legacy-Pfad neu. Kein stilles Mischparquet.

### PR-31 — Tagesregeln und Ledger einmal

- Abhängigkeit: PR-30.
- Flag: `TVA_DAY_RULES`.
- Tun: Abschnitt 2.4. Ledger-Id `day:YYYY-MM-DD`.
- Tests: M7 zählt in R-MAX10; M9; Rollup-Zähler 1; R-CLOSE und R-PLAYBOOK auf einem gültigen Clip unverändert; L0 `rules.json` gleich; R-DLL weiter unverifiable bei null-Limit.
- Akzeptanz: die fünf Tagesregeln im Session-JSON sind `unverifiable` mit Tagesgrund, sobald der Tages-Pfad aktiv ist. Die uhrgemappten Regeln sind das nicht, außer 2.2 setzt sie auf `alignment_invalid`. Die Tagesdatei hat genau eine Zeile pro Tagesregel. `tva rules` auf einer Session des Tages schreibt beides.
- Rollback: Flag aus, Ledger-Zeile `day:*` dieses Laufs löschen. Im PR als Kommando beschrieben, nicht als Nebenwirkung eines anderen Befehls.

### PR-32 — Eine Notion-Seite

- Abhängigkeit: PR-31 (der Body zitiert die Tagesregeln).
- Flag: `TVA_DAY_PUBLISH`.
- Tun: Abschnitt 2.5.
- Tests: zwei Sessions, zwei Publishes, eine `page_id`, Body der zweite Publish ist nicht der Clip-Debrief; L0 Payload gleich; Flag aus: bisheriges Überschreiben bleibt getestet, damit der Unterschied sichtbar ist.
- Akzeptanz: Titelbyte gleich dem heutigen `debrief_title`.
- Rollback: Flag aus. Die schon geschriebene Seite bleibt im Journal stehen, der nächste Legacy-Publish überschreibt sie wieder. Das ist das alte Verhalten und steht so im PR.

### PR-33 — Trades/Stunde

- Abhängigkeit: PR-30 für den Zähler, PR-29 für „suspected zählt nicht“.
- Flag: `TVA_TRADING_HOURS`.
- Tun: Abschnitt 2.6.
- Tests: L0 Rate gleich, Schlüssel `trades_per_hour_reason` fehlt; M1 Zähler ohne Doppelte und `sessions` zählt die Clips, nicht die `day:`-Zeile; M5 Nenner ohne die kurze Dauer, Rate null, Grund `paused_clip`, kein eingesetzter OCR-Span.
- Akzeptanz: Feld `trades_per_hour` auf L0 gleich. Auf M5 nicht die heutige überhöhte Rate. Clip-`hours` und Tages-`hours` sind nicht beide die Mediendauer.
- Rollback: Flag aus, Rollup neu erzeugen. Alte Formel.

### Review-Checkliste, jeder PR

Ein separater Agent, nicht der Autor. Er hakt nur ab, was er im Diff sieht.

1. Flag default aus, und mit Flag aus ist der bisherige Teststand grün, ohne angepasste Erwartungen.
2. L0 mit Flag an feldgleich zu Flag aus, Laufzeitstempel benannt und nur diese ausgenommen.
3. Kein Schreiben außerhalb `sessions/`, `days/`, `ledger/` unter `TVA_ROOT`. Kein Video-Write. Kein `/musiclabel`.
4. Kein neuer Fallback, der bei fehlender Probe, fehlender CSV oder überlappendem Kern einen Wert erfindet.
5. Sortierung fest, Test mit vertauschter Ingest-Reihenfolge, wo der PR Tageszustand schreibt.
6. Alte `session.json` lädt. Session-`schema_version` bleibt `"1"`, außer der PR begründet eine Anhebung und migriert lesend.
7. Keine Änderung an Prompts, Coach, Proposals-Logik, `rules.yaml`, R-CLOSE, R-BIAS, Sprachregeln auf gültigen Clips.
8. Phase 2 kommt im Diff nicht vor: kein Datenvertrag, kein Abschalten einer Analyse, die nicht in 2.2 als Unterdrückung bei `invalid` genannt ist.
9. Ein einzelner `suspected` Clip bekommt `day.json`. Drei Proben nach 2.2: M5 `suspected`, M6 und M10 und M11 nicht.

---

## 5. Risiken

1. **Parität nur auf L0.** Ein realer Ein-Clip-Tag mit Fills im 30-min-Pad bleibt auf dem Legacy-Pfad richtig. Ein realer Ein-Clip-Tag mit einer unentdeckten Pause (`unverifiable`) bleibt so falsch wie heute. Ohne `TVA_PAUSE_GUARD` ändert sich nichts. Das ist gewollt und leicht mit „wir haben die Pause gefixt“ zu verwechseln.
2. **Drei Proben und die blinde Zone.** Eine einzelne Pause ist immer 2-gegen-1. Unter 120 s, bei nur zwei Proben, oder als Rampe, bleibt sie `unverifiable`. Eine einzelne OCR-Uhr, die mehr als 120 s danebenliegt, invalidiert die ganze Datei. Die Stufe wird nicht auf die Sekunde gelegt. Stückweise Evidence auf pausierten Altbändern leistet Phase 1 nicht. KW40-Mittwoch bleibt für Fill-zu-Videozeit unbenutzbar, bis jemand CD5 auf Stückweise stellt und das eine eigene Serie wird.
3. **5 s Stitch.** Ein Stop/Start innerhalb von 5 s wird ein Clip. Der Plan ändert das nicht. Wer Blöcke trennen will, wartet länger als 5 s. Steht in CD8 nur am Rand; die eigentliche Empfehlung ist betrieblich, nicht ein Code-Change.
4. **Ledger-Zeile `day:`.** Ein vergessener Rollback lässt eine Tagesverletzung neben den alten Session-Zeilen stehen. PR-31 beschreibt das Löschen. Der Review prüft, dass der Rollup die Session-Zeilen der fünf Tagesregeln nicht als `violated` zählt, solange sie `unverifiable` mit Tagesgrund sind, und dass Stunden und Trade-Zähler nicht aus Clip-Zeile plus `day:`-Zeile addiert werden.
5. **`tva fills` nach einem halben Rollback.** PR-30 warnt davor. Ein Session-Parquet vom Tages-Pfad und Regeln vom Legacy-Pfad wären eine Mischung. Der Flag-Satz ist einer: 30, 31, 32, 33 zusammen an oder aus, sobald sie gemerged sind. 28 und 29 dürfen einzeln an sein. `tva fills` mit `TVA_EXCLUSIVE_FILLS` schreibt den ganzen Tag, nicht eine Legacy-Datei daneben.
6. **DST-Woche und ETH-Datum.** Der Tagesschlüssel ist Vienna. Ein Join außen auf `session_date` kann in der Übergangswoche von 23:00 bis 24:00 Vienna danebenliegen. KW38–KW40 2026 liegt vor dieser Woche (1.8). Der Audit berichtet beide Daten, damit man das sieht, ohne die Zuordnung umzustellen.
7. **Notion-Seite existiert schon und ist der schlechte Clip-Debrief.** Der erste Tages-Publish ersetzt sie. Das ist der Zweck. Wer den alten Text behalten will, muss ihn vorher kopieren. Der PR sagt das in einem Satz, ohne eine zweite Seite anzulegen.
8. **Proben lesen die NAS.** Nur lesen, nur drei Frames, nur wenn der Guard oder `--sample-clocks` an ist. Ein fehlgeschlagenes OCR der Uhr ist `unverifiable`, kein erfundener Offset.

---

## 6. Offene Entscheidungen

CD1–CD10. Nicht die D1–D9 aus `docs/05_ROADMAP.md`. Jede mit Empfehlung. Der Plan ist so geschrieben, dass die Empfehlung gilt, wenn Accumu nicht widerspricht. Ein Widerspruch ändert den betroffenen PR, nicht die schon gemergten Vorgänger, solange die Reihenfolge eingehalten wird.

### CD1. Tagesschlüssel

Vienna-Kalendertag des Dateistarts, nicht ETH 18:00 New York.

Empfehlung: Vienna. Das ist Session-Id, Notion-Titel und Ledger-Datum. Im Sommer, also auch in KW38–KW40 2026, ist das ETH-Datum derselbe Kalendertag. Die Abweichung ist nur 23:00–24:00 Vienna in den zwei DST-Übergangswochen und betrifft diese Aufnahmen nicht. Fills werden über den Zeitstempel zugeordnet, nicht über `session_date`.

### CD2. Fills außerhalb des Pads auf einem einzelnen Clip

Die Kurzfassung sagt, Fills außerhalb der Clips zählen in den Tagesregeln. Auf einem einzelnen durchgehenden Clip ist „außerhalb“ heute alles jenseits des 30-min-Pads, und die Session-Regeln zählen es nicht. Beides gleichzeitig geht nicht mit Parität.

Empfehlung: Legacy-Pfad lässt sie draußen. Der Tages-Pfad (mehrere Clips oder ein invalidierter Clip) zählt sie. L0 bleibt feldgleich.

### CD3. Was der Nenner von Trades/Stunde ist

Empfehlung: Summe der `duration_s` der Clips, die Fills besitzen dürfen. `suspected` trägt nicht bei. Keine geschätzte Wandspanne. Zähler eindeutig. Legacy-Pfad exakt `duration_s / 3600`.

Alternative, nicht gebaut: Spanne vom ersten Fill-Zeitstempel bis zum letzten, Lücken zwischen Blöcken inklusive. Die Rate wäre niedriger und auf L0 ungleich `duration_s`, sobald der Clip länger ist als der Fill-Span. Deshalb nicht der Default.

### CD4. Ursache von 20/88

Offen, bis PR-34 die Klassen geschrieben hat. Keine Zeitzonen-Korrektur, kein anderes Fenster und kein Dedupe-Fix in PR-30, der nur diese Zahl erklären soll. Das ist nicht das geparkte Roadmap-D4 zum Tagesverlust. PR-30 setzt den Besitz um, wie in 2.3 beschrieben, unabhängig davon. Gestoppt wird nur, wenn der Audit zeigt, dass der Fehlbetrag **nicht** das Fenster und nicht die Doppelzählung ist, sondern etwas, das 2.3 nicht trifft (zum Beispiel ein durchgängig falscher CSV-Offset). Dann wird CD4 neu vorgelegt, bevor PR-30 mergt. Ein ETH-gegen-Vienna-Join scheidet für KW38–KW40 aus (1.8).

### CD5. Pause: invalidieren oder stückweise

Empfehlung: invalidieren, ganze Datei, Zeitmapping aus.

Stückweise bräuchte Schnittstellen auf die Sekunde, ein anderes Alignment-Objekt als der eine Strahl, und alle Leser von `wall_to_video_t` (`evidence.py:127`, `rules.py:689`). Drei Proben liefern die Stufe, nicht den Schnitt. Ein Suchlauf über die Datei wäre eine zweite Uhr und läge außerhalb von „nur so viel ändern, wie der Bug erzwingt“. Transkripte bleiben benutzbar, weil sie in Videozeit sind. Fill-zu-Videozeit auf KW40-Mittwoch bleibt aus, bis eine spätere Serie das ausdrücklich will. Diese Serie ist nicht Phase 2 und nicht Phase 1.

### CD6. Notion

Empfehlung: eine Seite, bisheriger Titel, ein Schreiber, Body aus dem Tag. Nicht ein Titel pro Clip mit Uhrzeit.

### CD7. KW38–KW40 neu rechnen

Empfehlung: nur Audit. Markierung steht im Audit-JSON (`pause_check`), nicht in den Session-Ordnern. Neu transkribieren, neu alignen oder Notion überschreiben erst, wenn Accumu das nach dem Audit sagt. Das ist dann ein eigener Auftrag, kein stiller Schritt in PR-34.

### CD8. Session-Id und 5-s-Stitch

Empfehlung: Ids nicht ändern. Stitch-Regel nicht ändern. M8 dokumentiert die Kollision gleicher Startsekunde. Wer zwei Blöcke will, lässt mehr als 5 s zwischen Ende und nächstem Start. Ein Präfix in der Id wäre eine Migration aller bestehenden Ordner und bricht jeden Pfad, der `YYYY-MM-DD_HHMMSS` erwartet.

### CD9. R-CLOSE

Empfehlung: pro Session lassen. `flat_by` ist null, die Regel ist unverifiable. Sie in den Tageskatalog zu ziehen würde eine Regel anfassen, die der Auftrag nicht nennt, ohne heute eine andere Zahl zu erzeugen.

### CD10. Welche echte Session L0 auf der NAS ist

Offen, nur für den optionalen Test. CI läuft auf der synthetischen L0. Accumu nennt die Id, wenn er den NAS-Test will. Bis dahin überspringt der Test.

---

## 7. Nicht in diesem Plan

Phase 2, erst wenn Phase 1 gemerged ist und der Tages-Pfad auf einem echten Mehrclip-Tag gelaufen ist: ob TVA Analysen abschaltet, umbaut, oder dem Trading Coach einen Datenvertrag gibt (Transkript, Manifest, Uhrzeit nur wenn verifiziert, Vollständigkeitszahlen, keine Urteile). Fakten-Spezifikation und die zweite, unabhängige Rechnung aus dem Rohexport gehören dorthin. Sie sind hier nicht entworfen, damit eine Abweichung nach Phase 1 nur eine Ursache hat.
