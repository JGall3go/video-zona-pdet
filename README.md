# Video Host (FastAPI) para Coolify

Mini servicio para subir videos y verlos desde una URL.

| Ruta | Qué hace |
|---|---|
| `GET /` | Lista de videos + formulario de subida |
| `POST /upload` | Sube un video (campo `file`) |
| `GET /ver/{id}` | Página con reproductor |
| `GET /video/{id}` | Archivo directo (soporta Range: se puede adelantar) |
| `GET /health` | Healthcheck |

Formatos: mp4, webm, mov, m4v, mkv, ogv. Para que se vea en cualquier navegador, usa **MP4 (H.264 + AAC)**.

## Desplegar en Coolify

1. Sube esta carpeta a un repo de GitHub/GitLab.
2. En Coolify: **+ New → Public/Private Repository**, elige el repo.
3. **Build Pack: Dockerfile**. Puerto expuesto: `8000`.
4. **Storages → Add Volume**: destino `/data/videos` (aquí se guardan los videos; sin esto se pierden al redeploy).
5. **Environment Variables**:
   - `UPLOAD_TOKEN` = una clave secreta (recomendado; sin ella cualquiera puede subir).
   - `MAX_UPLOAD_MB` = límite en MB (por defecto `2048`).
6. Asigna tu dominio y despliega.

> Si los videos grandes fallan al subir, revisa el límite de tamaño del proxy (Traefik/Caddy) en Coolify.

## Uso

Desde el navegador: entra a `https://tu-dominio/`, elige el archivo, pon el token y sube. Te redirige a `/ver/{id}`.

Desde la terminal:

```bash
curl -H "X-Upload-Token: TU_TOKEN" -F "file=@mi-video.mp4" https://tu-dominio/upload
# {"id":"mi-video-a1b2c3","ver":"/ver/mi-video-a1b2c3","archivo":"/video/mi-video-a1b2c3"}
```

Para incrustarlo en otra web:

```html
<video src="https://tu-dominio/video/mi-video-a1b2c3" controls></video>
```

## Correr en local

```bash
pip install -r requirements.txt
VIDEO_DIR=./data uvicorn app.main:app --reload
# o con Docker
docker build -t video-host . && docker run -p 8000:8000 -v $(pwd)/data:/data/videos -e UPLOAD_TOKEN=secreto video-host
```
