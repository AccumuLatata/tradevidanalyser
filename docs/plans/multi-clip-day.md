# Phase 1 — Mehrere Clips pro Tag und Pausen

**Status:** Plan. Keine Code-, Konfigurations-, Test- oder Datenänderung in diesem PR.
**Stand geprüft:** `main` (Coach, PR-27).
**Danach:** Review durch Accumu und den Trading Coach. Umsetzung erst als die PR-Serie unten, jeder PR mit eigenem Review.
**Nicht in diesem Plan:** Phase 2 (Analysen abschalten, umbauen oder einen Datenvertrag für den Trading Coach). Alle bestehenden Analysen, Ausgaben und Schemas bleiben inhaltlich, was sie sind. Geändert wird nur, was nötig ist, damit sie bei mehreren Clips pro Tag und bei Pausen nicht mehr still falsch sind.

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

Ohne OCR-Uhr fällt `align` auf den Dateinamen zurück: Offset 0, Drift 0, Konfidenz 0,6, Methode `filename` (`align.py:15`, `align.py:178-185`, `align.py:235-236`). `--manual-offset` setzt nur den Offset, Drift bleibt 0 (`align.py:188-201`).

**Korrektur an der Kurzfassung.** Konfidenz unter 0,8 macht nicht alle zeitabhängigen Regeln unverifiable. `ALIGNMENT_LOW = 0.8` (`rules.py:63`) wird nur in `_alignment_too_low` (`rules.py:773-781`) gelesen, und das nur von:

- R-HOURLY (`rules.py:949-958`)
- R-ZONE (`rules.py:982-991`)
- R-TILT (`rules.py:1028-1038`)

R-DLL, R-MAX10, R-3L30, R-5M, R-REENTRY und R-CLOSE lesen die Alignment-Konfidenz nicht (`rules.py:396-621`). Evidence baut die Fenster auch bei niedriger Konfidenz und setzt nur `alignment: "low"` (`evidence.py:144-154`, `evidence.py:325-337`). Ein stilles falsches Mapping bleibt also stehen. Genau das soll Phase 1 für **Pausen** abstellen, nicht für den bisherigen Dateinamen-Fallback eines durchgehenden Clips (Parität, Abschnitt 4).

`parse_clock` legt die Uhr auf den Kalendertag von `Start + video_t` und klappt nur, wenn die Abweichung größer als 12 h ist (`ocr.py:142-174`). Die KW40-Lücke von etwa 5,5 h liegt darunter und würde als Residual sichtbar, **wenn** Samples auf beiden Seiten existieren. Eine Lücke über 12 h kann vom Klapper verdeckt werden. Das ist eine Grenze des Detektors, nicht der KW40-Fall.

### 1.2 OCR sieht eine Pause heute meist nicht

`tva frames` ohne `--at` nimmt nur Chapter-Marker mit Offsets −5, −2, 0, +2, +5 s (`frames.py:27`, `frames.py:127-135`). `tva ocr` liest diese Frames. Ein Clip ohne Chapters hat keine Uhr-Samples, also den Dateinamen-Fallback. Ein Clip mit Chapters nur am Anfang hat Samples auf einer Seite der Pause. Audio sieht die Pause ebenfalls nicht: die ausgelassene Wandzeit liegt nicht in der Datei, der Ton ist entlang der Videozeit lückenlos. Stille im File ist Kommentar-Pause, nicht OBS-Pause. Audio ist deshalb kein Pausensignal.

### 1.3 Fills-Fenster, doppelt und zu kurz

`tva fills` behält Fills im geschlossenen Intervall `[Start − 30 min, Start + Dauer + 30 min]` (`fills.py:35`, `fills.py:122-141`). Start und Ende kommen aus Dateiname und `duration_s`, nicht aus dem Alignment. Jede Session schreibt ihr eigenes `fills.parquet` (`fills.py:649-704`). Es gibt keine Zuordnung über Sessions. Der Ledger-Schlüssel ist `(session_id, tva_trade_id)` (`ledger.py:205`), dieselbe Execution in zwei Sessions ist zwei Trades.

Beispiel aus dem Auftrag, nachgerechnet: `2026-09-30 09-02-21.mp4`, Dauer 51:48, Start 09:02:21 Vienna. Nominales Ende 09:54:09, plus 30 min Pad → **10:24:09**. Die Aussage „ab etwa 10:24 unbrauchbar“ ist dieses Fenster, nicht `Start + Dauer`. Fills danach fehlen in diesem `fills.parquet`. Fills davor können drin sein und werden mit dem linearen Strahl auf die Videozeit gelegt. Beides ist falsch, sobald in der Datei eine Pause steckt. Ob der Strahl vor der Pause zufällig noch stimmt, hängt von den OCR-Samples ab (1.1) und wird nicht geprüft.

Kurze Clips: zwei Blöcke, deren Pads sich überlappen (Abstand der Kerne unter 60 min), teilen sich dieselben Fills. Evidence hängt dann an einem Trade, der in dem Clip nicht zu sehen ist.

### 1.4 Tagesregeln laufen pro Session

`build_rules_report` liest nur `sessions/<id>/trades.parquet` (`rules.py:1182-1208`) und bewertet den Katalog darauf (`rules.py:1079-1085`). R-MAX10 zählt Paare (Round-Turns, offene inklusive), nicht Executions (`rules.py:440-445`). R-DLL summiert `net_pnl_currency`, ersatzweise brutto mit `fees_unknown` (`rules.py:321-329`). Schwellen: `rules.yaml:7-17` (`daily_loss_limit_usd` ist null, R-DLL damit heute unverifiable; `max_trades_per_day: 10`).

Zehn Clips mit je drei Trades liefern zehnmal „3 ≤ 10, pass“. Dieselben zehn Sessions im Ledger zählen eine Tagesverletzung zehnmal mit, weil `rule_checks` pro `(session_id, rule)` liegt (`ledger.py:211-216`) und der Rollup Zeilen zählt.

