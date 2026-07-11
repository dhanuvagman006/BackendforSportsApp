"""SportyQo API entrypoint.

Run (dev):  uvicorn app.main:app --reload
Docs:       /docs (Swagger) · /redoc
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.errors import install_error_handlers
from app.db.base import SessionLocal
from app.services.scoring import seed_tiers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # storage dirs + card-tier seed (config data, safe to run on every boot)
    Path(settings.storage_dir, "public").mkdir(parents=True, exist_ok=True)
    Path(settings.storage_dir, "private").mkdir(parents=True, exist_ok=True)
    async with SessionLocal() as db:
        await seed_tiers(db)
        await db.commit()
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.api_version,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=settings.cors_origins != "*",
    allow_methods=["*"],
    allow_headers=["*"],
)

install_error_handlers(app)
app.include_router(api_router, prefix="/v1")

# public media (local storage provider); production serves from S3+CDN instead
Path(settings.storage_dir, "public").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(Path(settings.storage_dir) / "public")), name="static")


@app.get("/health", tags=["system"])
async def health():
    return {"status": "ok", "version": settings.api_version}
