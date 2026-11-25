import json

import httpx
import structlog

from app.common.config import Settings


class ClickHouse:
    def __init__(self, config: Settings):
        self.client = httpx.AsyncClient(
            base_url=config.clickhouse_url,
            auth=(config.clickhouse_user, config.clickhouse_password),
            timeout=120,
        )

    async def execute(self, sql: str, params=None):
        response = await self.client.post("/", content=sql, params=params)
        if response.is_error:
            structlog.get_logger().error(
                "clickhouse_query_failed", status=response.status_code, error=response.text[:1500]
            )
        response.raise_for_status()
        return response.text

    async def query(self, sql: str, params=None):
        text = await self.execute(sql + " FORMAT JSONEachRow", params)
        return [json.loads(line) for line in text.splitlines() if line]

    async def insert(self, table: str, rows: list[dict]):
        if not rows:
            return
        data = "\n".join(json.dumps(row, default=str, allow_nan=False) for row in rows)
        await self.execute(f"INSERT INTO {table} FORMAT JSONEachRow\n{data}")

    async def close(self):
        await self.client.aclose()
