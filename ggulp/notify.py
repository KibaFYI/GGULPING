"""Discord pings (optional): done, or something went down the wrong way."""

from __future__ import annotations

import datetime as _dt

from forge.notify import valid_webhook

PURPLE, PINK = 0xB388FF, 0xFF5CCB


class Discord:
    def __init__(self, url: str | None, name: str = ""):
        self.url = url.strip() if valid_webhook(url) else None
        self.name = name

    def send(self, title: str, description: str = "", ok: bool = True) -> None:
        if not self.url:
            return
        import requests

        embed = {
            "title": title[:256],
            "description": description[:4000],
            "color": PURPLE if ok else PINK,
            "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "footer": {"text": f"GGULPING{' · ' + self.name if self.name else ''}"},
        }
        try:
            requests.post(self.url, json={"username": "GGULPING", "embeds": [embed]}, timeout=20)
        except Exception:  # noqa: BLE001 - a ping must never break the meal
            pass