### 1.5 Notion überschreibt die Tagesseite

Titel ist `<D Mon YYYY> Session Debrief` aus dem Vienna-Kalendertag des Dateistarts (`publish.py:108-109`, `context.py:104-106`, `context.py:121-136`). `publish_session` sucht diese Seite und aktualisiert sie (`publish.py:788-794`). Der zweite Clip desselben Tages ersetzt Summaries und Learnings. Ein Titel pro Clip existiert nicht.

### 1.6 Trades/Stunde

`hours = duration_s / 3600` pro Session (`ledger.py:507`, `ledger.py:530-533`), Rollup summiert `hours` (`ledger.py:668-701`), Rate ist `trades / hours` (`ledger.py:555-558`). Das ist die Summe der Dateilängen. Für einen pausierten Clip ist der Nenner zu klein. Für mehrere gültige Stop/Start-Clips ist der Nenner die aufgezeichnete Zeit ohne die Lücken dazwischen. Der Zähler ist zu groß, sobald Fills doppelt im Ledger stehen. Beides verzerrt die Rate. Eine geschätzte Wandspanne aus einem kaputten Alignment wäre eine dritte Verzerrung. Abschnitt 2.6 legt fest, was stattdessen gilt.

### 1.7 Zusammenlegen der OBS-Teile

`discover_split_parts` (`ingest.py:97-159`): gleiche Dateiendung, gleiches Namenspräfix (`naming.py:49-55`), aufeinanderfolgende Starts, `|Startₙ₊₁ − Startₙ − Dauerₙ| ≤ 5 s` (`ingest.py:14`, `ingest.py:149-154`). Das ist der Auto-Split und muss bleiben.

Zwei Korrekturen:

- Ein normaler OBS-Name `YYYY-MM-DD HH-MM-SS` hat das Präfix `""`. Alle solchen Dateien in **einem** Ordner gelten als gleiches Präfix. Getrennt wird nur durch die 5 s. Ein Stop/Start innerhalb von 5 s nach dem Ende der vorigen Datei wird zusammengelegt. Ein echter Block mit mehr als 5 s Abstand wird nicht zusammengelegt (`tests/test_ingest_and_pipeline.py:47-86` und `:145-155`).
- Die Session-Id ist `YYYY-MM-DD_HHMMSS` ohne Präfix (`naming.py:45`). Zwei Dateien mit derselben Startsekunde und verschiedenem Text davor landen auf derselben Id. Das ist ein bestehendes Kollisionsverhalten, kein Pausen-Bug. Phase 1 ändert die Id nicht (Entscheidung D8).

### 1.8 KW40: 20 von 88 Executions

Nicht nachgerechnet. Export und `fills.parquet` sind nicht im Repo, und dieser Plan liest die NAS nicht. Aus dem Code sind das die möglichen Ursachen, in dieser Reihenfolge. Keine davon wird hier als die Ursache festgelegt.

1. **Fenster.** Es wird nur `[Start − 30 min, Ende + 30 min]` geschrieben. Ein Tagesexport gegen ein Session-Parquet, oder gegen die Vereinigung kurzer Fenster, verliert alles außerhalb. Für die pausierte Mittwoch-Datei endet das Fenster um 10:24:09. Das allein kann einen großen Fehlbetrag erklären.
2. **Vergleich gegen eine Datei statt gegen den Tag.** Jede Session hat ihr eigenes Parquet.
3. **Zeitzone, aber nicht als stiller TVA-Fallback.** Der Loader verlangt einen expliziten Offset und speichert UTC (`fills_mirror.py:334-363`). Naive Zeiten werfen, sie werden nicht als Vienna geraten. Das Fenster rechnet den Dateistart von Vienna nach UTC. Ein erfolgreicher Lauf hat also Offsets gesehen. Ein falscher Offset **in der CSV** (etwa `Z` statt `+02:00`) schiebt Fills um 1–2 h aus dem Fenster. Das wäre eine Eigenschaft der Datei, kein zweiter Uhr-Pfad im Code.
4. **`session_date` ist nicht der Vienna-Tag.** `trading_session_date` ist ETH 18:00 America/New_York (`fills_mirror.py:156-162`). Der Fensterfilter benutzt den Zeitstempel, nicht dieses Datum. Ein Join über `session_date` gegen den Vienna-Tag geht in den DST-Übergangswochen auseinander: EU stellt am letzten Sonntag im Oktober um, die USA am ersten Sonntag im November. Dazwischen rollt die ETH-Session um 23:00 Vienna, nicht um Mitternacht. Für die NY-Morgenclips (etwa 09:00–16:00 Vienna) fällt beides auf denselben Kalendertag. Ob der 20/88-Vergleich so gejoint hat, ist offen.
5. **Kein stilles Dedupe.** `fill_id` ist `tv:{Zeilenindex}:{spread}:{UTC-Sekunde}:{side}:{price}:{qty}` (`fills_mirror.py:312-324`). Dieselbe Execution bekommt bei anderer Zeilenreihenfolge eine andere Id. Der Filter wirft keine Dubletten weg. Ein Abgleich, der nur auf die Id geht, unterschätzt Treffer, wenn der Export neu sortiert wurde.
6. **Parse-Fehler erklären keine Teilmenge.** Eine unlesbare Zeile wirft und bricht den Lauf ab (`fills_mirror.py:258-271`, nacktes `MNQ` ohne Kontraktmonat inklusive). Ein geschriebenes Parquet ist vollständig für die geparsten Zeilen. Manuelle Nicht-Futures bleiben im `fills.parquet` (geschrieben vor dem Pairing) und fehlen in `trades.parquet`, solange `--include-manual` nicht gesetzt ist.

Der Audit-Befehl in PR-34 zählt jede Execution in genau eine Klasse (`in_one`, `in_many`, `outside_all`) und berichtet zusätzlich, wie die Zähler bei einer Verschiebung von +1 h und +2 h aussähen. Er wendet keine Verschiebung an. Erst dieses Protokoll entscheidet D4. Bis dahin baut PR-30 das Fenster nicht „auf Verdacht Zeitzone“ um.

