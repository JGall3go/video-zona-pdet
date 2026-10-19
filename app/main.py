"""
Mini servidor de videos para Coolify.

Rutas:
  GET  /                 -> lista de videos + formulario de subida
  POST /upload           -> sube un video (campo "file"; token en campo "token" o header X-Upload-Token)
  GET  /ver/{video_id}   -> página con reproductor
  GET  /video/{video_id} -> archivo crudo (soporta Range, permite adelantar/retroceder)
  GET  /health           -> healthcheck
"""

import html
import os
import re
import secrets
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

VIDEO_DIR = Path(os.getenv("VIDEO_DIR", "/data/videos"))
UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "")  # si está vacío, cualquiera puede subir
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "2048"))
FECHA_FIJA = os.getenv("FECHA_FIJA", "2026-09-24 22:45")

ALLOWED = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".ogv": "video/ogg",
}
ID_RE = re.compile(r"^[a-z0-9-]{1,80}$")

VIDEO_DIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="Video Host")


# ---------- utilidades ----------

def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:50] or "video"


def find_video(video_id: str) -> Path:
    if not ID_RE.match(video_id):
        raise HTTPException(404, "Video no encontrado")
    for ext in ALLOWED:
        p = VIDEO_DIR / f"{video_id}{ext}"
        if p.is_file():
            return p
    raise HTTPException(404, "Video no encontrado")


def list_videos():
    items = [p for p in VIDEO_DIR.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED]
    items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return items


def check_token(token: str | None):
    if UPLOAD_TOKEN and not secrets.compare_digest(token or "", UPLOAD_TOKEN):
        raise HTTPException(401, "Token inválido")


def save_stream(src, dest: Path, max_bytes: int) -> int:
    written = 0
    with dest.open("wb") as out:
        while chunk := src.read(1024 * 1024):
            written += len(chunk)
            if written > max_bytes:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"El archivo supera {MAX_UPLOAD_MB} MB")
            out.write(chunk)
    return written


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  body{{font-family:system-ui,sans-serif;max-width:900px;margin:0 auto;padding:16px;background:#111;color:#eee}}
  a{{color:#7cc4ff}} video{{width:100%;max-height:75vh;background:#000;border-radius:8px}}
  form,li{{background:#1c1c1c;padding:12px;border-radius:8px;margin:8px 0}}
  ul{{list-style:none;padding:0}} input,button{{font-size:1rem;margin:4px 0}}
  small{{color:#999}} code{{background:#222;padding:2px 6px;border-radius:4px}}
</style></head><body>{body}</body></html>""")


# ---------- rutas ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index():
    rows = []
    for p in list_videos():
        vid = p.stem
        mb = p.stat().st_size / 1_048_576
        date = FECHA_FIJA
        rows.append(f'<li><a href="/ver/{vid}">{html.escape(vid)}</a> '
                    f'<small>· {mb:.1f} MB · {date}</small></li>')
    token_field = '<input type="password" name="token" placeholder="Token de subida" required><br>' if UPLOAD_TOKEN else ""
    body = f"""
<h1>🎬 Videos</h1>
<form action="/upload" method="post" enctype="multipart/form-data">
  <strong>Subir video</strong> <small>({", ".join(ALLOWED)} · máx {MAX_UPLOAD_MB} MB)</small><br>
  <input type="file" name="file" accept="video/*" required><br>
  {token_field}
  <input type="hidden" name="from_form" value="1">
  <button type="submit">Subir</button>
</form>
<ul>{"".join(rows) or "<li><small>Aún no hay videos.</small></li>"}</ul>"""
    return page("Videos", body)


@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    token: str | None = Form(None),
    from_form: str | None = Form(None),
    x_upload_token: str | None = Header(None),
):
    check_token(token or x_upload_token)

    original = file.filename or "video.mp4"
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Formato no permitido. Usa: {', '.join(ALLOWED)}")

    video_id = f"{slugify(Path(original).stem)}-{secrets.token_hex(3)}"
    dest = VIDEO_DIR / f"{video_id}{ext}"
    tmp = dest.with_suffix(dest.suffix + ".part")

    await run_in_threadpool(save_stream, file.file, tmp, MAX_UPLOAD_MB * 1_048_576)
    tmp.rename(dest)

    # Desde formulario HTML -> redirige al reproductor; desde API -> JSON
    if from_form:
        return RedirectResponse(f"/ver/{video_id}", status_code=303)
    return JSONResponse({"id": video_id, "ver": f"/ver/{video_id}", "archivo": f"/video/{video_id}"}, status_code=201)


@app.get("/ver/{video_id}", response_class=HTMLResponse)
def ver(video_id: str):
    p = find_video(video_id)
    vid = html.escape(video_id)
    body = f"""
<p><a href="/">← Todos los videos</a></p>
<h2>{vid}</h2>
<video controls preload="metadata" playsinline>
  <source src="/video/{vid}" type="{ALLOWED[p.suffix.lower()]}">
  Tu navegador no soporta video HTML5.
</video>
<p><small>Enlace directo: <a href="/video/{vid}">/video/{vid}</a></small></p>"""
    return page(video_id, body)


@app.get("/video/{video_id}")
def video(video_id: str):
    p = find_video(video_id)
    # FileResponse de Starlette soporta peticiones Range (206) -> se puede adelantar el video
    return FileResponse(p, media_type=ALLOWED[p.suffix.lower()])
