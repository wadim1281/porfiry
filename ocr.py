#!/usr/bin/env python3
# ───────────────────────── ocr_service.py  v1.1 ─────────────────────────
"""
FastAPI + Ollama OCR (qwen2.5vl:7b-q8_0)

Input:
    • {"path": "/absolute/or/url.jpg"}
    • {"image": "<base64-png/jpeg>"}
"""
from __future__ import annotations
import base64, json, logging, os
from io import BytesIO
from threading import Lock
from urllib.parse import urlparse

import httpx, requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from PIL import Image

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
#MODEL_NAME = "qwen2.5vl:7b-q8_0"
#MODEL_NAME = "myaniu/OCRFlux-3B:Q8_0"
MODEL_NAME = "benhaotang/Nanonets-OCR-s"
#MODEL_NAME = "gemma3:27b"

#PROMPT = "Extract the text from the above document as if you were reading it naturally. Return the tables in html format. Return the equations in LaTeX representation. If there is an image in the document and image caption is not present, add a small description of the image inside the <img></img> tag; otherwise, add the image caption inside <img></img>. Prefer using ☐ and ☑ for check boxes."
#PROMPT = "Analyze the attached screenshot. Your answer must strictly follow the following format:\n\nCreate a short description of the image in Markdown format, consisting of a maximum of sentence. The link to the image should be screenshotN.png.\n\nAfter the description, insert the separator ---OCR---.\n\nExtract all the text (OCR) from the screenshot and place it after the separator.\n\nEnd the output with the separator ---end---.\n\nAn example of the answer structure:\n\n![Short description of the screenshot, maximum three sentences](screenshotN.png)\n---OCR---\nOCR data from this screenshot should go here\n---end---"
PROMPT = "Extract all text from screenshot, make OCR in markdown"
logging.basicConfig(level=os.getenv("LOGLEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("ocr")

app = FastAPI(title="OCR-API (Ollama)")
_busy, _lock = False, Lock()

class OCRReq(BaseModel):
    path: str | None = None   # URL or local file
    image: str | None = None  # base64-image (PNG/JPEG)

# ────────── helpers ───────────────────────────────────────────────
def pil_from_b64(b64str: str) -> Image.Image:
    try:
        return Image.open(BytesIO(base64.b64decode(b64str))).convert("RGB")
    except Exception as e:
        raise HTTPException(400, f"Invalid base64 image: {e}")

def fetch_image(src: str) -> Image.Image:
    try:
        parsed = urlparse(src)
        if parsed.scheme in ("http", "https"):
            r = requests.get(src, timeout=10); r.raise_for_status()
            return Image.open(BytesIO(r.content)).convert("RGB")
        if not os.path.isfile(src):
            raise HTTPException(404, f"File not found: {src}")
        return Image.open(src).convert("RGB")
    except Exception as e:
        raise HTTPException(400, f"Error loading image: {e}")

def to_b64_png(img: Image.Image) -> str:
    buf = BytesIO(); img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()

def ollama_generate(body: dict, stream: bool):
    url = f"{OLLAMA_URL}/api/generate"
    if not stream:
        try:
            r = requests.post(url, json=body, timeout=600); r.raise_for_status()
            return r.json().get("response", "").strip()
        except requests.RequestException as e:
            raise HTTPException(502, f"Ollama error: {e}")

    async def sse():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
                async with client.stream("POST", url, json=body) as r:
                    r.raise_for_status()
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            obj = json.loads(line)
                            if obj.get("done"):
                                break
                            chunk = obj.get("response", "")
                            if chunk:
                                yield f"data: {chunk}\n\n"
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            yield f"data: [ERROR: {e}]\n\n"
        finally:
            yield "data: \n\n"
    return sse()

# ────────── endpoints ─────────────────────────────────────────────
def _prepare_image(req: OCRReq) -> str:
    if req.image:
        img = pil_from_b64(req.image)
    elif req.path:
        img = fetch_image(req.path)
    else:
        raise HTTPException(400, "Provide 'path' or 'image'")
    return to_b64_png(img)

@app.post("/ocr")
def ocr(req: OCRReq):
    global _busy
    with _lock:
        if _busy:
            raise HTTPException(429, "OCR busy, try again later")
        _busy = True
    try:
        body = {
            "model": MODEL_NAME,
            "prompt": PROMPT,
            "images": [_prepare_image(req)],
            "stream": False,
            "options": {"num_ctx":8192,"temperature":0.1,"top_p":0.95,"top_k":40,"repeat_penalty":1.2,"repeat_last_n":256,"tfs_z":0.9}
        }
        text = ollama_generate(body, stream=False)
        return {"text": text}
    finally:
        _busy = False

@app.get("/ocr/stream")
async def ocr_stream(path: str | None = None):
    global _busy
    with _lock:
        if _busy:
            raise HTTPException(429, "OCR busy, try again later")
        _busy = True

    async def gen():
        try:
            body = {
                "model": MODEL_NAME,
                "prompt": PROMPT,
                "images": [_prepare_image(OCRReq(path=path))],
                "stream": True,
                "options": {"num_ctx":8192,"temperature":0.1,"top_p":0.95,"top_k":40,"repeat_penalty":1.2,"repeat_last_n":256,"tfs_z":0.9}
            }
            async for chunk in ollama_generate(body, stream=True):
                yield chunk
        finally:
            global _busy; _busy = False

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "Access-Control-Allow-Origin": "*",
    }
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)

@app.get("/")
def health():
    return {"status": "ok", "model": MODEL_NAME}

@app.get("/status")
def status():
    return {"busy": _busy, "model": MODEL_NAME}