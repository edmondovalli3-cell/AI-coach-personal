"""AI Coach — interfaccia verso Claude API.

Due modalità:
    - chat generale: con tutto lo stato di forma (sonno, recupero, attività)
    - chat per attività: focus su una singola sessione

Le risposte vengono streamate al frontend in tempo reale (SSE).
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from anthropic import Anthropic, APIError

from . import db

log = logging.getLogger("coach")

# Modello di default. Sonnet è il giusto compromesso qualità/costo per il coaching.
DEFAULT_MODEL = "claude-sonnet-4-6"
FAST_MODEL = "claude-haiku-4-5"

MAX_TOKENS = 2000

SYSTEM_GENERAL = """Sei il coach personale di Edmondo: un allenatore esperto e diretto, con solide basi di
fisiologia dello sport, nutrizione sportiva, fisiologia del sonno e gestione del carico di allenamento.

Edmondo ti ha collegato i suoi dati Garmin (Epix Pro Gen 2). Hai accesso a:
- attività degli ultimi giorni/mesi (pace, HR, splits, training effect, training load)
- sonno (punteggio, fasi, HRV notturno, respirazione)
- wellness giornaliero (Body Battery, stress, frequenza a riposo, passi)
- stato di allenamento (Training Readiness, Training Status, VO2max, Endurance/Hill score, race predictors)

Le tue risposte:
- sono brevi e dirette: niente preamboli, niente "ottima domanda", niente bullet point se non strettamente utili
- usano i NUMERI specifici dei suoi dati (passo, HR, % zone, Body Battery, ecc.) — mai vaghi
- parlano italiano scorrevole, tu informale
- se i dati non ci sono per rispondere, dillo, non inventare
- se serve un calcolo (volume settimanale, % zone, trend), fallo concretamente
- quando dai consigli su recupero/sonno/nutrizione, sii pratico (orari, grammi, quantità)
- ricorda sempre il contesto: chi è, gli obiettivi che menziona, le settimane recenti

Edmondo è un runner. Se non specifica obiettivo, assumi voglia migliorare in corsa (5k-half).
"""

SYSTEM_ACTIVITY = """Sei il coach personale di Edmondo, focalizzato ora su UNA SINGOLA attività che ha appena sincronizzato
dal suo Garmin Epix Pro Gen 2. Hai i dati completi: splits, HR per km, training effect, recupero post-attività.

Analizza questa specifica sessione. Quando rispondi:
- usa SEMPRE i numeri concreti di QUESTA attività (km, passo, HR, splits)
- confronta con sessioni simili recenti se rilevante (dati passati nel contesto)
- valuta il tipo di workout effettivo (recovery, easy, tempo, soglia, ripetute, progressivo, fartlek)
- dai feedback su esecuzione, qualità dell'allenamento, e cosa fare dopo
- italiano scorrevole, tu informale, niente preamboli o frasi di circostanza

