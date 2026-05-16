"""Integrazione con Garmin Connect via libreria `garminconnect`.

Garth è stato deprecato a marzo 2026 dopo che Garmin ha cambiato il sistema
di autenticazione. La libreria `garminconnect` (di cyberjunky) usa lo stesso
flusso SSO mobile dell'app ufficiale Garmin Android e funziona regolarmente.

- Login con MFA tramite callback in thread separato (per il flusso web).
- Salvataggio token OAuth in `app/data/garmin_tokens/` con refresh automatico.
- Sync di attività, sonno, HRV, Body Battery, Stress, Training Readiness, ecc.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from garminconnect import Garmin
from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError

from . import db

log = logging.getLogger("garmin")

# Directory dei token Garmin — env var DATA_DIR ha precedenza (per cloud deploy)
_DATA_DIR = os.environ.get("DATA_DIR") or str(Path(__file__).parent / "data")
TOKEN_DIR = Path(_DATA_DIR) / "garmin_tokens"


# ===========================================================================
# Login con MFA
# ===========================================================================

class LoginSession:
    """Gestisce un login Garmin in corso (con MFA opzionale).

    `Garmin(...).login()` è bloccante e può chiedere il codice MFA via callback.
    Lo facciamo girare in un thread separato; il callback aspetta su una queue
    che viene riempita quando l'utente invia il codice via API.
    """

    def __init__(self) -> None:
        self._mfa_q: queue.Queue[str] = queue.Queue(maxsize=1)
        self._result: dict | None = None
        self._needs_mfa = threading.Event()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: Garmin | None = None

    def start(self, email: str, password: str) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Login già in corso")
        self._result = None
        self._needs_mfa.clear()
        self._done.clear()

        def worker() -> None:
            try:
                TOKEN_DIR.mkdir(parents=True, exist_ok=True)

                def prompt_mfa() -> str:
                    self._needs_mfa.set()
                    log.info("Garmin: in attesa codice MFA…")
                    code = self._mfa_q.get(timeout=600)  # max 10 min
                    return code.strip()

                client = Garmin(email, password, prompt_mfa=prompt_mfa)
                client.login(str(TOKEN_DIR))
                self._client = client
                _set_active_client(client)
                self._result = {"ok": True, "user": getattr(client, "display_name", None) or email}
                log.info("Garmin: login OK")
            except GarminConnectAuthenticationError as e:
                log.warning("Garmin auth fail: %s", e)
                self._result = {"ok": False, "error": f"Credenziali non valide: {e}"}
            except GarminConnectConnectionError as e:
                log.warning("Garmin connection fail: %s", e)
                self._result = {"ok": False, "error": f"Connessione fallita: {e}"}
            except Exception as e:  # noqa: BLE001
                log.exception("Garmin login fallito")
                self._result = {"ok": False, "error": str(e)}
            finally:
                self._done.set()

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def submit_mfa(self, code: str) -> None:
        if not self._needs_mfa.is_set():
            raise RuntimeError("Login non sta aspettando un codice MFA")
        self._mfa_q.put(code)

    def status(self) -> dict:
        if self._done.is_set():
            assert self._result is not None
            return {"status": "ok" if self._result["ok"] else "error", **self._result}
        if self._needs_mfa.is_set() and self._mfa_q.empty():
            return {"status": "needs_mfa"}
        return {"status": "pending"}


# Una sola sessione attiva alla volta (app single-user)
_login_session: LoginSession | None = None
_active_client: Garmin | None = None


def _set_active_client(client: Garmin) -> None:
    global _active_client
    _active_client = client


def get_login_session() -> LoginSession:
    global _login_session
    if _login_session is None:
        _login_session = LoginSession()
    return _login_session


def reset_login_session() -> None:
    global _login_session
    _login_session = None


# ===========================================================================
# Resume sessione (silenzioso)
# ===========================================================================

def has_saved_session() -> bool:
    return TOKEN_DIR.exists() and any(TOKEN_DIR.iterdir())


def ensure_authenticated() -> bool:
    """Carica la sessione salvata se serve. Restituisce True se ok."""
    global _active_client
    if _active_client is not None:
        return True
    if not has_saved_session():
        return False
    # Proviamo diverse strategie di "resume" perché l'API di garminconnect
    # è leggermente diversa tra versioni.
    for attempt in ("no_args", "empty_strings"):
        try:
            if attempt == "no_args":
                client = Garmin()
            else:
                client = Garmin("", "")
            client.login(str(TOKEN_DIR))
            _active_client = client
            log.info("Sessione Garmin ripristinata (strategy=%s)", attempt)
            return True
        except TypeError as e:
            log.debug("Strategy %s non valida: %s", attempt, e)
            continue
        except Exception as e:  # noqa: BLE001
            log.warning("Resume Garmin fallito (strategy=%s): %s", attempt, e)
            return False
    return False


def logout() -> None:
    """Cancella la sessione salvata."""
    import shutil
    global _active_client
    if TOKEN_DIR.exists():
        shutil.rmtree(TOKEN_DIR)
    _active_client = None
    reset_login_session()


def whoami() -> str | None:
    if ensure_authenticated() and _active_client:
        try:
            return _active_client.display_name or _active_client.username
        except Exception:  # noqa: BLE001
            return None
    return None


def _client() -> Garmin:
    if not ensure_authenticated() or _active_client is None:
        raise RuntimeError("Garmin non autenticato")
    return _active_client


# ===========================================================================
# Helper estrazione dati (i nomi delle chiavi cambiano leggermente tra versioni)
# ===========================================================================

def _safe(d: Any, *keys, default=None):
    if d is None:
        return default
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k)
        elif isinstance(d, list) and isinstance(k, int) and 0 <= k < len(d):
            d = d[k]
        else:
            return default
        if d is None:
            return default
    return d


# ===========================================================================
# SYNC
# ===========================================================================

def fetch_activities(start: int = 0, limit: int = 50) -> list[dict]:
    return _client().get_activities(start, limit) or []


def fetch_activity_detail(activity_id: str) -> dict:
    try:
        return _client().get_activity(activity_id) or {}
    except Exception:  # noqa: BLE001
        return {}


def fetch_activity_splits(activity_id: str) -> dict:
    try:
        return _client().get_activity_splits(activity_id) or {}
    except Exception:  # noqa: BLE001
        return {}


def fetch_daily_sleep(d: date) -> dict | None:
    try:
        return _client().get_sleep_data(d.isoformat())
    except Exception as e:  # noqa: BLE001
        log.debug("Sleep fetch fail %s: %s", d, e)
        return None


def fetch_daily_summary(d: date) -> dict | None:
    """User summary del giorno (steps, kcal, body battery, stress, RHR)."""
    try:
        return _client().get_stats(d.isoformat())
    except Exception as e:  # noqa: BLE001
        log.debug("Stats fetch fail %s: %s", d, e)
        return None


def fetch_body_battery(d: date) -> dict | None:
    try:
        data = _client().get_body_battery(d.isoformat(), d.isoformat())
        if isinstance(data, list) and data:
            return data[0]
        return data
    except Exception:  # noqa: BLE001
        return None


def fetch_hrv(d: date) -> dict | None:
    try:
        return _client().get_hrv_data(d.isoformat())
    except Exception:  # noqa: BLE001
        return None


def fetch_stress(d: date) -> dict | None:
    try:
        return _client().get_stress_data(d.isoformat())
    except Exception:  # noqa: BLE001
        return None


def fetch_training_readiness(d: date) -> dict | None:
    try:
        res = _client().get_training_readiness(d.isoformat())
        if isinstance(res, list) and res:
            return res[0]
        return res
    except Exception:  # noqa: BLE001
        return None


def fetch_training_status(d: date) -> dict | None:
    try:
        return _client().get_training_status(d.isoformat())
    except Exception:  # noqa: BLE001
        return None


def fetch_race_predictions() -> dict | None:
    try:
        return _client().get_race_predictions()
    except Exception:  # noqa: BLE001
        return None


def fetch_max_metrics(d: date) -> Any:
    """Metriche tipo VO2max."""
    try:
        return _client().get_max_metrics(d.isoformat())
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Map Garmin → DB row
# ---------------------------------------------------------------------------

def _activity_to_row(a: dict) -> dict:
    avg_speed = a.get("averageSpeed") or 0  # m/s
    avg_pace_sec = (1000.0 / avg_speed) if avg_speed else None
    start_local = a.get("startTimeLocal") or a.get("startTimeGMT")
    return {
        "id": str(a.get("activityId") or a.get("activityIdString") or ""),
        "source": "garmin",
        "type": _safe(a, "activityType", "typeKey"),
        "name": a.get("activityName"),
        "start_time": start_local,
        "duration_sec": a.get("duration"),
        "moving_sec": a.get("movingDuration") or a.get("duration"),
        "distance_m": a.get("distance"),
        "elev_gain_m": a.get("elevationGain"),
        "elev_loss_m": a.get("elevationLoss"),
        "avg_pace_sec": avg_pace_sec,
        "avg_hr": a.get("averageHR"),
        "max_hr": a.get("maxHR"),
        "avg_cadence": a.get("averageRunningCadenceInStepsPerMinute") or a.get("averageBikingCadenceInRevPerMinute"),
        "max_cadence": a.get("maxRunningCadenceInStepsPerMinute") or a.get("maxBikingCadenceInRevPerMinute"),
        "avg_power": a.get("avgPower"),
        "max_power": a.get("maxPower"),
        "avg_speed": avg_speed,
        "max_speed": a.get("maxSpeed"),
        "calories": a.get("calories"),
        "aerobic_te": a.get("aerobicTrainingEffect"),
        "anaerobic_te": a.get("anaerobicTrainingEffect"),
        "training_load": a.get("activityTrainingLoad"),
        "vo2max": a.get("vO2MaxValue"),
        "pte": a.get("performanceCondition"),
        "summary_json": json.dumps(a, default=str),
        "detail_json": None,
    }


def _sleep_to_row(s: dict, day: date) -> dict:
    dto = _safe(s, "dailySleepDTO") or {}
    return {
        "date": day.isoformat(),
        "score": _safe(dto, "sleepScores", "overall", "value"),
        "duration_sec": dto.get("sleepTimeSeconds"),
        "deep_sec": dto.get("deepSleepSeconds"),
        "light_sec": dto.get("lightSleepSeconds"),
        "rem_sec": dto.get("remSleepSeconds"),
        "awake_sec": dto.get("awakeSleepSeconds"),
        "avg_hrv": dto.get("averageHRV") or dto.get("avgOvernightHrv"),
        "avg_resp_rate": dto.get("averageRespirationValue"),
        "avg_spo2": dto.get("averageSpO2Value"),
        "avg_stress": dto.get("avgSleepStress"),
        "raw_json": json.dumps(s, default=str),
    }


def _wellness_to_row(summary: dict | None, bb: dict | None, day: date) -> dict:
    summary = summary or {}
    bb = bb or {}
    return {
        "date": day.isoformat(),
        "body_battery_high": summary.get("bodyBatteryHighestValue") or bb.get("max"),
        "body_battery_low": summary.get("bodyBatteryLowestValue") or bb.get("min"),
        "body_battery_charged": summary.get("bodyBatteryChargedValue") or bb.get("charged"),
        "body_battery_drained": summary.get("bodyBatteryDrainedValue") or bb.get("drained"),
        "stress_avg": summary.get("averageStressLevel"),
        "stress_max": summary.get("maxStressLevel"),
        "hrv_status": None,
        "hrv_weekly_avg": None,
        "hrv_last_night": summary.get("avgOvernightHrv"),
        "resting_hr": summary.get("restingHeartRate"),
        "steps": summary.get("totalSteps"),
        "floors_climbed": summary.get("floorsAscended"),
        "active_kcal": summary.get("activeKilocalories"),
        "raw_json": json.dumps({"summary": summary, "body_battery": bb}, default=str),
    }


def _training_to_row(tr_readiness: Any, tr_status: dict | None,
                     race_pred: dict | None, vo2: Any, day: date) -> dict:
    readiness = tr_readiness or {}
    if isinstance(readiness, list):
        readiness = readiness[0] if readiness else {}
    status_data = tr_status or {}
    vo2_running = None
    vo2_cycling = None
    if vo2:
        if isinstance(vo2, dict):
            vo2_running = _safe(vo2, "generic", "vo2MaxValue") or vo2.get("vo2MaxValue")
        elif isinstance(vo2, list):
            for v in vo2:
                if not isinstance(v, dict): continue
                sport = _safe(v, "generic", "sport") or v.get("sport")
                value = _safe(v, "generic", "vo2MaxValue") or v.get("vo2MaxValue")
                if sport in ("RUNNING", "running"): vo2_running = value
                elif sport in ("CYCLING", "cycling"): vo2_cycling = value
    return {
        "date": day.isoformat(),
        "training_readiness": readiness.get("score"),
        "training_status": (_safe(status_data, "mostRecentTrainingStatus", "latestTrainingStatusData") or
                            _safe(status_data, "trainingStatus")),
        "acute_load": readiness.get("acuteLoad"),
        "load_focus": None,
        "recovery_time_hours": readiness.get("recoveryTime"),
        "vo2max_running": vo2_running,
        "vo2max_cycling": vo2_cycling,
        "endurance_score": _safe(status_data, "mostRecentEnduranceScore", "value"),
        "hill_score": _safe(status_data, "mostRecentHillScore", "value"),
        "race_predictor_5k": (race_pred or {}).get("raceTime5K") if isinstance(race_pred, dict) else None,
        "race_predictor_10k": (race_pred or {}).get("raceTime10K") if isinstance(race_pred, dict) else None,
        "race_predictor_half": (race_pred or {}).get("raceTimeHalf") if isinstance(race_pred, dict) else None,
        "race_predictor_full": (race_pred or {}).get("raceTimeMarathon") if isinstance(race_pred, dict) else None,
        "raw_json": json.dumps({"readiness": tr_readiness, "status": tr_status,
                                "race": race_pred, "vo2": vo2}, default=str),
    }


# ---------------------------------------------------------------------------
# Orchestrazione sync
# ---------------------------------------------------------------------------

def sync_recent_activities(max_activities: int = 50) -> int:
    if not ensure_authenticated():
        raise RuntimeError("Non autenticato a Garmin")
    activities = fetch_activities(start=0, limit=max_activities)
    n = 0
    for a in activities:
        try:
            db.upsert_activity(_activity_to_row(a))
            n += 1
        except Exception as e:  # noqa: BLE001
            log.warning("Skip activity %s: %s", a.get("activityId"), e)
    db.set_meta("last_activity_sync", datetime.utcnow().isoformat())
    return n


def sync_daily_data(days_back: int = 14) -> dict:
    if not ensure_authenticated():
        raise RuntimeError("Non autenticato a Garmin")
    today = date.today()
    stats = {"wellness": 0, "sleep": 0, "training": 0}
    for offset in range(days_back):
        d = today - timedelta(days=offset)
        # Wellness
        try:
            summary = fetch_daily_summary(d)
            bb = fetch_body_battery(d)
            if summary or bb:
                db.upsert_wellness(_wellness_to_row(summary, bb, d))
                stats["wellness"] += 1
        except Exception as e:  # noqa: BLE001
            log.debug("Wellness %s skip: %s", d, e)
        # Sleep
        try:
            s = fetch_daily_sleep(d)
            if s:
                db.upsert_sleep(_sleep_to_row(s, d))
                stats["sleep"] += 1
        except Exception as e:  # noqa: BLE001
            log.debug("Sleep %s skip: %s", d, e)
        # Training (solo per i giorni più recenti per ridurre chiamate)
        try:
            tr = fetch_training_readiness(d)
            ts = fetch_training_status(d) if offset < 7 else None
            vo2 = fetch_max_metrics(d) if offset == 0 else None
            rp = fetch_race_predictions() if offset == 0 else None
            if tr or ts or vo2 or rp:
                db.upsert_training(_training_to_row(tr, ts, rp, vo2, d))
                stats["training"] += 1
        except Exception as e:  # noqa: BLE001
            log.debug("Training %s skip: %s", d, e)
    db.set_meta("last_daily_sync", datetime.utcnow().isoformat())
    return stats


def full_sync(activities: int = 50, days: int = 14) -> dict:
    a = sync_recent_activities(activities)
    d = sync_daily_data(days)
    return {"activities_synced": a, **d}
