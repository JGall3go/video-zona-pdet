"""
Mini servidor de videos para Coolify.

Rutas:
  GET  /                              -> lista de videos + formulario de subida
  POST /subida/iniciar                -> inicia una subida por partes (JSON: nombre, tamano)
  PUT  /subida/{upload_id}?offset=N   -> envía una parte (cuerpo binario)
  POST /subida/{upload_id}/finalizar  -> termina la subida y devuelve el id del video
  POST /upload                        -> subida en una sola petición (para curl/scripts)
  GET  /ver/{video_id}                -> página con reproductor
  GET  /video/{video_id}              -> archivo crudo (soporta Range, permite adelantar/retroceder)
  GET  /health                        -> healthcheck

El formulario web sube el video en partes de pocos MB. Así ninguna petición
dura más de unos segundos y no la corta el timeout del proxy (Coolify,
Cloudflare, etc.), y si una parte falla se reintenta sola.
"""

import html
import json
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

VIDEO_DIR = Path(os.getenv("VIDEO_DIR", "/data/videos"))
UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "")  # si está vacío, cualquiera puede subir
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "2048"))
CHUNK_MB = int(os.getenv("CHUNK_MB", "5"))  # tamaño de cada parte que envía el navegador
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
UPLOAD_ID_RE = re.compile(r"^[a-f0-9]{32}$")

MAX_BYTES = MAX_UPLOAD_MB * 1_048_576
PARTS_DIR = VIDEO_DIR / ".partes"  # subidas en curso (se guardan en el mismo volumen)

VIDEO_DIR.mkdir(parents=True, exist_ok=True)
PARTS_DIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="Video Host")


# ---------- utilidades ----------

def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:50] or "video"


def new_video_id(filename: str) -> str:
    return f"{slugify(Path(filename).stem)}-{secrets.token_hex(3)}"


def check_ext(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED:
        raise HTTPException(400, f"Formato no permitido. Usa: {', '.join(ALLOWED)}")
    return ext


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


def upload_paths(upload_id: str) -> tuple[Path, Path]:
    if not UPLOAD_ID_RE.match(upload_id):
        raise HTTPException(404, "Subida no encontrada")
    meta, part = PARTS_DIR / f"{upload_id}.json", PARTS_DIR / f"{upload_id}.part"
    if not meta.is_file() or not part.is_file():
        raise HTTPException(404, "Subida no encontrada o expirada")
    return meta, part


def cleanup_old_parts(max_age_hours: int = 24):
    limit = time.time() - max_age_hours * 3600
    for p in PARTS_DIR.iterdir():
        try:
            if p.stat().st_mtime < limit:
                p.unlink()
        except OSError:
            pass


def append_bytes(path: Path, data: bytes):
    with path.open("ab") as f:
        f.write(data)


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
  progress{{width:100%;height:14px}} #estado{{min-height:1.2em}} .error{{color:#ff8080}}
</style></head><body>{body}</body></html>""")


UPLOAD_JS = """
<script>
const form = document.getElementById('subir');
const estado = document.getElementById('estado');
const barra = document.getElementById('barra');
const boton = form.querySelector('button');

function mostrar(msg, error) {
  estado.textContent = msg;
  estado.className = error ? 'error' : '';
}

async function pedir(url, opciones, intentos = 5) {
  for (let i = 1; ; i++) {
    try {
      const r = await fetch(url, opciones);
      if (r.ok || r.status === 409 || (r.status >= 400 && r.status < 500)) return r;
      throw new Error('HTTP ' + r.status);
    } catch (e) {
      if (i >= intentos) throw e;
      mostrar('Conexión inestable, reintentando (' + i + '/' + (intentos - 1) + ')...');
      await new Promise(res => setTimeout(res, 1500 * i));
    }
  }
}

async function errorDe(r) {
  try { return (await r.json()).detail || ('HTTP ' + r.status); }
  catch { return 'HTTP ' + r.status; }
}

form.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const archivo = form.file.files[0];
  if (!archivo) return;
  const token = form.token ? form.token.value : '';
  const headers = { 'X-Upload-Token': token };
  const CHUNK = CHUNK_BYTES;

  boton.disabled = true;
  barra.hidden = false;
  barra.value = 0;
  try {
    let r = await pedir('/subida/iniciar', {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ nombre: archivo.name, tamano: archivo.size }),
    });
    if (!r.ok) throw new Error(await errorDe(r));
    const { upload_id } = await r.json();

    let offset = 0;
    while (offset < archivo.size) {
      const parte = archivo.slice(offset, offset + CHUNK);
      r = await pedir('/subida/' + upload_id + '?offset=' + offset, {
        method: 'PUT', headers, body: parte,
      });
      if (r.status === 409 || r.ok) {
        offset = (await r.json()).recibido;   // el servidor dice cuánto tiene
      } else {
        throw new Error(await errorDe(r));
      }
      barra.value = offset / archivo.size;
      mostrar('Subiendo... ' + Math.floor(100 * offset / archivo.size) + '% (' +
              (offset / 1048576).toFixed(1) + ' / ' + (archivo.size / 1048576).toFixed(1) + ' MB)');
    }

    mostrar('Terminando...');
    r = await pedir('/subida/' + upload_id + '/finalizar', { method: 'POST', headers });
    if (!r.ok) throw new Error(await errorDe(r));
    window.location.href = (await r.json()).ver;
  } catch (e) {
    mostrar('Error: ' + e.message, true);
    boton.disabled = false;
  }
});
</script>
"""


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
        rows.append(f'<li><a href="/ver/{vid}">{html.escape(vid)}</a> '
                    f'<small>· {mb:.1f} MB · {html.escape(FECHA_FIJA)}</small></li>')
    token_field = '<input type="password" name="token" placeholder="Token de subida" required><br>' if UPLOAD_TOKEN else ""
    body = f"""