### 1.9 Altdaten KW38–KW40

| ISO-Woche | Vienna-Tage |
|---|---|
| KW38 | 2026-09-14 … 2026-09-20 |
| KW39 | 2026-09-21 … 2026-09-27 |
| KW40 | 2026-09-28 … 2026-10-04 |

KW40-Mittwoch ist 2026-09-30. Welche anderen Sessions pausiert sind, steht nicht im Repo. Der Audit markiert sie. Neu rechnen tut er nicht (D7).

---

## 2. Teil 1 — Verhalten, das die Serie herstellt

Zwei Pfade, hart getrennt.

**Legacy-Pfad.** Der Vienna-Tag hat genau eine Session, und ihr `pause_check` ist nicht `suspected`. Dann laufen Ingest, Alignment, Fills, Evidence, Regeln, Report, Publish, Ledger und Rollup durch den heutigen Code. Kein neues Feld in `session.json`, kein `days/`-Eintrag, kein anderer Nenner. Parität ist dieser Pfad, nicht ein Abgleich, der zufällig gleich aussieht.

**Tages-Pfad.** Zwei oder mehr Sessions an dem Vienna-Tag, oder mindestens eine Session mit `pause_check: suspected`. Dann gelten die Regeln unten. Flags stehen bis zum letzten PR auf aus. Aus heißt: auch der Tages-Pfad ist tot, alles bleibt Legacy.

Der Tages-Pfad ist eine reine Funktion aus den Session-Ordnern, der Executions-CSV und den Pausen-Proben. Sortierung `(start_wallclock_vienna, session_id)`. Kein Watch-Zeitpunkt, keine Verarbeitungsreihenfolge. `tva day build <YYYY-MM-DD>` ersetzt `days/<date>/` atomar (temp, dann rename). Watch ruft das in dieser Serie nicht auf. Publish bleibt opt-in über `--notion`.

### 2.1 Tagesobjekt

Neu, nur auf dem Tages-Pfad:

```text
TVA_ROOT/days/YYYY-MM-DD/day.json
```

`schema_version: "1"`. Session-Schema bleibt `"1"`. Der Tagesschlüssel ist der Vienna-Kalendertag von `start_wallclock_vienna`, derselbe Tag wie Session-Id, Notion-Titel und Ledger-`session_date` (D1). Nicht die ETH-`session_date`.

Pro Clip im Manifest, und nur diese Felder:

| Feld | Bedeutung |
|---|---|
| `session_id`, `filename` | wie heute |
| `nominal_start`, `nominal_end` | Dateistart und Dateistart + `duration_s`, Vienna |
| `duration_s` | Mediendauer, nicht Wandspanne |
| `alignment_method`, `alignment_confidence` | Kopie des Session-Stands vor einem Invalidieren |
| `pause_check` | `clear`, `suspected` oder `unverifiable` |
| `pause_evidence` | die Proben, die den Status tragen; leer wenn unverifiable |

Ein zusammengelegter Auto-Split ist **eine** Session und damit ein Clip. Die Teile stehen weiter in `recording.parts`.

### 2.2 Pausen

Empfehlung: **nicht** stückweise ausrichten. Der Clip wird `alignment.method = "invalid"`, Konfidenz 0, Offset 0, Drift 0. Zeitabhängige Ausgaben werden unterdrückt. Begründung in D5.

Detektor, drei Proben, nicht die Chapter-OCR:

- Videozeiten `0`, `duration/2`, `max(duration − 0,5 s, 0)`, gelesen von der Datei, **nicht** nach `ocr.parquet` oder `frames/` geschrieben. Ergebnisse nur ins Manifest. Sonst ändert die Probe den Theil–Sen-Fit und bricht die Parität, sobald jemand den Legacy-Clip später neu aligned.
- `y = (geparste Uhr − Dateistart) − video_t`, in Sekunden.
- Konstanten: `PAUSE_JUMP_S = 30`, `AGREE_S = 5`. 30 s liegt über normaler Drift und OCR-Streuung und unter einer echten OBS-Pause. 5 s ist „dieselbe Stufe“.
- `max(y) − min(y) ≤ 30` → `clear`.
- Zwei Proben innerhalb von 5 s, die dritte weiter als 30 s von ihrem Median weg → **nicht** suspected. Das ist der einzelne Ausreißer aus `test_align.py:124-140`. Status `unverifiable` (eine Probe kann eine schlechte OCR sein). Der lineare Fit bleibt. Kein Raten, welche Probe stimmt.
- Sonst, also eine Stufe, die nicht an einer einzelnen Probe hängt → `suspected`.

Weniger als zwei erfolgreiche Proben → `unverifiable`. Der Legacy-Fit bleibt. Das Manifest sagt das ausdrücklich. Ein Clip ohne OCR wird nicht pauschal invalidiert, sonst wäre jeder heutige Dateinamen-Fallback eine Verhaltensänderung.

`suspected` gilt für die **ganze** Datei. Wir schneiden nicht „gültig bis zur Stufe“, weil drei Proben die Schnittstelle nicht auf die Sekunde legen und ein teilgültiger Strahl wieder ein stilles Mapping wäre.

Unterdrückt, sobald `method == "invalid"`:

- `tva evidence`: kein Fenster, Grund `alignment_invalid`. Kein `wall_to_video`.
- R-HOURLY, R-ZONE, R-TILT, R-3C-CT, R-ARRIVAL: `unverifiable`, Grund `alignment_invalid`. Die ersten drei prüfen heute nur die Konfidenz; die letzten zwei rechnen den Fill auf die Videozeit (`rules.py:880-941`) und würden mit Offset 0 falsch verletzt oder bestanden.
- R-PLAYBOOK, R-DEFINED, R-BIAS, R-SLTP: unverändert in ihrer Definition. Ohne Evidence werden die sprachgestützten davon `unverifiable` über den bestehenden Absent-Speech-Pfad, nicht über eine neue Heuristik.
- Transkript, Insights, Proposals aus dem Transkript: bleiben. Die Videozeit darin ist die Dateizeit und stimmt auch bei einer Pause.

