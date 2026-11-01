"""小帅通知:飞书 + macOS 原生通知 + badge 状态文件(供菜单栏 app 读)。"""
from __future__ import annotations
import os
import json
import subprocess
from typing import Optional


class Notifier:
    def __init__(self, base: Optional[str] = None, feishu_script: Optional[str] = "auto",
                 native: bool = True):
        self.base = (base or os.environ.get("XIAOSHUAI_DIR")
                     or os.path.expanduser("~/.jarvis/xiaoshuai"))
        os.makedirs(self.base, exist_ok=True)
        self.badge_path = os.path.join(self.base, "badge.json")
        self.native = native
        if feishu_script == "auto":
            feishu_script = os.path.expanduser("~/.claude/scripts/feishu-notify.sh")
            if not os.path.exists(feishu_script):
                feishu_script = None
        self.feishu_script = feishu_script

    def set_badge(self, pending: int = 0, alerts: int = 0) -> None:
        with open(self.badge_path, "w", encoding="utf-8") as f:
            json.dump({"pending": pending, "alerts": alerts}, f)

    def _native(self, title: str, msg: str) -> None:
        if not self.native:
            return
        try:
            subprocess.run(["osascript", "-e",
                            f'display notification "{msg[:200]}" with title "小帅 · {title[:80]}"'],
                           timeout=10)
        except Exception:
            pass

    def _feishu(self, msg: str) -> None:
        if not self.feishu_script:
            return
        try:
            subprocess.run([self.feishu_script, msg[:500]], timeout=15)
        except Exception:
            pass

    def alert(self, level: str, title: str, msg: str) -> None:
        self._native(title, msg)
        self._feishu(f"[小帅·{level}] {title}: {msg}")

    def fyi(self, title: str, msg: str) -> None:
        self._native(title, msg)
