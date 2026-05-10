from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from finance_bro.core import logging as logging_cfg
from finance_bro.core.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    logging_cfg.configure(level=settings.log_level)
    yield


app = FastAPI(title="finance-bro", lifespan=lifespan)
