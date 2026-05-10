from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from finance_bro.core import logging as logging_cfg
from finance_bro.core.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging_cfg.configure(level=settings.log_level)
    yield


app = FastAPI(title="finance-bro", lifespan=lifespan)