Se l'utente chiede chiarimenti sulle metriche Garmin (PTE, TE, training load), spiegale concretamente sui SUOI dati.
"""


# ===========================================================================
# Client management
# ===========================================================================

_client: Anthropic | None = None
_api_key: str | None = None


def set_api_key(key: str) -> None:
    """Imposta la chiave API; reinizializza il client."""
    global _client, _api_key
    _api_key = key.strip()
    _client = Anthropic(api_key=_api_key) if _api_key else None


def has_api_key() -> bool:
    return _api_key is not None and _client is not None


def test_api_key() -> tuple[bool, str]:
    """Fa una chiamata minimale per verificare che la chiave funzioni."""
    if not _client:
        return False, "Chiave API non impostata"
    try:
        _client.messages.create(
            model=FAST_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": "ciao"}],
        )
        return True, "ok"
    except APIError as e:
        return False, f"Errore API: {e.message}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


# ===========================================================================
# Context building
# ===========================================================================

def _format_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}h{m:02d}" if h else f"{m}:{s:02d}"


def _format_pace(sec_per_km: float | None) -> str:
    if not sec_per_km:
        return "—"
    m, s = divmod(int(sec_per_km), 60)
    return f"{m}:{s:02d}/km"


def _format_activity_compact(a: dict) -> str:
    dist = (a.get("distance_m") or 0) / 1000
    return (
        f"  · {a.get('start_time','?')[:10]} {a.get('type','?')}: "
        f"{dist:.1f}km in {_format_duration(a.get('moving_sec'))} "
        f"@ {_format_pace(a.get('avg_pace_sec'))}"
        f"{', HR ' + str(int(a['avg_hr'])) if a.get('avg_hr') else ''}"
        f"{', D+ ' + str(int(a['elev_gain_m'])) + 'm' if a.get('elev_gain_m') else ''}"
        f"{', TE ' + str(round(a['aerobic_te'],1)) if a.get('aerobic_te') else ''}"
    )


def build_general_context() -> str:
    """Compila un blocco di contesto coi dati recenti dell'utente."""
    today = date.today()
    activities = db.list_activities(limit=30)
    sleep = db.recent_sleep(days=14)
    wellness = db.recent_wellness(days=14)
    training = db.recent_training(days=14)
    monthly = db.monthly_totals(months_back=4)

    lines: list[str] = []
    lines.append(f"# DATI DI EDMONDO — aggiornati al {today.isoformat()}\n")

    # Volume mensile
    if monthly:
        lines.append("## Volume per mese (km, ore, n. attività):")
        for m in monthly:
            lines.append(f"  · {m['month']}: {m.get('km') or 0:.1f}km · {m.get('hours') or 0:.1f}h · {m['n_activities']} att.")
        lines.append("")

    # Attività recenti
    if activities:
        lines.append("## Ultime attività:")
        for a in activities[:15]:
            lines.append(_format_activity_compact(a))
        lines.append("")

    # Training status oggi
    if training:
        t0 = training[0]
        lines.append("## Stato allenamento corrente:")
        if t0.get("training_readiness") is not None:
            lines.append(f"  · Training Readiness: {t0['training_readiness']}/100")
        if t0.get("training_status"):
            lines.append(f"  · Training Status: {t0['training_status']}")
        if t0.get("recovery_time_hours") is not None:
            lines.append(f"  · Recovery Time: {t0['recovery_time_hours']}h")
        if t0.get("vo2max_running"):
            lines.append(f"  · VO2max running: {t0['vo2max_running']}")
        if t0.get("endurance_score"):
            lines.append(f"  · Endurance score: {t0['endurance_score']}")
        if t0.get("hill_score"):
            lines.append(f"  · Hill score: {t0['hill_score']}")
        race = []
        if t0.get("race_predictor_5k"): race.append(f"5k {_format_duration(t0['race_predictor_5k'])}")
        if t0.get("race_predictor_10k"): race.append(f"10k {_format_duration(t0['race_predictor_10k'])}")
        if t0.get("race_predictor_half"): race.append(f"21k {_format_duration(t0['race_predictor_half'])}")
        if t0.get("race_predictor_full"): race.append(f"42k {_format_duration(t0['race_predictor_full'])}")
        if race:
            lines.append("  · Race predictor: " + " · ".join(race))
        lines.append("")

    # Sleep recente
    if sleep:
        lines.append("## Sonno ultimi 7 giorni:")
        for s in sleep[:7]:
            dur = _format_duration(s.get("duration_sec"))
            score = s.get("score") or "—"
            hrv = s.get("avg_hrv") or "—"
            lines.append(f"  · {s['date']}: durata {dur}, punteggio {score}, HRV notturno {hrv}ms")
        lines.append("")

    # Wellness recente
    if wellness:
        lines.append("## Wellness ultimi 7 giorni:")
        for w in wellness[:7]:
            bb_low = w.get("body_battery_low") or "—"
            bb_high = w.get("body_battery_high") or "—"
            stress = w.get("stress_avg") or "—"
            rhr = w.get("resting_hr") or "—"
            lines.append(f"  · {w['date']}: Body Battery {bb_low}→{bb_high}, stress medio {stress}, FC riposo {rhr}")
        lines.append("")

    return "\n".join(lines)


