"""Where the server runs: Railway, Render, or elsewhere (a hospital server, a laptop).

The app is the same everywhere; what differs by host is read here:

* the client's IP address (login limits, PDPA access log): only a header that the host's front proxy sets on
  every request, replacing whatever the caller sent, can be trusted;
* the site's public address (links in LINE messages) and the deployed version (status page);
* where an admin changes settings and reads logs (named in messages and on the status page);
* an old server after a move (``APP_MOVED_TO``): every page sends people to the new address.
"""
from __future__ import annotations

import html
import os
from urllib.parse import quote, urlsplit

RAILWAY, RENDER = "railway", "render"

# Railway's edge sets X-Real-IP and replaces a caller's copy; it does not set CF-Connecting-IP, so a caller's
# copy of that one would pass straight through. Render runs behind Cloudflare, which always overwrites
# CF-Connecting-IP; the first X-Forwarded-For entry is whatever the client sent (Render only appends).
IP_HEADERS = {RAILWAY: ("x-real-ip",), RENDER: ("cf-connecting-ip", "x-forwarded-for")}
DEFAULT_IP_HEADERS = ("cf-connecting-ip", "x-forwarded-for")  # elsewhere: set CLIENT_IP_HEADER (or "none")

NAMES = {RAILWAY: "Railway", RENDER: "Render"}
SETTINGS = {RAILWAY: "Railway → บริการ ORchestraAI → Variables", RENDER: "Render → Environment"}
LOGS = {RAILWAY: "Railway → Deployments → View logs", RENDER: "Render → Logs"}
# How a setting is added there (Railway stages variable changes until they are deployed).
HOW = {RAILWAY: "กด New Variable ใส่ชื่อและค่า แล้วกด Deploy ที่แถบการเปลี่ยนแปลงด้านบน (ใช้ค่าใหม่หลัง deploy เสร็จ ประมาณ 2–3 นาที)",
       RENDER: "กด Add Environment Variable ใส่ Key และ Value แล้วกด Save (Render deploy ใหม่เอง ประมาณ 2–3 นาที)"}


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def provider() -> str:
    """"railway", "render" or "" (each sets its own variables in every deployment)."""
    if _env("RAILWAY_PROJECT_ID") or _env("RAILWAY_ENVIRONMENT_ID") or _env("RAILWAY_SERVICE_ID"):
        return RAILWAY
    if _env("RENDER") or _env("RENDER_SERVICE_ID"):
        return RENDER
    return ""


def name() -> str:
    return NAMES.get(provider(), "เซิร์ฟเวอร์")


def settings_place() -> str:
    """Where an admin adds or changes a setting (environment variable)."""
    return SETTINGS.get(provider(), "ค่าตั้งของเซิร์ฟเวอร์ (environment variables)")


def logs_place() -> str:
    return LOGS.get(provider(), "log ของเซิร์ฟเวอร์")


def ip_headers() -> tuple[str, ...]:
    custom = _env("CLIENT_IP_HEADER").lower()
    if custom:  # a hospital's own reverse proxy (e.g. nginx sets X-Real-IP); "none" = the TCP peer only
        return () if custom == "none" else (custom,)
    return IP_HEADERS.get(provider(), DEFAULT_IP_HEADERS)


def client_ip(headers, peer: str | None) -> str:
    """The caller's IP address from the trusted header (else the connection's peer)."""
    for header in ip_headers():
        value = (headers.get(header) or "").split(",")[0].strip()
        if value:
            return value[:64]
    return peer or "unknown"


def public_url() -> str:
    """The site's address for links sent outside the web page (LINE messages)."""
    url = _env("APP_URL") or _env("RENDER_EXTERNAL_URL")
    if not url and _env("RAILWAY_PUBLIC_DOMAIN"):
        url = "https://" + _env("RAILWAY_PUBLIC_DOMAIN")
    return url.rstrip("/")


def commit() -> str:
    return _env("RAILWAY_GIT_COMMIT_SHA") or _env("RENDER_GIT_COMMIT")


def region() -> str:
    return _env("RAILWAY_REPLICA_REGION")