<h1>🎬 Videos</h1>
<form id="subir">
  <strong>Subir video</strong> <small>({", ".join(ALLOWED)} · máx {MAX_UPLOAD_MB} MB)</small><br>
  <input type="file" name="file" accept="video/*" required><br>
  {token_field}
  <button type="submit">Subir</button>
  <progress id="barra" max="1" value="0" hidden></progress>
  <div id="estado"></div>
</form>
<ul>{"".join(rows) or "<li><small>Aún no hay videos.</small></li>"}</ul>
{UPLOAD_JS.replace("CHUNK_BYTES", str(CHUNK_MB * 1_048_576))}"""
    return page("Videos", body)


# --- subida por partes (la usa el formulario web) ---

@app.post("/subida/iniciar")
async def subida_iniciar(request: Request, x_upload_token: str | None = Header(None)):
    check_token(x_upload_token)
    try:
        data = await request.json()
        nombre = str(data.get("nombre") or "video.mp4")
        tamano = int(data.get("tamano") or 0)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Datos inválidos")
    ext = check_ext(nombre)
    if tamano <= 0:
        raise HTTPException(400, "El archivo está vacío")
    if tamano > MAX_BYTES:
        raise HTTPException(413, f"El archivo supera {MAX_UPLOAD_MB} MB")

    await run_in_threadpool(cleanup_old_parts)
    upload_id = secrets.token_hex(16)
    meta = {"video_id": new_video_id(nombre), "ext": ext, "tamano": tamano}
    (PARTS_DIR / f"{upload_id}.json").write_text(json.dumps(meta))
    (PARTS_DIR / f"{upload_id}.part").touch()
    return {"upload_id": upload_id}


@app.put("/subida/{upload_id}")
async def subida_parte(
    upload_id: str, offset: int, request: Request, x_upload_token: str | None = Header(None)
):
    check_token(x_upload_token)
    meta_path, part = upload_paths(upload_id)
    meta = json.loads(meta_path.read_text())

    recibido = part.stat().st_size
    if offset != recibido:
        # El navegador reintentó una parte que ya llegó (o se desincronizó): le decimos dónde seguir
        return JSONResponse({"recibido": recibido}, status_code=409)

    data = await request.body()
    if recibido + len(data) > meta["tamano"]:
        raise HTTPException(400, "Se recibieron más datos de los anunciados")
    await run_in_threadpool(append_bytes, part, data)
    return {"recibido": recibido + len(data)}


@app.post("/subida/{upload_id}/finalizar")
async def subida_finalizar(upload_id: str, x_upload_token: str | None = Header(None)):
    check_token(x_upload_token)
    meta_path, part = upload_paths(upload_id)
    meta = json.loads(meta_path.read_text())
    if part.stat().st_size != meta["tamano"]:
        raise HTTPException(400, "La subida está incompleta")

    video_id = meta["video_id"]
    part.rename(VIDEO_DIR / f"{video_id}{meta['ext']}")
    meta_path.unlink(missing_ok=True)
    return {"id": video_id, "ver": f"/ver/{video_id}", "archivo": f"/video/{video_id}"}


# --- subida en una sola petición (para curl / scripts) ---

@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    token: str | None = Form(None),
    x_upload_token: str | None = Header(None),
):
    check_token(token or x_upload_token)
    original = file.filename or "video.mp4"
    ext = check_ext(original)

    video_id = new_video_id(original)
    dest = VIDEO_DIR / f"{video_id}{ext}"
    tmp = PARTS_DIR / f"{video_id}{ext}.tmp"

    await run_in_threadpool(save_stream, file.file, tmp, MAX_BYTES)
    tmp.rename(dest)
    return JSONResponse({"id": video_id, "ver": f"/ver/{video_id}", "archivo": f"/video/{video_id}"}, status_code=201)


# ---------- ver videos ----------

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