Nicht unterdrückt: die Fills selbst als Fakten mit ihrer eigenen Uhr. Ein invalidierter Clip **beansprucht** nur keine Fills (2.3). Die Tagesregeln zählen sie weiter (2.4).

### 2.3 Jeder Fill genau einmal

Universum auf dem Tages-Pfad: alle Executions der CSV, deren Zeitstempel in Vienna auf dieses Kalenderdatum fällt, plus Executions, die im **Kern** eines Clips dieses Tages liegen, falls die Uhr knapp über Mitternacht ragt. Kein ±30-min-Pad als Besitz.

Kern eines gültigen Clips (`pause_check` ist `clear`, oder `unverifiable` und Methode nicht `invalid`):

```text
[nominal_start, nominal_end)
```

halboffen, aus Dateiname und Dauer, nicht aus Offset und Drift. Der Dateistart ist die OBS-Startzeit und für einen Clip ohne Pause die richtige Wandzeit. Offset und Drift bleiben, was sie heute sind, für Evidence **innerhalb** eines Clips, den der Fill schon besitzt. Sie entscheiden nicht, wem der Fill gehört. Sonst würde ein schlechter Fit den Besitz verschieben.

Ein Clip mit `suspected` hat einen bekannten Start und ein unbekanntes Wandende. Sein Kern ist leer. Er bekommt keine Fills.

Besitz, in dieser Reihenfolge, stabil sortiert:

1. Liegt der Zeitstempel in genau einem Kern → dieser Clip.
2. Liegt er in mehreren Kernen (überlappende Aufnahmen) → die Session mit dem kleineren `(start, session_id)`. Das Manifest führt `overlap` mit den anderen Ids. Kein zweites Parquet.
3. Liegt er in keinem Kern → `outside`, mit Grund `before_first`, `gap` oder `after_last`. Genau ein Eintrag.

Grenze: Zeitstempel gleich `nominal_end` von A und gleich `nominal_start` von B gehört zu B (halboffen). Zeitstempel gleich `nominal_end` des letzten Clips, ohne nächsten Kern, ist `outside`. Heute ist das Fenster beidseitig geschlossen; auf dem Legacy-Pfad (ein Clip, keine Pause) bleibt das so, inklusive Pad. Der Grenzfall-Test für halboffen liegt auf dem Tages-Pfad.

Identität für „derselbe Fill“: `(timestamp UTC, side, price, qty, instrument, contract_month, contract_year, source_group_id)`. Nicht `fill_id`, weil die den CSV-Zeilenindex enthält (1.8). Zwei echte Fills mit demselben Schlüssel in derselben Sekunde bleiben zwei Einträge und werden über den Zeilenindex nur als Tie-Break sortiert, nicht verschmolzen.

Pairing für die Tagesregeln läuft **einmal** über die Vereinigung aus zugewiesenen und `outside`-Fills. Ein Round-Turn, der in Clip A aufgeht und in Clip B zugeht, ist ein Trade. Das Session-`trades.parquet` auf dem Tages-Pfad enthält die Teilmenge dieser Tages-Trades, deren Entry-Fill der Session gehört, mit der **Tages**-`tva_trade_id`. Kein zweites Pairing auf der Teilmenge: das machte den Trade in A offen und in B herrenlos. Liegt der Exit außerhalb des Clips, endet das Evidence-Fenster am Clipende und trägt die Lücke `exit_outside_clip`. Das ist ein neues Gap-String, kein neues Urteil.

`tva_trade_id` bleibt `T01…` in der Reihenfolge `(entry_timestamp, trade_id)` (`fills.py:145-150`). Auf dem Legacy-Pfad ist die Menge dieselbe wie heute, die Ids auch.

### 2.4 Regeln auf Tagesebene

Nur diese fünf, und nur auf dem Tages-Pfad, über das eine Tages-Pairing, `outside` eingeschlossen:

- R-DLL, R-MAX10, R-3L30, R-5M, R-REENTRY

Dieselbe Funktion wie heute (`rules.py:396-586`), andere Eingabemenge. Schwellen bleiben `rules.yaml`. R-DLL bleibt unverifiable, solange `daily_loss_limit_usd` null ist. R-MAX10 zählt weiter Round-Turns, nicht die 88 Executions.

Geschrieben nach `days/<date>/rules.json`, einmal.

In jedem Session-`rules.json` dieses Tages werden dieselben fünf auf `unverifiable` gesetzt, Grund `evaluated on day YYYY-MM-DD`. Sonst zählt der Ledger die Verletzung pro Clip. Der Rollup addiert die Tageszeile einmal. Akzeptanz: drei Clips, eine Tagesverletzung von R-MAX10 → Rollup `violated = 1`, nicht 0 und nicht 3.

R-CLOSE bleibt pro Session (D9). `flat_by` ist null, die Regel ist heute unverifiable. Die sprachgestützten Regeln bleiben pro Clip, mit der Invalidierung aus 2.2.

### 2.5 Notion: eine Seite pro Tag

Empfehlung D6: **eine** Seite, Titel unverändert `<D Mon YYYY> Session Debrief`.

Der Bug ist der viele Schreiber, nicht der Titel. Ein Titel pro Clip würde die Journal-Konvention verlassen und zehn Seiten erzeugen. `tva publish <irgendeine Session des Tages> --notion` schreibt die Tagesseite und ist idempotent (dieselbe `page_id`). Ein zweiter Clip ersetzt den Body nicht durch seinen eigenen Debrief. Auf dem Legacy-Pfad ist der Payload `payload_from_debrief` wie heute (`publish.py:140-150`), Feld für Feld gleich.

