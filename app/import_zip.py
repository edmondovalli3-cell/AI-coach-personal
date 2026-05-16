"""Import dell'archivio completo esportato da Garmin Connect.

L'export è un zip con questa struttura tipica (verificata sull'export reale di Edmondo):
    DI_CONNECT/
        DI-Connect-Fitness/
            <email>_0_summarizedActivities.json   ← TUTTE le attività
        DI-Connect-Wellness/
            <range>_<userId>_sleepData.json       ← sonno per range
            <range>_<userId>_healthStatusData.json ← HRV, body battery
        DI-Connect-Aggregator/
            UDSFile_<range>.json                  ← daily summary (passi, RHR, stress, kcal)
        DI-Connect-Metrics/
            TrainingReadinessDTO_*.json
            MetricsMaxMetData_*.json              ← VO2max
            RunRacePredictions_*.json
            EnduranceScore_*.json
            HillScore_*.json
            MetricsAcuteTrainingLoad_*.json
            TrainingHistory_*.json

⚠️ ATTENZIONE — UNITÀ DI MISURA:
L'export usa unità diverse dall'API live:
    distance         → centimetri (NON metri)
    duration         → millisecondi (NON secondi)
    elevationGain    → centimetri
    avgSpeed         → cm/ms  (≈ m/s × 10)
    startTimeGmt     → ms epoch (es. 1.778847209E12)
"""
from __future__ import annotations

import json
import logging
import re
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import db

log = logging.getLogger("import")


# ===========================================================================
# Activities
# ===========================================================================

def _parse_summarized_activities(raw: Any) -> int:
    """raw può essere lista di dict, dict con chiave 'summarizedActivitiesExport', ecc."""
    if isinstance(raw, dict):
        if "summarizedActivitiesExport" in raw:
            return _parse_summarized_activities(raw["summarizedActivitiesExport"])
        return _import_activity(raw)
    n = 0
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and "summarizedActivitiesExport" in entry:
                n += _parse_summarized_activities(entry["summarizedActivitiesExport"])
            else:
                try:
                    n += _import_activity(entry)
                except Exception as e:  # noqa: BLE001
                    log.warning("Skip activity: %s", e)
    return n


def _to_iso(value: Any) -> str | None:
    """Converte un timestamp Garmin (ms epoch o string) in ISO 8601."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Garmin export usa ms epoch (es. 1.778847209E12)
        ts = value / 1000.0 if value > 1e12 else value
        try:
            return datetime.fromtimestamp(ts).isoformat()
        except (ValueError, OSError):
            return None
    if isinstance(value, str):
        return value
    return None


def _import_activity(a: dict) -> int:
    """Importa una singola attività dall'export Garmin (unità cm/ms)."""
    if not isinstance(a, dict):
        return 0

    activity_id = a.get("activityId") or a.get("activityIdString")
    if not activity_id:
        return 0

    # --- UNIT CONVERSION ---
    # L'export usa centimetri e millisecondi, NON metri e secondi
    distance_m = (a.get("distance") or 0) / 100.0  # cm → m
    duration_sec = (a.get("duration") or 0) / 1000.0  # ms → s
    moving_sec = (a.get("movingDuration") or a.get("duration") or 0) / 1000.0
    elev_gain_m = (a.get("elevationGain") or 0) / 100.0
    elev_loss_m = (a.get("elevationLoss") or 0) / 100.0

    # avgSpeed nell'export è in cm/ms ≈ m/s × 10 (in realtà cm/ms = 10× m/s)
    avg_speed_export = a.get("avgSpeed") or 0
    # Conferma empirica: 0.3816 cm/ms ≈ 3.82 m/s (4:22/km)
    # → m/s = (cm/ms) × 10
    avg_speed_mps = avg_speed_export * 10.0 if avg_speed_export else 0
    max_speed_export = a.get("maxSpeed") or 0
    max_speed_mps = max_speed_export * 10.0 if max_speed_export else 0

    avg_pace_sec = (1000.0 / avg_speed_mps) if avg_speed_mps else None

    # Timestamp
    start = a.get("startTimeLocal") or a.get("startTimeGmt") or a.get("beginTimestamp")
    start_iso = _to_iso(start)

    # Tipo attività: nell'export è una stringa (es. "running"), non un dict
    activity_type = a.get("activityType") or a.get("sportType", "").lower()

    row = {
        "id": str(activity_id),
        "source": "import",
        "type": activity_type if activity_type else None,
        "name": a.get("name") or a.get("activityName"),
        "start_time": start_iso,
        "duration_sec": duration_sec,
        "moving_sec": moving_sec,
        "distance_m": distance_m,
        "elev_gain_m": elev_gain_m,
        "elev_loss_m": elev_loss_m,
        "avg_pace_sec": avg_pace_sec,
        "avg_hr": a.get("avgHr"),
        "max_hr": a.get("maxHr"),
        "avg_cadence": (a.get("avgRunCadence") * 2 if a.get("avgRunCadence") else None) or
                        a.get("avgBikeCadence") or
                        a.get("avgDoubleCadence"),
        "max_cadence": (a.get("maxRunCadence") * 2 if a.get("maxRunCadence") else None) or
                        a.get("maxBikeCadence") or
                        a.get("maxDoubleCadence"),
        "avg_power": a.get("avgPower"),
        "max_power": a.get("maxPower"),
        "avg_speed": avg_speed_mps,
        "max_speed": max_speed_mps,
        "calories": a.get("calories"),
        "aerobic_te": a.get("aerobicTrainingEffect"),
        "anaerobic_te": a.get("anaerobicTrainingEffect"),
        "training_load": a.get("activityTrainingLoad"),
        "vo2max": a.get("vO2MaxValue"),
        "pte": a.get("performanceCondition"),
        "summary_json": json.dumps(a, default=str),
        "detail_json": None,
    }
    db.upsert_activity(row)
    return 1


