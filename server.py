"""FastAPI server del Coach Personale.

Endpoint:
    GET  /                          → frontend HTML
    GET  /api/config                → stato configurazione (chiave API, sessione Garmin)
    POST /api/config/api_key        → salva chiave API Claude
    POST /api/auth/garmin/start     → avvia login Garmin (con MFA opzionale)
    POST /api/auth/garmin/mfa       → invia codice MFA
    GET  /api/auth/garmin/status    → polling stato login
    POST /api/auth/garmin/logout    → cancella sessione Garmin
    POST /api/sync                  → triggera sync recente
    GET  /api/sync/status           → ultima sync
    GET  /api/activities            → lista attività
    GET  /api/activities/{id}       → dettaglio attività
    DELETE /api/activities/{id}     → cancella
    GET  /api/wellness              → ultimi giorni wellness/sleep/training
    GET  /api/monthly               → totali mensili
    POST /api/chat                  → streaming risposta coach
    GET  /api/chat/history          → storia chat
    DELETE /api/chat                → cancella chat
    POST /api/import/zip            → import archivio Garmin
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import coach, db, garmin_sync, import_zip

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("server")

APP_DIR = Path(__file__).parent
# Config persistente: env var DATA_DIR ha precedenza (per cloud deploy)
_DATA_DIR = os.environ.get("DATA_DIR") or str(APP_DIR / "data")
CONFIG_PATH = Path(_DATA_DIR) / "config.json"

app = FastAPI(title="Coach Personale di Edmondo")


# ===========================================================================
# Config persistence
# ===========================================================================

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception as e:  # noqa: BLE001
        log.warning("config corrotto: %s", e)
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    try:
        CONFIG_PATH.chmod(0o600)
    except Exception:  # noqa: BLE001
        pass


# ===========================================================================
# Startup
# ===========================================================================

@app.on_event("startup")
async def on_startup() -> None:
    log.info("=" * 60)
    log.info("AVVIO COACH PERSONALE")
    log.info("=" * 60)
    try:
        # Crea data dir se non esiste
        Path(_DATA_DIR).mkdir(parents=True, exist_ok=True)
        db.init()
        log.info("✓ Database OK (%s)", db.DB_PATH)
    except Exception as e:  # noqa: BLE001
        log.error("✗ Errore init DB: %s", e)

    try:
        # Priorità: env var > config file
        env_key = os.environ.get("ANTHROPIC_API_KEY")
        cfg = load_config()
        if env_key:
            coach.set_api_key(env_key)
            log.info("✓ Chiave API Claude caricata da env var")
        elif cfg.get("anthropic_api_key"):
            coach.set_api_key(cfg["anthropic_api_key"])
            log.info("✓ Chiave API Claude caricata da config")
        else:
            log.info("ℹ  Nessuna chiave API ancora configurata")
    except Exception as e:  # noqa: BLE001
        log.error("✗ Errore caricamento config: %s", e)

    try:
        if garmin_sync.has_saved_session():
            if garmin_sync.ensure_authenticated():
                log.info("✓ Sessione Garmin ripristinata: %s", garmin_sync.whoami())
            else:
                log.info("ℹ  Sessione Garmin presente ma non valida — login richiesto")
        else:
            log.info("ℹ  Nessuna sessione Garmin — login richiesto")
    except Exception as e:  # noqa: BLE001
        log.error("✗ Errore caricamento sessione Garmin: %s", e)

    log.info("=" * 60)
    log.info("Server pronto su http://127.0.0.1:8765")
    log.info("=" * 60)


# ===========================================================================
# Frontend
# ===========================================================================

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((APP_DIR / "frontend.html").read_text())


# ===========================================================================
# Config & auth
# ===========================================================================

class ApiKeyBody(BaseModel):
    api_key: str


@app.get("/api/config")
async def get_config_status() -> dict:
    cfg = load_config()
    return {
        "has_api_key": bool(cfg.get("anthropic_api_key")),
        "has_garmin_session": garmin_sync.ensure_authenticated(),
        "garmin_user": garmin_sync.whoami(),
        "last_activity_sync": db.get_meta("last_activity_sync"),
        "last_daily_sync": db.get_meta("last_daily_sync"),
        "db_stats": db.db_stats(),
    }


@app.post("/api/config/api_key")
async def set_api_key(body: ApiKeyBody) -> dict:
    key = body.api_key.strip()
    if not key.startswith("sk-ant-"):
        raise HTTPException(400, "La chiave deve iniziare con 'sk-ant-'.")
    coach.set_api_key(key)
    ok, msg = await asyncio.to_thread(coach.test_api_key)
    if not ok:
        raise HTTPException(400, f"Chiave non valida: {msg}")
    cfg = load_config()
    cfg["anthropic_api_key"] = key
    save_config(cfg)
    return {"ok": True}


class GarminLoginBody(BaseModel):
    email: str
    password: str


@app.post("/api/auth/garmin/start")
async def garmin_start(body: GarminLoginBody) -> dict:
    session = garmin_sync.get_login_session()
    session.start(body.email, body.password)
    # Aspetta un attimo per vedere se serve subito MFA o se finisce subito
    await asyncio.sleep(2)
    return session.status()


class MfaBody(BaseModel):
    code: str


@app.post("/api/auth/garmin/mfa")
async def garmin_mfa(body: MfaBody) -> dict:
    session = garmin_sync.get_login_session()
    session.submit_mfa(body.code)
    await asyncio.sleep(2)
    return session.status()


@app.get("/api/auth/garmin/status")
async def garmin_status() -> dict:
    session = garmin_sync.get_login_session()
    return session.status()


@app.post("/api/auth/garmin/logout")
async def garmin_logout() -> dict:
    garmin_sync.logout()
    return {"ok": True}


# ===========================================================================
# Sync
# ===========================================================================

class SyncBody(BaseModel):
    activities: int = 50
    days: int = 14


@app.post("/api/sync")
async def trigger_sync(body: SyncBody) -> dict:
    if not garmin_sync.ensure_authenticated():
        raise HTTPException(401, "Sessione Garmin scaduta. Rifai il login.")
    result = await asyncio.to_thread(garmin_sync.full_sync, body.activities, body.days)
    return result


# ===========================================================================
# Activities
# ===========================================================================

@app.get("/api/activities")
async def get_activities(limit: int = 200, offset: int = 0, since: Optional[str] = None) -> dict:
    activities = db.list_activities(limit=limit, offset=offset, since=since)
    return {"activities": activities}


@app.get("/api/activities/{activity_id}")
async def get_activity(activity_id: str) -> dict:
    a = db.get_activity(activity_id)
    if not a:
        raise HTTPException(404, "Attività non trovata")
    return a


@app.delete("/api/activities/{activity_id}")
async def delete_activity(activity_id: str) -> dict:
    db.delete_activity(activity_id)
    return {"ok": True}


# ===========================================================================
# Wellness / Monthly
# ===========================================================================

@app.get("/api/wellness")
async def get_wellness(days: int = 14) -> dict:
    return {
        "wellness": db.recent_wellness(days=days),
        "sleep": db.recent_sleep(days=days),
        "training": db.recent_training(days=days),
    }


@app.get("/api/monthly")
async def get_monthly(months: int = 12) -> dict:
    return {"months": db.monthly_totals(months_back=months)}


# ===========================================================================
# Chat
# ===========================================================================

class ChatBody(BaseModel):
    message: str
    activity_id: Optional[str] = None


@app.post("/api/chat")
async def chat(body: ChatBody) -> StreamingResponse:
    if not coach.has_api_key():
        raise HTTPException(400, "Chiave API Claude non configurata.")

    def gen():
        for chunk in coach.stream_reply(body.message, body.activity_id):
            yield chunk

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8")


@app.get("/api/chat/history")
async def chat_history(activity_id: Optional[str] = None) -> dict:
    return {"messages": coach.get_history(activity_id)}


@app.delete("/api/chat")
async def chat_clear(activity_id: Optional[str] = None) -> dict:
    coach.clear_history(activity_id)
    return {"ok": True}


# ===========================================================================
# Import
# ===========================================================================

@app.post("/api/import/zip")
async def import_garmin_zip(file: UploadFile = File(...)) -> dict:
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(400, "Carica un file .zip esportato da Garmin.")
    # Salva temporaneamente
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    try:
        shutil.copyfileobj(file.file, tmp)
        tmp.close()
        result = await asyncio.to_thread(import_zip.import_garmin_export, tmp.name)
        return result
    finally:
        Path(tmp.name).unlink(missing_ok=True)
