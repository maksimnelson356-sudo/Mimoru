import asyncio
from contextlib import suppress

import structlog
from redis.asyncio import Redis
from sqlalchemy import text

from app.db.session import SessionFactory


class HealthServer:
    def __init__(self, redis: Redis, host: str = "0.0.0.0", port: int = 8080) -> None:
        self.redis = redis
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None
        self.ready = False

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        structlog.get_logger().info("health_server_started", host=self.host, port=self.port)

    def set_ready(self, value: bool) -> None:
        self.ready = value

    async def close(self) -> None:
        self.ready = False
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()

    async def _dependencies_ok(self) -> bool:
        try:
            # Run DB and Redis checks in parallel to reduce latency and improve readiness reporting.
            async with SessionFactory() as session:
                db_task = session.execute(text("SELECT 1"))
                redis_task = self.redis.ping()
                db_result, redis_ok = await asyncio.gather(db_task, redis_task, return_exceptions=True)
                if isinstance(db_result, Exception) or isinstance(redis_ok, Exception):
                    return False
                return bool(redis_ok)
        except Exception:
            return False

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status = "503 Service Unavailable"
        body = b"not ready\n"
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=2)
            path = request_line.decode("ascii", errors="ignore").split(" ")[1]
            if path == "/healthz":
                status, body = "200 OK", b"ok\n"
            elif path == "/readyz" and self.ready and await self._dependencies_ok():
                status, body = "200 OK", b"ready\n"
            elif path not in {"/healthz", "/readyz"}:
                status, body = "404 Not Found", b"not found\n"
        except Exception as error:
            structlog.get_logger().debug("health_request_invalid", error=str(error))
        headers = (
            f"HTTP/1.1 {status}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(headers + body)
        with suppress(Exception):
            await writer.drain()
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()
