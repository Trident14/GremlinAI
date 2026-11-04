"""
GremlinAI — FastAPI application entry point.
"""
from __future__ import annotations
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from gremlinai.api.routes import router
from gremlinai.db import db
from gremlinai.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="GremlinAI", version="0.1.0")

# Mount static web files
_WEB_DIR = Path(__file__).parent / "web"
app.mount("/static", StaticFiles(directory=str(_WEB_DIR)), name="static")
app.mount("/demo", StaticFiles(directory=str(_WEB_DIR / "demo"), html=True), name="demo")  # built-in test site

# Mount runs directory for artifacts, screenshots & reports
_RUNS_DIR = Path("runs")
_RUNS_DIR.mkdir(exist_ok=True)
app.mount("/runs", StaticFiles(directory=str(_RUNS_DIR)), name="runs")

# Include all API routes
app.include_router(router)


@app.on_event("startup")
async def startup():
    Path("runs").mkdir(exist_ok=True)
    await db.init()
    logger.info(
        "GremlinAI started — provider=%s | fast=%s | vision=%s",
        settings.llm_provider,
        settings.gemini_model_fast if settings.llm_provider == "gemini"
            else (settings.meta_model_fast if settings.llm_provider == "meta" else "mock"),
        settings.gemini_model_vision if settings.llm_provider == "gemini"
            else (settings.meta_model_vision if settings.llm_provider == "meta" else "mock"),
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    return (_WEB_DIR / "index.html").read_text(encoding="utf-8")
