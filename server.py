"""YukiLoader: download videos by link (yt-dlp). loader.yukichat.lol → nginx → :6860.

POST /api/info {url}          → title, thumb, duration, available qualities
POST /api/job  {url, q}       → {id}; q = "best" | "1080" | "720" | ... | "mp3"
GET  /api/job/{id}            → {state, pct, speed, eta, name, error}
GET  /api/file/{id}           → the file (deleted 30 min after it's ready)
"""
import asyncio, ipaddress, os, re, shutil, socket, threading, time, uuid
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")
MAX_BYTES = 2 * 1024**3
KEEP = 30 * 60
os.makedirs(WORK, exist_ok=True)
shutil.rmtree(WORK, ignore_errors=True); os.makedirs(WORK)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
jobs: dict[str, dict] = {}
slots = threading.Semaphore(3)
per_ip: dict[str, list[float]] = {}


def check_url(u: str) -> str:
    u = (u or "").strip()
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    p = urlparse(u)
    if not p.hostname or len(u) > 2000:
        raise HTTPException(400, "That doesn't look like a link")
    try:
        for fam, _, _, _, addr in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(addr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise HTTPException(400, "That link isn't allowed")
    except socket.gaierror:
        raise HTTPException(400, "Can't find that site")
    return u


def limit(req: Request, n=30):
    ip = req.headers.get("cf-connecting-ip") or req.headers.get("x-real-ip") or req.client.host
    now = time.time()
    hits = [t for t in per_ip.get(ip, []) if now - t < 600]
    if len(hits) >= n:
        raise HTTPException(429, "Too many downloads, try again in a few minutes")
    hits.append(now); per_ip[ip] = hits


BASE = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20,
        "max_filesize": MAX_BYTES, "playlist_items": "1"}


def clean_err(e) -> str:
    s = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
    s = re.sub(r"^\[[^\]]+\] [^:]+: ", "", s)
    if "Unsupported URL" in s:
        return "This site isn't supported, or there's no video on that page"
    if "Sign in" in s or "login" in s.lower() or "private" in s.lower():
        return "This video is private or needs a login"
    return s[:300] or "Couldn't get that video"


@app.post("/api/info")
async def info(req: Request):
    body = await req.json()
    url = check_url(body.get("url"))
    limit(req, 60)

    def run():
        with yt_dlp.YoutubeDL({**BASE, "skip_download": True}) as y:
            return y.extract_info(url, download=False)
    try:
        i = await asyncio.wait_for(asyncio.to_thread(run), 45)
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        raise HTTPException(504, "The site took too long to answer")
    except Exception as e:
        raise HTTPException(422, clean_err(e))
    if i.get("_type") == "playlist" and i.get("entries"):
        i = next(iter(i["entries"]), None) or i
    fmts = i.get("formats") or []
    heights = sorted({f["height"] for f in fmts if f.get("height") and f.get("vcodec") not in (None, "none")}, reverse=True)
    heights = [h for h in heights if h in (2160, 1440, 1080, 720, 480, 360, 240, 144)] or heights[:6]
    has_audio = any(f.get("acodec") not in (None, "none") for f in fmts) or not fmts
    return {
        "title": i.get("title") or "video",
        "thumb": i.get("thumbnail"),
        "duration": i.get("duration"),
        "site": i.get("extractor_key") or urlparse(url).hostname,
        "uploader": i.get("uploader") or i.get("channel"),
        "heights": heights,
        "audio": has_audio,
        "url": i.get("webpage_url") or url,
    }


def fmt_for(q: str):
    if q == "mp3":
        return {"format": "bestaudio/best",
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]}
    h = f"[height<={int(q)}]" if q.isdigit() else ""
    return {"format": f"bv*{h}[ext=mp4]+ba[ext=m4a]/bv*{h}+ba/b{h}/b", "merge_output_format": "mp4"}


def worker(jid: str, url: str, q: str):
    j = jobs[jid]
    d = os.path.join(WORK, jid); os.makedirs(d)

    def hook(s):
        if s["status"] == "downloading":
            tot = s.get("total_bytes") or s.get("total_bytes_estimate") or 0
            got = s.get("downloaded_bytes") or 0
            if got > MAX_BYTES:
                raise yt_dlp.utils.DownloadError("File is bigger than 2 GB")
            j.update(state="downloading", pct=round(got * 100 / tot, 1) if tot else None,
                     speed=s.get("speed"), eta=s.get("eta"), got=got)
        elif s["status"] == "finished":
            j.update(state="processing", pct=100)

    with slots:
        j["state"] = "starting"
        try:
            opts = {**BASE, **fmt_for(q), "outtmpl": os.path.join(d, "%(title).120B [%(id)s].%(ext)s"),
                    "progress_hooks": [hook], "concurrent_fragment_downloads": 8, "restrictfilenames": False}
            with yt_dlp.YoutubeDL(opts) as y:
                y.extract_info(url, download=True)
            files = [f for f in os.listdir(d) if not f.endswith((".part", ".ytdl")) and ".temp." not in f]
            if not files:
                raise RuntimeError("Nothing was downloaded")
            f = max(files, key=lambda x: os.path.getsize(os.path.join(d, x)))
            j.update(state="done", path=os.path.join(d, f), name=f, size=os.path.getsize(os.path.join(d, f)), done=time.time())
        except Exception as e:
            j.update(state="error", error=clean_err(e), done=time.time())
            shutil.rmtree(d, ignore_errors=True)


@app.post("/api/job")
async def job(req: Request):
    body = await req.json()
    url = check_url(body.get("url"))
    q = str(body.get("q") or "best")
    if q not in ("best", "mp3") and not (q.isdigit() and 100 <= int(q) <= 4320):
        raise HTTPException(400, "Bad quality")
    limit(req)
    jid = uuid.uuid4().hex
    jobs[jid] = {"state": "queued", "pct": 0, "at": time.time()}
    threading.Thread(target=worker, args=(jid, url, q), daemon=True).start()
    return {"id": jid}


@app.get("/api/job/{jid}")
def status(jid: str):
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404, "This download expired")
    return {k: j.get(k) for k in ("state", "pct", "speed", "eta", "got", "name", "size", "error")}


@app.get("/api/file/{jid}")
def file(jid: str):
    j = jobs.get(jid)
    if not j or j.get("state") != "done" or not os.path.exists(j["path"]):
        raise HTTPException(404, "This download expired")
    return FileResponse(j["path"], filename=j["name"], media_type="application/octet-stream")


async def janitor():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for jid, j in list(jobs.items()):
            if (j.get("done") and now - j["done"] > KEEP) or now - j["at"] > 6 * 3600:
                shutil.rmtree(os.path.join(WORK, jid), ignore_errors=True)
                jobs.pop(jid, None)


@app.on_event("startup")
async def _start():
    asyncio.create_task(janitor())


@app.exception_handler(HTTPException)
async def _err(_, e: HTTPException):
    return JSONResponse({"error": e.detail}, status_code=e.status_code)


app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=6860, proxy_headers=True)
