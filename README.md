# YukiLoader

A simple web interface for downloading video and audio from a link, powered by yt-dlp.

**Live:** [loader.yukichat.lol](https://loader.yukichat.lol)

## Features

- Paste a link to see the title, thumbnail, duration and available qualities
- Download video in the chosen quality, or the audio as MP3
- Progress, speed and estimated time while the download runs
- Works with any site yt-dlp supports

## Stack

- **Server:** Python standard library HTTP server (`server.py`), yt-dlp and ffmpeg
- **Frontend:** a single static page (`static/index.html`)

## Requirements

- Python 3.10+
- ffmpeg on `PATH`

## Running

```sh
python3 -m venv venv
venv/bin/pip install yt-dlp
venv/bin/python server.py      # listens on :6860
```

Keep yt-dlp up to date, because sites change often:

```sh
venv/bin/pip install -U yt-dlp
```

## API

| Endpoint | Description |
| --- | --- |
| `POST /api/info` `{url}` | Title, thumbnail, duration and available qualities |
| `POST /api/job` `{url, q}` | Starts a download and returns `{id}`. `q` is `best`, a height such as `1080`, or `mp3` |
| `GET /api/job/{id}` | Job state, progress, speed, ETA and any error |
| `GET /api/file/{id}` | The finished file |

## Limits and safety

- At most 3 downloads run at once, with a per-IP limit
- Files are capped at 2 GB and deleted 30 minutes after they finish
- Requests to private and local network addresses are blocked

Respect the terms of service of the sites you download from and the rights of content owners.