# ===========================================================================
# Sonno (struttura reale dell'export, FLAT)
# ===========================================================================

def _import_sleep_file(data: Any) -> int:
    """Sonno: lista di entry, ogni entry è un dict con `calendarDate`, durate in SECONDI,
    `sleepScores.overallScore`, ecc.
    """
    if not isinstance(data, list):
        return 0
    n = 0
    for entry in data:
        if not isinstance(entry, dict):
            continue
        cal = entry.get("calendarDate")
        if not cal:
            continue
        try:
            deep = entry.get("deepSleepSeconds") or 0
            light = entry.get("lightSleepSeconds") or 0
            rem = entry.get("remSleepSeconds") or 0
            awake = entry.get("awakeSleepSeconds") or 0
            total = deep + light + rem  # tempo dormito (escluso awake)

            scores = entry.get("sleepScores") or {}
            score = scores.get("overallScore") if isinstance(scores, dict) else None

            spo2 = entry.get("spo2SleepSummary") or {}

            row = {
                "date": cal,
                "score": score,
                "duration_sec": total,
                "deep_sec": deep,
                "light_sec": light,
                "rem_sec": rem,
                "awake_sec": awake,
                "avg_hrv": None,  # non presente in questo file (è in healthStatusData)
                "avg_resp_rate": entry.get("averageRespiration"),
                "avg_spo2": spo2.get("averageSPO2") if isinstance(spo2, dict) else None,
                "avg_stress": entry.get("avgSleepStress"),
                "raw_json": json.dumps(entry, default=str),
            }
            db.upsert_sleep(row)
            n += 1
        except Exception as e:  # noqa: BLE001
            log.debug("Sleep import skip (%s): %s", cal, e)
    return n


# ===========================================================================
# Daily Summary (UDSFile): passi, RHR, kcal, stress
# ===========================================================================