Der Tages-Body hält die bisherige Abschnittsreihenfolge. Fakten aus Manifest, Tagesregeln und den Clip-Transkripten. Ein invalidierter Clip erscheint als Lücke `alignment_invalid`, ohne Fill-auf-Videozeit-Sätze. Kein neuer Prosa-Auftrag an das Modell.

### 2.6 Trades/Stunde auf Handelszeit

Zwei Fehler, getrennt.

**Zähler.** Eindeutige Tages-Trades (Entry-Fill-Identität aus 2.3), nicht die Summe der Session-Zähler.

**Nenner.** Aufgezeichnete Zeit der Clips, die Fills besitzen dürfen: Summe ihrer `duration_s`. Lücken zwischen Stop/Start-Blöcken zählen nicht. Ein `suspected` Clip zählt **nicht** mit seiner zu kurzen Mediendauer mit, und es wird keine Wandspanne aus dem kaputten Alignment eingesetzt. Enthält die Periode nur solche Clips, ist `trades_per_hour` null und der Grund `paused_clip` steht im Rollup. Ein geratener Nenner ist schlimmer als null.

Auf dem Legacy-Pfad bleibt `hours = duration_s / 3600` exakt, auch wenn Drift ≠ 0. Die aligned Wandlänge `duration · (1 + drift/3600)` wird nicht benutzt.

Das ist „Handelszeit“ als Zeit, in der OBS einen gültigen Block aufgenommen hat. Die Spanne vom ersten Fill bis zum letzten Fill (inklusive Pausen zwischen Blöcken) ist die Alternative in D3 und wird nicht gebaut, solange D3 bei der Empfehlung bleibt.

### 2.7 Altdaten

`tva day audit --from 2026-09-14 --to 2026-10-04` ist lesend.

- Schreibt höchstens `TVA_ROOT/days/audit-kw38-40.json`.
- Ändert kein `session.json`, kein Parquet, kein `ocr.parquet`, keine Notion-Seite.
- Proben, falls `--sample-clocks` gesetzt ist, stehen nur im Audit-JSON. Drei Frames lesen ist erlaubt. Zurückschreiben in die Session nicht.
- Ohne `--executions` ist `fills_match: not_run`.
- Mit CSV: die Klassen aus 1.8, auf dem **heutigen** Fenster, plus die +1 h / +2 h-Gegenrechnung als Zahl, nicht als Korrektur.
- Listet jede Session der drei Wochen mit `pause_check`. Erfindet keine Session, die nicht im Store liegt. `2026-09-30_090221` ist dabei, wenn es sie gibt.

Neu rechnen ist kein Schritt dieser Serie (D7).

### 2.8 Was unverändert bleibt

Transkript, Insights, Frames, Clips, VLM, Context, Proposals, Coach-Prompt, `rules.yaml`-Schwellen, R-CLOSE, sprachgestützte Regeln auf einem gültigen Clip, der 5-s-Stitch, Session-Ids, das ±30-min-Fenster auf dem Legacy-Pfad. Neue Felder nur: das Tagesobjekt, `pause_check` dort, `alignment.method = "invalid"` nur wenn der Guard eine Pause gesetzt hat, das Gap `exit_outside_clip` und `alignment_invalid`.

---

## 3. Parität, Determinismus, Schema

### 3.1 Parität

Fixture L0, synthetisch, in CI, ohne NAS: ein durchgehender Clip, keine Pause, Chapters optional, Executions alle im heutigen Fenster. Pipeline mit allen Flags aus und mit allen Flags an. Gleich sein müssen, byte- oder feldgleich, Laufzeitstempel ausgenommen:

`session.json`, `fills.parquet`, `trades.parquet`, `evidence.json`, `rules.json`, `debrief.md`, `debrief.json`, Ledger-Zeilen dieser Session, Notion-Payload (Titel, Summaries, Learnings).

`days/` existiert auf L0 nicht.

L0 deckt den Fall „ein Clip, aber Fills außerhalb des Pads“ bewusst nicht als Verhaltensänderung ab. Diese Fills bleiben draußen, wie heute. Sie zählen erst auf dem Tages-Pfad (D2).

Echte Session: Accumu nennt eine unpausierte Ein-Datei-Session, die schon Fills, Regeln und Debrief hat (D10). Der Test überspringt, wenn sie fehlt, gleiches Muster wie `docs/GOLDEN_EXCERPT.md`. CI hängt nicht daran.

### 3.2 Weitere Fixtures

Alle synthetisch, Flags an, Tages-Pfad:

| Id | Aufbau | Erwartung |
|---|---|---|
| M1 | viele kurze Clips, überlappende Pads | jeder Fill in genau einem Parquet oder in `outside`; Ledger-Trade einmal |
| M2 | Fill genau auf `nominal_end` | gehört zum nächsten Clip; ohne nächsten Clip `outside` |
| M3 | zwei Teile, Abstand ≤ 5 s, Präfix gleich | eine Session, wie `test_split_parts_become_one_session` |
| M4 | zwei Dateien, Abstand 6 s | zwei Sessions, nicht verschmolzen |
| M5 | pausierter Clip, Stufe 300 s in den drei Proben | `suspected`, `method invalid`, keine Evidence-Fenster, Fills nicht diesem Clip zugeordnet, Tagesregeln zählen sie |
| M6 | eine Probe 60 s daneben, zwei einig | nicht `suspected` |
| M7 | Tag mit Fills in der Lücke zwischen Clips | einmal `outside`, in R-MAX10 / R-DLL der Tagesdatei, in keiner Session-Evidence |
| M8 | zwei Dateien, dieselbe Startsekunde, Präfix verschieden | dokumentiertes Kollisionsverhalten der Id, Test fällt nicht um, weil wir die Id ändern; siehe D8 |
| M9 | drei Clips, R-3L30 über Clipgrenzen | eine Verletzung im Tages-`rules.json`, Rollup-Zähler 1 |

### 3.3 Determinismus

