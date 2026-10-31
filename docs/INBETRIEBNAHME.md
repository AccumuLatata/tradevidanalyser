# TVA – Inbetriebnahmeplan

Grundsätze: Jeder Schritt erst nach Florians Freigabe des vorherigen.
Rollback = betroffenes Flag aus (Unset). Nichts wird still geschrieben –
jeder Schreibvorgang (NAS tradevid, TradesViz, Notion) nur nach
Ankündigung und Freigabe.

Stand der Doku: `main` @ `dbde7ae` (GitHub #49). Der ursprüngliche
KW44-Testlauf (30.10.2026) lief auf `239d01c` (#44). GitHub #45–#49
sind danach gemerged; der Nachtrag unten gilt nach diesen Merges.

## 0. Testlauf KW44 (erledigt, 30.10.2026)

- Ergebnis: [`KW44_DRYRUN.md`](KW44_DRYRUN.md). Netto-Abgleich
  ThesisTester ↔ TopstepX über alle Lots der Woche ok.
- Blocker auf `239d01c`: Erstbild-Versatz bimodal (eine Gruppe um
  ~0,30 s, eine um ~1,05 s). Die alte einzelne Band 0,8–1,3 s (seit
  #45 ersetzt) hat die 0,3-s-Gruppe verworfen; der Tagespfad war
  damit blockiert. Ein Teil der Fills lag nicht auf Video.
- Vor Schritt 1 klären: Ursache der 0,30-s-Gruppe (Sidecar-Plugin /
  OBS-Timing), Evidence-Fenster vor Clip-Start (seit #46 hinter
  `TVA_SIDECAR` behoben), Rückstände eines abgebrochenen `day build`
  (seit #46 voller Rollback).
- **Freigabe Florian nötig.**

## 0b. Nachtrag nach GitHub #46–#49

Gelandet, geprüft gegen den Code (nicht gegen einen neuen Live-Export):

| GitHub | Merge | Für die Inbetriebnahme |
|---|---|---|
| #45 | `13e6892` | Zwei Erstbild-Bänder 0,20–0,45 s und 0,80–1,30 s; `offset_mode`. Die 0,3-s-Gruppe aus KW44 liegt im Low-Band. |
| #46 | `1128a6f` | Sidecar-Fallback: Fill-once, Gebühren bleiben bei installiertem ThesisTester, voller Rollback bei Abbruch, echte TT-Reconcile-Status, No-Video-Kennzahl ohne Padding; `TVA_ACCOUNTS_FILE` / `--accounts-file`. |
| #47 | `0b58d82` | `fee_mismatch` in `RECON_STATUSES`; `TVA_DESK_RULES_FILE` / `--desk-rules-file`. |
| #48 | `353c8a8` | Tagesregeln und Besitz sehen nur die Trades dieses Tages. |
| #49 | `dbde7ae` | TT-Tag-Grammatik ab 2026-11-02 (`PB_` / `E_` / `V_` / `T_` / `C_none`). |

Rerun nach #46–#49 (Kurzfassung, siehe [`KW44_DRYRUN.md`](KW44_DRYRUN.md)):

- R-DLL entspricht dem TopstepX-Netto auf den Cent an allen 5 Tagen.
- 27.10. und 28.10. R-DLL verletzt.
- 11 On-Video-Trades stimmen.
- Publish hat korrekt verweigert (kein TT-Wochen-Lock).

Offen bleibt, was schon in Schritt 0 stand: Lots ohne Video bleiben
unassigned; Publish braucht einen TT-Wochen-Lock (`tick prepare` +
`lock_files`). **Freigabe Florian nötig**, bevor Stufe A dauerhaft an
geht.

## 1. Nächste Woche mit Pausen

- Florian nutzt bewusst Pause/Resume; OBS-Start vor dem ersten Trade.
- Prüfung je Abschnitt: |Wandspanne − Videospanne − Erstbild-Versatz|
  ≤ 0,026 s, mit ms-Rundung (Wall-Zeiten auf ms, ffprobe auf ms runden,
  dann vergleichen). Das ist die engere [Rv]-Prüfung
  (`RV_SECTION_RESIDUAL_MAX_S`); der Code-Gate bleibt 0,10 s
  (`SPAN_RESIDUAL_MAX_S` in `sidecar.py`).
- Weiterhin nur Lauf in Testverzeichnis, Report wie KW44.
- **Freigabe Florian nötig.**

## 2. Flags dauerhaft an, gestaffelt

- Stufe A: `TVA_SIDECAR=1`, `TVA_PAUSE_GUARD=1` (erlaubte Kombination).
  Eine Woche beobachten.
- Stufe B (eine Woche später, nach Freigabe): `TVA_DAY_MANIFEST`,
  `TVA_EXCLUSIVE_FILLS`, `TVA_DAY_RULES`, `TVA_DAY_PUBLISH`,
  `TVA_TRADING_HOURS` und `TVA_NET_FEES` (`TVA_NET_FEES` nur zusammen
  mit dem Vierer-Set erlaubt). Voraussetzung: echte `accounts.yaml` mit
  Topstep-Gebühren (über `TVA_ACCOUNTS_FILE` / `--accounts-file`; die
  mitgelieferte Datei bleibt synthetisch).
- Rollback: Flag aus; Tagesordner per `tva day delete <datum>` (benannt,
  nicht still).
- **Freigabe Florian je Stufe.**

## 3. `tva serve` auf dem Farm-PC

- Nur über Tailscale erreichbar (Bind auf Tailscale-IP, kein
  öffentlicher Port), `TVA_API_TOKEN` gesetzt, Medien-Auslieferung aus
  (`TVA_SERVE_MEDIA` aus), außer explizit freigegeben.
- Rollback: Dienst stoppen.
- **Freigabe Florian nötig.**

## 4. Zusammenarbeit Coach ↔ Tester

- Regeln: [`COACH_THESISTESTER_RULES.md`](COACH_THESISTESTER_RULES.md)
  (freigegeben 2026-10-31).
- Der Trading Coach schlägt nur Studien vor. Der Thesistester-Bot führt
  sie auf dem Farm-PC aus. Nur von Florian freigegebene Ergebnisse
  (Program B Run 2 oder neuer) fließen ins Coaching.
- Rollback: Auslöser deaktivieren.
- **Freigabe Florian nötig.**