def build_activity_context(activity_id: str) -> str:
    a = db.get_activity(activity_id)
    if not a:
        return "Attività non trovata."

    lines = []
    lines.append(f"# ATTIVITÀ: {a.get('name') or a.get('type')}")
    lines.append(f"Data: {a.get('start_time')}")
    lines.append(f"Tipo: {a.get('type')}")
    dist = (a.get("distance_m") or 0) / 1000
    lines.append(f"Distanza: {dist:.2f} km")
    lines.append(f"Durata moving: {_format_duration(a.get('moving_sec'))}")
    lines.append(f"Durata totale: {_format_duration(a.get('duration_sec'))}")
    if a.get("avg_pace_sec"):
        lines.append(f"Passo medio: {_format_pace(a['avg_pace_sec'])}")
    if a.get("avg_hr"):
        lines.append(f"HR media/max: {int(a['avg_hr'])} / {int(a.get('max_hr') or 0)}")
    if a.get("elev_gain_m") or a.get("elev_loss_m"):
        lines.append(f"D+: {int(a.get('elev_gain_m') or 0)}m / D-: {int(a.get('elev_loss_m') or 0)}m")
    if a.get("avg_cadence"):
        lines.append(f"Cadenza: {int(a['avg_cadence'])} spm")
    if a.get("avg_power"):
        lines.append(f"Potenza: {int(a['avg_power'])}W medi / {int(a.get('max_power') or 0)}W max")
    if a.get("aerobic_te"):
        lines.append(f"Training Effect aerobico: {a['aerobic_te']:.1f}")
    if a.get("anaerobic_te"):
        lines.append(f"Training Effect anaerobico: {a['anaerobic_te']:.1f}")
    if a.get("training_load"):
        lines.append(f"Training Load: {int(a['training_load'])}")
    if a.get("calories"):
        lines.append(f"Calorie: {int(a['calories'])}")

    # Wellness intorno alla data
    try:
        day = a["start_time"][:10]
        wellness = db.recent_wellness(days=30)
        sleep = db.recent_sleep(days=30)
        training = db.recent_training(days=30)
        # Sonno della notte precedente
        prev_day = (date.fromisoformat(day) - timedelta(days=0)).isoformat()
        prev_sleep = next((s for s in sleep if s["date"] == prev_day), None)
        if prev_sleep:
            lines.append("")
            lines.append("## Sonno notte precedente:")
            lines.append(f"  · durata {_format_duration(prev_sleep.get('duration_sec'))}, punteggio {prev_sleep.get('score') or '—'}, HRV {prev_sleep.get('avg_hrv') or '—'}ms")
        # Training readiness del giorno
        tr_day = next((t for t in training if t["date"] == day), None)
        if tr_day:
            lines.append("")
            lines.append("## Stato del giorno:")
            if tr_day.get("training_readiness") is not None:
                lines.append(f"  · Training Readiness: {tr_day['training_readiness']}/100")
            if tr_day.get("training_status"):
                lines.append(f"  · Training Status: {tr_day['training_status']}")
    except Exception:  # noqa: BLE001
        pass

    # Attività simili recenti per confronto
    similar = [
        x for x in db.list_activities(limit=50)
        if x["id"] != a["id"] and x.get("type") == a.get("type")
    ][:5]
    if similar:
        lines.append("")
        lines.append("## Sessioni simili recenti (per confronto):")
        for s in similar:
            lines.append(_format_activity_compact(s))

    return "\n".join(lines)


# ===========================================================================
# Chat
# ===========================================================================

def _scope_for_activity(activity_id: str | None) -> str:
    return f"activity:{activity_id}" if activity_id else "general"


def _system_prompt(activity_id: str | None) -> str:
    if activity_id:
        return SYSTEM_ACTIVITY + "\n\n" + build_activity_context(activity_id)
    return SYSTEM_GENERAL + "\n\n" + build_general_context()


def _load_history(scope: str) -> list[dict]:
    """Carica la cronologia del chat scope in formato Anthropic."""
    msgs = db.list_chat(scope, limit=40)  # ultimi 40 (20 turni)
    return [{"role": m["role"], "content": m["content"]} for m in msgs]


def stream_reply(user_message: str, activity_id: str | None = None) -> Iterator[str]:
    """Stream della risposta del coach. Yield chunk testuali.

    Salva il messaggio utente + la risposta completa al termine.
    """
    if not _client:
        yield "[errore: la chiave API Claude non è configurata. Vai nelle Impostazioni.]"
        return

    scope = _scope_for_activity(activity_id)
    db.add_chat(scope, "user", user_message)

    history = _load_history(scope)
    # `history` include già il messaggio appena salvato

    full_reply = ""
    try:
        with _client.messages.stream(
            model=DEFAULT_MODEL,
            max_tokens=MAX_TOKENS,
            system=_system_prompt(activity_id),
            messages=history,
        ) as stream:
            for text in stream.text_stream:
                full_reply += text
                yield text
    except APIError as e:
        msg = f"\n\n[errore API Claude: {e.message}]"
        full_reply += msg
        yield msg
    except Exception as e:  # noqa: BLE001
        msg = f"\n\n[errore: {e}]"
        full_reply += msg
        yield msg
    finally:
        if full_reply.strip():
            db.add_chat(scope, "assistant", full_reply)


def get_history(activity_id: str | None = None) -> list[dict]:
    return db.list_chat(_scope_for_activity(activity_id))


def clear_history(activity_id: str | None = None) -> None:
    db.clear_chat(_scope_for_activity(activity_id))


# ===========================================================================
# Insight "una tantum" — riassunto rapido (per la dashboard)
# ===========================================================================

def quick_status_insight() -> str:
    """Una frase di sintesi sullo stato attuale per la home.
    Usa il modello veloce per risparmiare.
    """
    if not _client:
        return "Configura la chiave API Claude nelle Impostazioni per attivare l'AI coach."

    context = build_general_context()
    try:
        resp = _client.messages.create(
            model=FAST_MODEL,
            max_tokens=200,
            system="Sei il coach personale di Edmondo. In max 3 frasi sintetizza lo stato di forma attuale e il consiglio principale per OGGI. Usa numeri reali. Niente preamboli.",
            messages=[{"role": "user", "content": "Sintesi stato oggi."}],
            # pass context via system
        )
        return resp.content[0].text if resp.content else "—"
    except Exception as e:  # noqa: BLE001
        log.warning("quick_status_insight failed: %s", e)
        return ""