Gleiche Sessions, gleiche CSV, gleiches Manifest. Tests bauen den Tag in beiden Ingest-Reihenfolgen und vergleichen `day.json`. Kein Zufall, keine Uhr außer den Zeitstempeln, die der Paritätsvergleich ausnimmt (`publish` Laufzeitzeile, `created`-Flags, wo sie heute schon Laufzeit sind).

### 3.4 Keine stillen Fallbacks

| Lage | Sichtbar | Nicht |
|---|---|---|
| Pause, Stufe in den Proben | `suspected`, Methode `invalid` | Strahl weiterverwenden, Wandende schätzen |
| eine abweichende Probe | `unverifiable` | die Mehrheit zur neuen Uhr machen |
| keine Proben | `unverifiable`, Legacy-Fit bleibt | Clip invalidieren |
| Fill in keinem Kern | `outside` mit Grund | dem nächsten Clip zuschlagen |
| überlappende Kerne | ein Besitzer, `overlap` im Manifest | beide Parquets |
| Nenner nur aus Pausen-Clips | `trades_per_hour: null`, Grund `paused_clip` | Dauer oder OCR-Spanne einsetzen |
| 20/88 ohne CSV | `fills_match: not_run` | eine Ursache nennen |

### 3.5 Schema und Migration

- Session-`schema_version` bleibt `"1"`. Alte `session.json` laden, weil `invalid` nur geschrieben wird, wenn der Guard läuft, und neue Leser die alte Methode akzeptieren.
- Tagesdatei hat ihre eigene `"1"`. Kein Umbau von `ocr.parquet`, Audio, Video.
- Ledger-Spalten auf dem Legacy-Pfad unverändert. Die Tages-Regelzeile ist eine zusätzliche Zeile mit neuer `session_id` `day:YYYY-MM-DD`, nicht eine neue Spalte in den bestehenden Session-Zeilen. Alte Ledger-Dateien bleiben lesbar. Rollback des Flags schreibt diese Zeile nicht mehr; die Zeile zu löschen ist Teil des Rollback-Schritts in PR-31, nicht ein stilles Liegenlassen.
- Bestehende Session-Ordner werden von keinem PR dieser Serie umgeschrieben, solange der jeweilige Flag aus ist.

### 3.6 Flags und Rollback

Umgebungsvariablen, Default aus, gleicher Stil wie `TVA_EXTRACT_PROVIDER`.

| Flag | PR | An |
|---|---|---|
| `TVA_DAY_MANIFEST` | 28 | Tagesordner bei ≥ 2 Sessions |
| `TVA_PAUSE_GUARD` | 29 | Proben, `invalid` |
| `TVA_EXCLUSIVE_FILLS` | 30 | Besitz, ein Parquet |
| `TVA_DAY_RULES` | 31 | fünf Regeln auf dem Tag |
| `TVA_DAY_PUBLISH` | 32 | ein Schreiber |
| `TVA_TRADING_HOURS` | 33 | Zähler und Nenner |

Rollback einer Stufe: Variable unset, der vorige Pfad ist der Code davor. Kein Daten-Rewrite nötig, weil der Legacy-Pfad Session-Dateien nicht anfasst. Ausnahme PR-31: die Zeile `day:YYYY-MM-DD` im Ledger wird beim Rollback entfernt, wenn sie geschrieben wurde. Das steht im PR-Text.

### 3.7 Grenzen

Lesen von Videos für die drei Proben und für den Audit ist erlaubt. Schreiben nicht: nicht auf die NAS-Videos, nicht außerhalb `TVA_ROOT` (`sessions/`, `days/`, `ledger/`), nicht nach `/musiclabel`. Der Audit schreibt keine Session-Artefakte.

---

## 4. PR-Serie

Jeder PR ist einzeln mergebar, Flag default aus, und enthält die Parität L0 mit **allen bis dahin existierenden** Flags an gegen Flags aus. Review durch einen eigenen Agenten, Checkliste am Ende. Nicht auf automatischen Bugbot warten. Kein PR ändert Verhalten, solange sein Flag aus ist.

Reihenfolge ist die Abhängigkeit. PR-34 sitzt vor PR-30 mit Absicht: die 20/88-Klassen auf dem heutigen Fenster, bevor das Fenster einen neuen Besitzer bekommt. Liefert PR-34 eine Verschiebung, die den Abgleich stark verändert, bleibt PR-30 stehen, bis D4 entschieden ist.

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
- Tun: drei Proben ins Manifest, nicht ins `ocr.parquet`. `suspected` → `method invalid` wie 2.2, Evidence und die fünf genannten Regeln unterdrückt. `unverifiable` und `clear` lassen den Fit in Ruhe.
- Tests: M5, M6, L0 mit Guard an.
- Akzeptanz: ein durchgehender Clip ohne Stufe bleibt feldgleich, Proben inklusive, sofern der Tag eine Session hat (kein Manifest, Proben werden gerechnet und verworfen, nichts geschrieben). Ein `suspected` Clip hat keine Evidence-Fenster.
- Rollback: Flag aus. Bereits auf `invalid` gesetzte Sessions bleiben so, bis jemand `tva align` neu laufen lässt. Der PR sagt das. Der Audit setzt nichts auf `invalid`.

### PR-34 — Audit KW38–KW40 und 20/88

- Abhängigkeit: PR-29 (dieselbe Probenfunktion, nur lesend).
- Flag: keiner. Der Befehl schreibt nur die Audit-Datei.
- Tun: Abschnitt 2.7. Klassen auf dem heutigen `session_utc_window`.
- Tests: synthetische Mischung aus innen, in zwei Fenstern, außerhalb; fehlende CSV → `not_run`; keine Session-Datei ändert mtime.
- Akzeptanz: Accumu kann die Datei lesen und D4 entscheiden. Ohne Store auf der NAS ist der CI-Test synthetisch.
- Rollback: Datei löschen. Nichts sonst ist entstanden.
- **Gate:** PR-30 wird nicht gemerged, bevor D4 auf dem Audit-Protokoll steht oder Accumu den Gate schriftlich aufhebt.

