"""Transport helpers shared by generated routes; no handwritten DTOs."""

import json
from decimal import Decimal

from fastapi import Request
from fastapi.routing import APIRoute


def reject_constant(value):
    raise ValueError("Non-finite numbers are not valid JSON")


class DecimalRequest(Request):
    async def json(self):
        if not hasattr(self, "_json"):
            try:
                self._json = json.loads(
                    await self.body(), parse_float=Decimal, parse_constant=reject_constant
                )
            except json.JSONDecodeError:
                raise
            except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                raise json.JSONDecodeError("Invalid JSON encoding or number", "", 0) from exc
        return self._json


class ContractRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def route(request: Request):
            return await handler(DecimalRequest(request.scope, request.receive))

        return route
