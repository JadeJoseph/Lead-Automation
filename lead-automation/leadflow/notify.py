"""Step 5: notify on Hot leads.

Always writes out/notifications.jsonl (so there is a local record and the demo works with no
credentials). Also posts to Slack and/or Telegram when configured. A failing channel is reported
back to the caller (and logged to the review queue); it never blocks or loses the lead.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .store import now_iso


def slack_escape(text: str) -> str:
    """Escape Slack control characters so a lead cannot inject <!channel> or links into our post."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Notifier:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log_path = cfg.out_dir / "notifications.jsonl"

    def format(self, row: dict, note: str = "") -> str:
        contact = row.get("email") or row.get("phone") or "no contact"
        head = f"HOT LEAD: {row['name']} ({row['company'] or 'no company'})"
        lines = [head, f"Contact: {contact}", f"Why: {row['tag_reason']}", f"Sheet row: {row['lead_id']}"]
        if note:
            lines.append(note)
        return "\n".join(lines)

    def notify_hot(self, row: dict, note: str = "") -> list[tuple[str, bool, str]]:
        text = self.format(row, note)
        results = []
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": now_iso(), "lead_id": row["lead_id"], "text": text}, ensure_ascii=False) + "\n")
            results.append(("file", True, ""))
        except OSError as exc:
            results.append(("file", False, str(exc)))
        if self.cfg.slack_webhook:
            results.append(("slack",) + self._retry(lambda: self._slack(text)))
        if self.cfg.telegram_token and self.cfg.telegram_chat_id:
            results.append(("telegram",) + self._retry(lambda: self._telegram(text)))
        return results

    # ------------------------------------------------------------------
    def _slack(self, text: str) -> None:
        self._post(self.cfg.slack_webhook, {"text": slack_escape(text)})

    def _telegram(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage"
        self._post(url, {"chat_id": self.cfg.telegram_chat_id, "text": text})

    @staticmethod
    def _post(url: str, body: dict) -> None:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()

    @staticmethod
    def _retry(fn, attempts: int = 3) -> tuple[bool, str]:
        err = ""
        for i in range(attempts):
            try:
                fn()
                return True, ""
            except urllib.error.HTTPError as exc:
                # redact: the URL (which may hold a token) is not in the message
                err = f"HTTP {exc.code}"
                if exc.code < 500 and exc.code != 429:
                    break
            except Exception as exc:  # noqa: BLE001
                err = type(exc).__name__
            time.sleep(0.5 * (i + 1))
        return False, err