### PR-30 — Fill-Besitz

- Abhängigkeit: PR-28, PR-29, Gate PR-34.
- Flag: `TVA_EXCLUSIVE_FILLS`.
- Tun: Abschnitt 2.3. Legacy-Fenster unberührt.
- Tests: M1, M2, M5 (Clip beansprucht nichts), M7, L0 Parquets gleich.
- Akzeptanz: keine `fill`-Identität in mehr als einem Session-Parquet desselben Tages. `outside` vollständig.
- Rollback: Flag aus. Session-Parquets, die der Tages-Pfad schon geschrieben hat, werden von diesem PR nicht automatisch auf den alten Stand gehoben. Der PR-Text sagt: Rollback vor dem nächsten `tva fills`, oder `tva fills` einmal auf dem Legacy-Pfad neu. Kein stilles Mischparquet.

### PR-31 — Tagesregeln und Ledger einmal

- Abhängigkeit: PR-30.
- Flag: `TVA_DAY_RULES`.
- Tun: Abschnitt 2.4. Ledger-Id `day:YYYY-MM-DD`.
- Tests: M7 zählt in R-MAX10; M9; Rollup-Zähler 1; R-CLOSE und R-PLAYBOOK auf einem gültigen Clip unverändert; L0 `rules.json` gleich; R-DLL weiter unverifiable bei null-Limit.
- Akzeptanz: die fünf Regeln im Session-JSON sind `unverifiable` mit Tagesgrund, sobald der Tages-Pfad aktiv ist. Die Tagesdatei hat genau eine Zeile pro Regel.
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
- Tests: L0 Rate gleich; M1 Zähler ohne Doppelte; M5 Nenner ohne die kurze Dauer, Rate null wenn nichts anderes übrig ist, kein eingesetzter OCR-Span.
- Akzeptanz: Feld `trades_per_hour` auf L0 gleich. Auf M5 nicht die heutige überhöhte Rate.
- Rollback: Flag aus, Rollup neu erzeugen. Alte Formel.

### Review-Checkliste, jeder PR

Ein separater Agent, nicht der Autor. Er hakt nur ab, was er im Diff sieht.

1. Flag default aus, und mit Flag aus ist der bisherige Teststand grün, ohne angepasste Erwartungen.
2. L0 mit Flag an feldgleich zu Flag aus, Laufzeitstempel benannt und nur diese ausgenommen.
3. Kein Schreiben außerhalb `sessions/`, `days/`, `ledger/` unter `TVA_ROOT`. Kein Video-Write. Kein `/musiclabel`.
4. Kein neuer Fallback, der bei fehlender Probe, fehlender CSV oder überlappendem Kern einen Wert erfindet.
5. Sortierung fest, Test mit vertauschter Ingest-Reihenfolge, wo der PR Tageszustand schreibt.
6. Alte `session.json` lädt. Session-`schema_version` bleibt `"1"`, außer der PR begründet eine Anhebung und migriert lesend.
7. Keine Änderung an Prompts, Coach, Proposals-Logik, `rules.yaml`, R-CLOSE, Sprachregeln auf gültigen Clips.
8. Phase 2 kommt im Diff nicht vor: kein Datenvertrag, kein Abschalten einer Analyse, die nicht in 2.2 als Unterdrückung bei `invalid` genannt ist.

---

## 5. Risiken

1. **Parität nur auf L0.** Ein realer Ein-Clip-Tag mit Fills im 30-min-Pad bleibt auf dem Legacy-Pfad richtig. Ein realer Ein-Clip-Tag mit einer unentdeckten Pause (`unverifiable`, keine Proben) bleibt so falsch wie heute, und das Manifest sagt das nur, wenn der Guard gelaufen ist. Ohne `TVA_PAUSE_GUARD` ändert sich nichts. Das ist gewollt und leicht mit „wir haben die Pause gefixt“ zu verwechseln.
2. **Drei Proben finden eine Stufe, nicht die Sekunde.** Stückweise Evidence auf pausierten Altbändern leistet Phase 1 nicht. KW40-Mittwoch bleibt für Fill-zu-Videozeit unbenutzbar, bis jemand D5 auf Stückweise stellt und das eine eigene Serie wird.
3. **5 s Stitch.** Ein Stop/Start innerhalb von 5 s wird ein Clip. Der Plan ändert das nicht. Wer Blöcke trennen will, wartet länger als 5 s. Steht in D8 nur am Rand; die eigentliche Empfehlung ist betrieblich, nicht ein Code-Change.
4. **Ledger-Zeile `day:`.** Ein vergessener Rollback lässt eine Tagesverletzung neben den alten Session-Zeilen stehen. PR-31 beschreibt das Löschen. Der Review prüft, dass der Rollup die Session-Zeilen der fünf Regeln nicht zusätzlich zählt, solange sie `unverifiable` mit Tagesgrund sind.
5. **`tva fills` nach einem halben Rollback.** PR-30 warnt davor. Ein Session-Parquet vom Tages-Pfad und Regeln vom Legacy-Pfad wären eine Mischung. Der Flag-Satz ist einer: 30, 31, 32, 33 zusammen an oder aus, sobald sie gemerged sind. 28 und 29 dürfen einzeln an sein.
6. **DST-Woche und ETH-Datum.** Der Tagesschlüssel ist Vienna. Ein Join außen auf `session_date` kann in der Übergangswoch um 23:00 Vienna danebenliegen. Der Audit berichtet beide Daten, damit man das sieht, ohne die Zuordnung umzustellen.
7. **Notion-Seite existiert schon und ist der schlechte Clip-Debrief.** Der erste Tages-Publish ersetzt sie. Das ist der Zweck. Wer den alten Text behalten will, muss ihn vorher kopieren. Der PR sagt das in einem Satz, ohne eine zweite Seite anzulegen.
8. **Proben lesen die NAS.** Nur lesen, nur drei Frames, nur wenn der Guard oder `--sample-clocks` an ist. Ein fehlgeschlagenes OCR der Uhr ist `unverifiable`, kein erfundener Offset.

