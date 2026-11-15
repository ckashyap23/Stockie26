from __future__ import annotations

import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import job_manager, public, v1
from app.config import bootstrap_config, get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app() -> FastAPI:
    copied = bootstrap_config(get_settings())  # first start with CONFIG_DIR on the Volume: copy default config files
    if copied:
        logging.getLogger(__name__).info("Copied default config into CONFIG_DIR: %s", ", ".join(copied))
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        settings = get_settings()
        scheduler = None
        if settings.scheduler_enabled:
            from app.scheduler import Scheduler

            scheduler = Scheduler(settings, job_manager(application, settings))
            scheduler.start()
        yield
        if scheduler:
            scheduler.stop()

    app = FastAPI(
        lifespan=lifespan,
        title="NIFTY News Signal API",
        version="0.1.0",
        description="Standalone service: seeded macro events -> news -> impact -> one signed NIFTY signal.",
    )
    app.include_router(public)
    app.include_router(v1)
    return app


app = create_app()
