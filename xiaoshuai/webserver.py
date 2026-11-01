"""小帅 HTTP backend :8360(仅 127.0.0.1)。服务 WKWebView UI + 控制端点 + 状态。"""
from __future__ import annotations
import os
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_WEBUI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")


def make_server(port, store, control, base, token=None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _authed(self) -> bool:
            # token 未启用(测试)→ 放行; 启用 → 改动类端点须带匹配 header(防本机其他进程/注入
            # 直接 POST 绕过 verifier 批准/控制)。小帅终验 w7021g61k LOW 加固。
            if not token:
                return True
            return self.headers.get("X-XS-Token") == token

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/health":
                return self._send(200, json.dumps({"ok": True, "name": "小帅"}))
            if p == "/":
                idx = os.path.join(_WEBUI, "index.html")
                if os.path.exists(idx):
                    html = open(idx, "r", encoding="utf-8").read()
                    html = html.replace("__XS_TOKEN__", token or "")  # 把 token 注入页面供 fetch 用
                    return self._send(200, html, "text/html; charset=utf-8")
                return self._send(200, "<h1>小帅</h1>", "text/html; charset=utf-8")
            if p == "/api/reviews":
                return self._send(200, json.dumps(store.list_reviews(), ensure_ascii=False))
            if p == "/api/status":
                pend = len(store.list_reviews("pending")) + len(store.list_reviews("verifying"))
                esc = len(store.list_reviews("escalated"))
                return self._send(200, json.dumps({"pending": pend, "escalated": esc}))
            return self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            ln = int(self.headers.get("Content-Length", 0))
            try:
                data = json.loads(self.rfile.read(ln) or "{}")
            except Exception:
                data = {}
            p = self.path.split("?")[0]
            # 改动类端点(批准/否决/控制)须通过 token 校验, 防本机其他进程/注入绕 verifier
            if p in ("/api/approve", "/api/reject", "/api/control") and not self._authed():
                return self._send(403, json.dumps({"error": "forbidden"}))
            if p == "/api/approve":
                rid = data.get("id")
                store.record_decision(rid, "approve", "admin", data.get("note", ""))
                store.set_state(rid, "safe_activated")
                if control:
                    row = store.get(rid)
                    if row:
                        control.activate(row["commit_sha"])
                return self._send(200, json.dumps({"ok": True}))
            if p == "/api/reject":
                rid = data.get("id")
                store.record_decision(rid, "reject", "admin", data.get("note", ""))
                if control:
                    row = store.get(rid)
                    if row:
                        control.revert(row["commit_sha"])
                store.set_state(rid, "blocked_reverted")
                return self._send(200, json.dumps({"ok": True}))
            if p == "/api/control" and control:
                act = data.get("action")
                fn = {"pause": control.pause_evolve, "resume": control.resume_evolve,
                      "safeword": control.safe_word}.get(act)
                if fn:
                    fn()
                    return self._send(200, json.dumps({"ok": True}))
            return self._send(404, json.dumps({"error": "not found"}))

    return ThreadingHTTPServer(("127.0.0.1", port), H)