---

## 6. Offene Entscheidungen

Jede mit Empfehlung. Der Plan ist so geschrieben, dass die Empfehlung gilt, wenn Accumu nicht widerspricht. Ein Widerspruch ändert den betroffenen PR, nicht die schon gemergten Vorgänger, solange die Reihenfolge eingehalten wird.

### D1. Tagesschlüssel

Vienna-Kalendertag des Dateistarts, nicht ETH 18:00 New York.

Empfehlung: Vienna. Das ist Session-Id, Notion-Titel und Ledger-Datum. Die NY-Morgenclips liegen auf beiden Kalendern auf demselben Tag. Die Abweichung sitzt um 23:00 Vienna in den zwei DST-Übergangswochen und betrifft diese Aufnahmen nicht. Fills werden über den Zeitstempel zugeordnet, nicht über `session_date`.

### D2. Fills außerhalb des Pads auf einem einzelnen Clip

Die Kurzfassung sagt, Fills außerhalb der Clips zählen in den Tagesregeln. Auf einem einzelnen durchgehenden Clip ist „außerhalb“ heute alles jenseits des 30-min-Pads, und die Session-Regeln zählen es nicht. Beides gleichzeitig geht nicht mit Parität.

Empfehlung: Legacy-Pfad lässt sie draußen. Der Tages-Pfad (mehrere Clips oder ein invalidierter Clip) zählt sie. L0 bleibt feldgleich.

### D3. Was der Nenner von Trades/Stunde ist

Empfehlung: Summe der `duration_s` der Clips, die Fills besitzen dürfen. `suspected` trägt nicht bei. Keine geschätzte Wandspanne. Zähler eindeutig. Legacy-Pfad exakt `duration_s / 3600`.

Alternative, nicht gebaut: Spanne vom ersten Fill-Zeitstempel bis zum letzten, Lücken zwischen Blöcken inklusive. Die Rate wäre niedriger und auf L0 ungleich `duration_s`, sobald der Clip länger ist als der Fill-Span. Deshalb nicht der Default.

### D4. Ursache von 20/88

Offen, bis PR-34 die Klassen geschrieben hat. Keine Zeitzonen-Korrektur, kein anderes Fenster und kein Dedupe-Fix in PR-30, der nur diese Zahl erklären soll. PR-30 setzt den Besitz um, wie in 2.3 beschrieben, unabhängig davon. Stoppt wird nur, wenn der Audit zeigt, dass der Fehlbetrag **nicht** das Fenster und nicht die Doppelzählung ist, sondern etwas, das 2.3 nicht trifft (zum Beispiel ein durchgängig falscher CSV-Offset). Dann wird D4 neu vorgelegt, bevor PR-30 mergt.

### D5. Pause: invalidieren oder stückweise

Empfehlung: invalidieren, ganze Datei, Zeitmapping aus.

Stückweise bräuchte Schnittstellen auf die Sekunde, ein anderes Alignment-Objekt als der eine Strahl, und alle Leser von `wall_to_video_t` (`evidence.py:127`, `rules.py:689`). Drei Proben liefern die Stufe, nicht den Schnitt. Ein Suchlauf über die Datei wäre eine zweite Uhr und läge außerhalb von „nur so viel ändern, wie der Bug erzwingt“. Transkripte bleiben benutzbar, weil sie in Videozeit sind. Fill-zu-Videozeit auf KW40-Mittwoch bleibt aus, bis eine spätere Serie das ausdrücklich will. Diese Serie ist nicht Phase 2 und nicht Phase 1.

### D6. Notion

Empfehlung: eine Seite, bisheriger Titel, ein Schreiber, Body aus dem Tag. Nicht ein Titel pro Clip mit Uhrzeit.

### D7. KW38–KW40 neu rechnen

Empfehlung: nur Audit. Markierung steht im Audit-JSON (`pause_check`), nicht in den Session-Ordnern. Neu transkribieren, neu alignen oder Notion überschreiben erst, wenn Accumu das nach dem Audit sagt. Das ist dann ein eigener Auftrag, kein stiller Schritt in PR-34.

### D8. Session-Id und 5-s-Stitch

Empfehlung: Ids nicht ändern. Stitch-Regel nicht ändern. M8 dokumentiert die Kollision gleicher Startsekunde. Wer zwei Blöcke will, lässt mehr als 5 s zwischen Ende und nächstem Start. Ein Präfix in der Id wäre eine Migration aller bestehenden Ordner und bricht jeden Pfad, der `YYYY-MM-DD_HHMMSS` erwartet.

### D9. R-CLOSE

Empfehlung: pro Session lassen. `flat_by` ist null, die Regel ist unverifiable. Sie in den Tageskatalog zu ziehen würde eine Regel anfassen, die der Auftrag nicht nennt, ohne heute eine andere Zahl zu erzeugen.

### D10. Welche echte Session L0 auf der NAS ist

Offen, nur für den optionalen Test. CI läuft auf der synthetischen L0. Accumu nennt die Id, wenn er den NAS-Test will. Bis dahin überspringt der Test.

---

## 7. Nicht in diesem Plan

Phase 2, erst wenn Phase 1 gemerged ist und der Tages-Pfad auf einem echten Mehrclip-Tag gelaufen ist: ob TVA Analysen abschaltet, umbaut, oder dem Trading Coach einen Datenvertrag gibt (Transkript, Manifest, Uhrzeit nur wenn verifiziert, Vollständigkeitszahlen, keine Urteile). Fakten-Spezifikation und die zweite, unabhängige Rechnung aus dem Rohexport gehören dorthin. Sie sind hier nicht entworfen, damit eine Abweichung nach Phase 1 nur eine Ursache hat.