def info() -> dict:
    """For the status and security pages."""
    return {"provider": provider() or None, "name": name(), "settings": settings_place(), "logs": logs_place(),
            "how": HOW.get(provider(), "เพิ่มค่า แล้วเริ่มเซิร์ฟเวอร์ใหม่"), "region": region() or None}


# ------------------------------------------------------------------ an old server after a move

def moved_to() -> str:
    """The new address from APP_MOVED_TO (https only, no path), or "" when this server is the live one."""
    raw = _env("APP_MOVED_TO")
    if not raw:
        return ""
    try:
        parts = urlsplit(raw if "://" in raw else "https://" + raw)
        parts.port  # noqa: B018 (raises on a malformed port, e.g. "javascript:alert(1)")
    except ValueError:
        return ""
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return ""
    return f"https://{parts.netloc}"


def moved_path(path: str) -> bool:
    """Requests the old server still answers itself: the host's health check, and the notification worker
    and its icons (phones that turned notifications on at the old address keep showing them)."""
    return path == "/api/health" or path == "/sw.js" or path.startswith("/icons/")


def moved_page(target: str, path: str) -> str:
    """The page every other address of the old server shows: where the system is now, then go there."""
    link = html.escape(target + quote(path if path.startswith("/") else "/", safe="/-._~"), quote=True)
    site = html.escape(target, quote=True)
    return f"""<!doctype html>
<html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="20; url={link}">
<title>ORchestraAI ย้ายที่อยู่แล้ว</title>
<style>
  body {{ margin: 0; background: #f5f7fb; color: #1d2433; font: 16px/1.6 Sarabun, system-ui, sans-serif; }}
  main {{ max-width: 560px; margin: 0 auto; padding: 40px 16px; }}
  h1 {{ font-size: 24px; margin: 0 0 12px; }}
  .box {{ background: #fff; border: 1px solid #dfe5ef; border-radius: 12px; padding: 20px; }}
  .url {{ font-weight: 600; word-break: break-all; }}
  a.go {{ display: inline-block; margin: 8px 0 4px; padding: 12px 20px; border-radius: 10px; background: #1f4fd1;
          color: #fff; font-weight: 600; text-decoration: none; }}
  li {{ margin: 6px 0; }} .hint {{ color: #5b6475; font-size: 14px; }}
</style></head>
<body><main>
  <h1>ORchestraAI ย้ายไปที่อยู่ใหม่แล้ว</h1>
  <div class="box">
    <p>ที่อยู่ใหม่: <span class="url">{site}</span></p>
    <a class="go" id="go" href="{link}">ไปที่ระบบใหม่</a>
    <p class="hint" id="wait">กำลังพาไปที่ระบบใหม่…</p>
    <ul>
      <li>เข้าสู่ระบบใหม่อีกครั้งด้วยชื่อผู้ใช้และรหัสผ่านเดิม (ข้อมูลทั้งหมดอยู่ครบ)</li>
      <li>บันทึกที่อยู่ใหม่ไว้ในรายการโปรด แทนที่อยู่เดิม</li>
      <li>ถ้าติดตั้งแอปไว้บนมือถือ: ลบไอคอนเดิม แล้วติดตั้งใหม่จากที่อยู่ใหม่</li>
      <li>การแจ้งเตือนในเบราว์เซอร์ของที่อยู่เดิมถูกปิดแล้ว เปิดใหม่ได้ที่ระบบใหม่ เมนู 🔔 แจ้งเตือน (LINE ใช้ได้เหมือนเดิม)</li>
    </ul>
  </div>
</main>
<script>
(async () => {{
  // The old address's notification worker: stop it, so a phone does not get every notice twice once
  // notifications are turned on at the new address (the server drops the old subscription on its next send).
  try {{
    if ('serviceWorker' in navigator) {{
      for (const reg of await navigator.serviceWorker.getRegistrations()) {{
        const sub = reg.pushManager && await reg.pushManager.getSubscription().catch(() => null);
        if (sub) await sub.unsubscribe().catch(() => {{}});
        await reg.unregister().catch(() => {{}});
      }}
    }}
  }} catch (e) {{}}
  setTimeout(() => {{ location.replace(document.getElementById('go').href); }}, 6000);
}})();
</script>
</body></html>"""
