"""Application composition root. HTTP routes and DTOs are generated from OpenAPI."""

from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from marketplace_api.apis import auth_api, health_api, orders_api, products_api, promo_codes_api
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException

from app.auth.service import bootstrap_admin
from app.core.database import SessionFactory, engine
from app.core.errors import (
    APIError,
    api_error_handler,
    http_error_handler,
    integrity_handler,
    validation_handler,
)
from app.core.logging import RequestLoggingMiddleware, configure_logging


@asynccontextmanager
async def lifespan(app):
    async with SessionFactory() as session:
        async with session.begin():
            await session.execute(text("SELECT 1"))
        await bootstrap_admin(session)
    yield
    await engine.dispose()


def create_app():
    configure_logging()
    app = FastAPI(title="Marketplace API", version="1.0.0", lifespan=lifespan)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_exception_handler(APIError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(IntegrityError, integrity_handler)
    for module in (health_api, auth_api, products_api, orders_api, promo_codes_api):
        app.include_router(module.router)
    # Serve the authored contract in Swagger UI, including its roles and exact errors.
    contract = yaml.safe_load(
        (Path(__file__).parent.parent / "openapi/marketplace.yaml").read_text()
    )
    app.openapi = lambda: contract
    return app


app = create_app()


def main():
    import os

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