def _import_uds_file(data: Any) -> int:
    if not isinstance(data, list):
        return 0
    n = 0
    for entry in data:
        if not isinstance(entry, dict):
            continue
        cal = entry.get("calendarDate")
        if not cal:
            continue

        # Stress è dentro allDayStress.aggregatorList[type=TOTAL]
        stress_avg = None
        stress_max = None
        all_day = entry.get("allDayStress") or {}
        agg_list = all_day.get("aggregatorList") if isinstance(all_day, dict) else None
        if isinstance(agg_list, list):
            for a in agg_list:
                if isinstance(a, dict) and a.get("type") == "TOTAL":
                    stress_avg = a.get("averageStressLevel")
                    stress_max = a.get("maxStressLevel")
                    break

        try:
            row = {
                "date": cal,
                "body_battery_high": None,  # in altri file (bodyBattery)
                "body_battery_low": None,
                "body_battery_charged": None,
                "body_battery_drained": None,
                "stress_avg": stress_avg,
                "stress_max": stress_max,
                "hrv_status": None,
                "hrv_weekly_avg": None,
                "hrv_last_night": None,
                "resting_hr": entry.get("restingHeartRate"),
                "steps": entry.get("totalSteps"),
                "floors_climbed": entry.get("floorsAscendedInMeters"),
                "active_kcal": entry.get("activeKilocalories"),
                "raw_json": json.dumps(entry, default=str)[:50000],  # tronco i file enormi
            }
            db.upsert_wellness(row)
            n += 1
        except Exception as e:  # noqa: BLE001
            log.debug("UDS import skip (%s): %s", cal, e)
    return n


# ===========================================================================
# Training Readiness
# ===========================================================================

def _import_readiness_file(data: Any) -> int:
    """File TrainingReadinessDTO_*: lista di entry, una per giorno (di solito multiple al giorno,
    prendiamo la più recente)."""
    if not isinstance(data, list):
        return 0
    by_date: dict[str, dict] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        cal = entry.get("calendarDate") or (entry.get("timestamp") or "")[:10]
        if not cal:
            continue
        prev = by_date.get(cal)
        if prev is None or (entry.get("timestamp") or "") > (prev.get("timestamp") or ""):
            by_date[cal] = entry
    n = 0
    for cal, entry in by_date.items():
        try:
            row = {
                "date": cal,
                "training_readiness": entry.get("score"),
                "training_status": entry.get("level"),
                "acute_load": entry.get("acuteLoad"),
                "load_focus": None,
                "recovery_time_hours": entry.get("recoveryTime"),
                "vo2max_running": None,
                "vo2max_cycling": None,
                "endurance_score": None,
                "hill_score": None,
                "race_predictor_5k": None,
                "race_predictor_10k": None,
                "race_predictor_half": None,
                "race_predictor_full": None,
                "raw_json": json.dumps(entry, default=str),
            }
            # Merge con eventuale riga esistente (non sovrascrivere se già abbiamo dati)
            _merge_training(row)
            n += 1
        except Exception as e:  # noqa: BLE001
            log.debug("Readiness skip (%s): %s", cal, e)
    return n


def _import_vo2max_file(data: Any) -> int:
    if not isinstance(data, list):
        return 0
    n = 0
    for entry in data:
        if not isinstance(entry, dict):
            continue
        cal = entry.get("calendarDate") or entry.get("date")
        if not cal:
            continue
        sport = entry.get("sport") or "RUNNING"
        value = entry.get("vo2MaxValue") or entry.get("vo2Max")
        if value is None:
            continue
        row = {
            "date": cal,
            "training_readiness": None,
            "training_status": None,
            "acute_load": None,
            "load_focus": None,
            "recovery_time_hours": None,
            "vo2max_running": value if sport.upper() in ("RUNNING", "RUN") else None,
            "vo2max_cycling": value if sport.upper() in ("CYCLING", "CYCLE") else None,
            "endurance_score": None,
            "hill_score": None,
            "race_predictor_5k": None,
            "race_predictor_10k": None,
            "race_predictor_half": None,
            "race_predictor_full": None,
            "raw_json": json.dumps(entry, default=str),
        }
        _merge_training(row)
        n += 1
    return n


def _import_race_pred_file(data: Any) -> int:
    if not isinstance(data, list):
        return 0
    n = 0
    for entry in data:
        if not isinstance(entry, dict):
            continue
        cal = entry.get("calendarDate")
        if not cal:
            continue
        row = {
            "date": cal,
            "training_readiness": None,
            "training_status": None,
            "acute_load": None,
            "load_focus": None,
            "recovery_time_hours": None,
            "vo2max_running": None,
            "vo2max_cycling": None,
            "endurance_score": None,
            "hill_score": None,
            "race_predictor_5k": entry.get("time5K") or entry.get("raceTime5K"),
            "race_predictor_10k": entry.get("time10K") or entry.get("raceTime10K"),
            "race_predictor_half": entry.get("timeHalf") or entry.get("raceTimeHalf"),
            "race_predictor_full": entry.get("timeMarathon") or entry.get("raceTimeMarathon"),
            "raw_json": json.dumps(entry, default=str),
        }
        _merge_training(row)
        n += 1
    return n


