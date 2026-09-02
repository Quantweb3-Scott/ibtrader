from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx

from .config import AlertConfig
from .db import Database

logger = logging.getLogger(__name__)


class AlertManager:
    def __init__(self, config: AlertConfig, db: Database):
        self.config = config
        self.db = db
        self._sent: dict[str, datetime] = {}

    async def send(
        self, key: str, severity: str, message: str, payload: dict | None = None
    ) -> bool:
        now = datetime.now(UTC)
        previous = self._sent.get(key)
        if previous and now - previous < timedelta(seconds=self.config.cooldown_seconds):
            return False
        self._sent[key] = now
        self.db.event(severity, key, message, payload)
        logger.log(
            logging.CRITICAL if severity == "CRITICAL" else logging.WARNING, "%s: %s", key, message
        )
        if self.config.webhook_url:
            headers = {}
            if self.config.webhook_bearer_token:
                headers["Authorization"] = f"Bearer {self.config.webhook_bearer_token}"
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(
                        self.config.webhook_url,
                        headers=headers,
                        json={
                            "event": key,
                            "severity": severity,
                            "message": message,
                            "timestamp": now.isoformat(),
                            "payload": payload or {},
                        },
                    )
                    response.raise_for_status()
            except Exception as exc:
                logger.exception("alert webhook failed")
                self.db.event("ERROR", "alert_delivery_failed", str(exc), {"original_event": key})
        return True