def _import_endurance_file(data: Any, kind: str) -> int:
    """kind = 'endurance' o 'hill'"""
    if not isinstance(data, list):
        return 0
    n = 0
    for entry in data:
        if not isinstance(entry, dict):
            continue
        cal = entry.get("calendarDate")
        if not cal:
            continue
        value = entry.get("value") or entry.get("score") or entry.get("overallScore")
        if value is None:
            continue
        row = {
            "date": cal,
            "training_readiness": None,
            "training_status": None,
            "acute_load": None,
            "load_focus": None,
            "recovery_time_hours": None,
            "vo2max_running": None,
            "vo2max_cycling": None,
            "endurance_score": value if kind == "endurance" else None,
            "hill_score": value if kind == "hill" else None,
            "race_predictor_5k": None,
            "race_predictor_10k": None,
            "race_predictor_half": None,
            "race_predictor_full": None,
            "raw_json": json.dumps(entry, default=str),
        }
        _merge_training(row)
        n += 1
    return n


def _merge_training(row: dict) -> None:
    """Upsert una riga di training senza sovrascrivere campi non-None già presenti."""
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute("SELECT * FROM training WHERE date=?", (row["date"],)).fetchone()
        if existing:
            merged = dict(existing)
            for k, v in row.items():
                if v is not None:
                    merged[k] = v
            row = merged
        db.upsert_training(row)
    finally:
        conn.close()


# ===========================================================================
# Main
# ===========================================================================

def import_garmin_export(zip_path: str) -> dict:
    """Import principale: legge lo zip e popola il DB."""
    stats = {"activities": 0, "sleep": 0, "wellness": 0, "training": 0,
             "files_scanned": 0, "errors": 0}
    log.info("Apertura archivio %s", zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        log.info("File nell'archivio: %d", len(names))

        for name in names:
            if not name.lower().endswith(".json"):
                continue
            stats["files_scanned"] += 1

            try:
                with zf.open(name) as f:
                    raw = f.read()
                    if not raw.strip() or raw.strip() in (b"[]", b"{}"):
                        continue
                    data = json.loads(raw)
            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                log.debug("JSON parse fail %s: %s", name, e)
                continue

            try:
                lower = name.lower()

                # --- attività ---
                if "summarizedactivities" in lower:
                    n = _parse_summarized_activities(data)
                    stats["activities"] += n
                    log.info("  %s → %d attività", Path(name).name, n)

                # --- sonno ---
                elif "sleepdata" in lower:
                    n = _import_sleep_file(data)
                    stats["sleep"] += n
                    log.info("  %s → %d notti", Path(name).name, n)

                # --- daily summary ---
                elif "udsfile" in lower:
                    n = _import_uds_file(data)
                    stats["wellness"] += n
                    log.info("  %s → %d giorni", Path(name).name, n)

                # --- training readiness ---
                elif "trainingreadinessdto" in lower:
                    n = _import_readiness_file(data)
                    stats["training"] += n
                    log.info("  %s → %d giorni readiness", Path(name).name, n)

                # --- VO2max ---
                elif "metricsmaxmetdata" in lower:
                    n = _import_vo2max_file(data)
                    stats["training"] += n

                # --- race predictions ---
                elif "runracepredictions" in lower:
                    n = _import_race_pred_file(data)
                    stats["training"] += n

                # --- endurance score ---
                elif "endurancescore" in lower:
                    n = _import_endurance_file(data, "endurance")
                    stats["training"] += n

                # --- hill score ---
                elif "hillscore" in lower:
                    n = _import_endurance_file(data, "hill")
                    stats["training"] += n

            except Exception as e:  # noqa: BLE001
                stats["errors"] += 1
                log.warning("Import skip %s: %s", name, e)

    db.set_meta("last_import_at", datetime.utcnow().isoformat())
    log.info("Import completato: %s", stats)
    return stats
