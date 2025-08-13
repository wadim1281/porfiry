#!/usr/bin/env python3
"""
backend.py – FastAPI + Ollama Gemma‑3‑27B‑IT (vision)

• `/generate` → ready Markdown
• `/generate/stream` → token stream

Run: `LOGLEVEL=DEBUG python -m uvicorn backend:app --reload`  

"""
from __future__ import annotations

# ───────── stdlib ─────────
import base64, functools, gc, inspect, logging, mimetypes, os, re, textwrap, time, urllib.parse
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

# ───────── 3‑rd party ─────────
import ollama           # pip install --upgrade ollama>=0.5
import psutil
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from tinydb import TinyDB, Query

# ───────── config ─────────

'''
SYSTEM_PROMPT = textwrap.dedent("""\
You are an analyst writing vulnerability reports in pentests. You are writing a report for the Customer. Your colleague, a pentester, gives you information about the vulnerabilities found.
You need to correctly, in technical language, describe the vulnerability, based on the text and pictures that he sent you.
Always insert a screenshot in the correct place in the text, after the description of the action described in the screenshot, for example, ![alt](screenshotN.png).
The report is structured like this:
## Vulnerability name
### Description \n
A few sentences about the vulnerability

| **Parameter** | **Value** |
| --------------- | --------------------------------------------- |
| **Severity** | ![CRITICAL](https://img.shields.io/badge/Severity-Critical-red) / High / Medium / Low |
| **Host** | `You must specify the IP address, subnet, or DNS name of the victim.` |

### Proof of exploitation
You must start with the phrase "To exploit this vulnerability, you must perform the following actions."
Based on the screenshots and text that you were sent, describe the exploitation of the vulnerability. Insert a link to the screenshots sent in the right place in the text.
Carefully ensure that the number of screenshots in the report matches the number of screenshots that were sent to you. First, describe the actions and then insert the screenshot.
### Risk analysis\n

### Recommendations
Here you need 2-3 most important recommendations for the customer.\n\n

""")
'''
SYSTEM_PROMPT = textwrap.dedent("""\
You are a vulnerability reporting analyst for pentests. You are writing a report for the Customer. Your colleague, a pentester, gives you information about the vulnerabilities found. You need to correctly, in technical language, describe the vulnerability, based on the text and pictures that he sent you.
Always insert screenshots directly into the text — ![alt](screenshotN.png).
The report is structured like this:
## Vulnerability name
### Description \n
A few sentences about the vulnerability

| **Parameter** | **Value** |
| --------------- | --------------------------------------------- |
| **Severity** | ![CRITICAL](https://img.shields.io/badge/Severity-Critical-red) / High / Medium / Low |
| **Node** | `You must specify the IP address, subnet, or DNS name of the victim.` |

### Proof of exploitation
You must start with the phrase "To exploit this vulnerability, you must perform the following actions."
In this chapter, you must prove the existence of the vulnerability based on the information and screenshots that the penetration tester sends you. When doing this, indicate where to insert the images using ![alt](screenshotN.png).
### Risk analysis
### Recommendations
Here you need 2-3 most important recommendations for the customer.
""")







# NEW: prompt focused on Kill-chain description
KILLCHAIN_PROMPT = textwrap.dedent("""\
you and I are writing a scenario of maximum attacks for a pentest report, I will give you a description of the killchain in a sequence of actions in informal language and you adapt it for the report.
The report should be written in competent technical language, avoiding slang expressions in markdown format.
Links to screenshots in markdown format must be left as is, without changing anything. In this case, you must immediately write under the screenshot
Screenshot: here is a short description.
I left hints for a short description under each screenshot.
Start the description with the title
## Attack scenario
then a short summary of the attack, which services were compromised
then a description of each stage of the attack
other than this, nothing needs to be described
""")


OLLAMA_HOST  = os.getenv("OLLAMA_HOST", "http://localhost:11434")
#OLLAMA_HOST  = os.getenv("OLLAMA_HOST", "http://localhost:1234")
#OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5vl:72b-Q4_K_M")
#OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5vl:32b-q8_0")
#OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5vl:7b-q8_0")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma3:27b")
#OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:30b-a3b-thinking-2507-q4_K_M")
TEMP, NUM_PREDICT = 0.10, 2048
PROBE_ROUTES = False

# ───────── logging ─────────
logging.basicConfig(level=os.getenv("LOGLEVEL", "INFO").upper(),
                    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s")
log, dbg = logging.getLogger("api"), logging.getLogger("debug")
mem = lambda: f"RAM={psutil.Process(os.getpid()).memory_info().rss/2**30:.2f} GB"


def probe(tag:str)->Callable[[Callable],Callable]:
    def wrap(fn:Callable)->Callable:
        @functools.wraps(fn)
        def inner(*a,**kw):
            t0=time.perf_counter(); dbg.debug("▶ %s | %s", tag, mem())
            try: return fn(*a,**kw)
            finally: dbg.debug("⏹ %s | %.2fs | %s", tag,time.perf_counter()-t0, mem())
        inner.__signature__=inspect.signature(fn)
        return inner
    return wrap

# ───────── init ─────────
client = ollama.Client(host=OLLAMA_HOST)
app = FastAPI(title="VulnReport API (Ollama)")
db  = TinyDB("reports.json"); TBL, Q = db.table("reports"), Query()

@app.middleware("http")
async def access(req:Request, nxt):
    t0=time.perf_counter(); resp:Response = await nxt(req)
    log.info("%s %s → %d %.1f ms", req.method, req.url.path, resp.status_code,(time.perf_counter()-t0)*1000)
    return resp

@app.exception_handler(RequestValidationError)
async def ve(_, exc):
    dbg.error("422 %s", exc.errors()); return JSONResponse(status_code=422, content={"detail": exc.errors()})

# ───────── models ─────────
class GenerateRequest(BaseModel):
    history:List[Dict[str,str]]=[]; images:List[str]=[]; filenames:List[str]=[]
class GenerateResponse(BaseModel):
    markdown:str; raw:str
class SavedReport(BaseModel):
    project:str="default"; markdown:str; images:List[str]; filenames:List[str]; history:List[Any]

# ───────── helpers ─────────

def to_data(p:str,n:str)->str:
    mime=mimetypes.guess_type(n)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(open(p,'rb').read()).decode()}"

def _lookup(u:str,m:Dict[str,str]):
    return m.get(u) or m.get(os.path.basename(urllib.parse.urlparse(u).path))

def inline(md:str,m:Dict[str,str],order:List[str])->str:
    unused=iter(m[k] for k in order)
    md=re.sub(r'(!\[[^\]]*]\()([^)]*)(\))',lambda x:f"{x.group(1)}{_lookup(x.group(2),m) or next(unused,x.group(2))}{x.group(3)}",md)
    md=re.sub(r'(!\[\[)([^\]]+)(]])',lambda x:f"{x.group(1)}{m.get(x.group(2)) or next(unused,x.group(2))}{x.group(3)}",md)
    return re.sub(r'Скриншот\s+(\d+):\s*(.+)',lambda x:f"![{x.group(2).strip()}]({m[order[int(x.group(1))-1]]})" if x.group(1).isdigit() and 1<=int(x.group(1))<=len(order) else x.group(0),md)

# ───────── LLM wrappers ─────────

def build_messages(req:GenerateRequest,imgs:List[bytes],prompt:str=SYSTEM_PROMPT):
    msgs=[{"role":"system","content":prompt}]
    if req.history:
        hist=[m.copy() for m in req.history]
        for m in hist:
            if m.get("role")=="user" and imgs: m["images"]=imgs; break
        msgs+=hist
    else:
        msgs.append({"role":"user","content":"(empty draft)","images":imgs or None})
    return msgs

def ollama_chat(msgs):
    resp=client.chat(model=OLLAMA_MODEL,messages=msgs,stream=False,options={"temperature":TEMP,"num_predict":NUM_PREDICT,"num_ctx":14096})
    return resp["message"]["content"].strip()

def ollama_stream(msgs):
    for c in client.chat(model=OLLAMA_MODEL,messages=msgs,stream=True,options={"temperature":TEMP,"num_predict":NUM_PREDICT,"num_ctx":14096}):
        tok=c["message"]["content"]
        if tok: yield tok.encode()



# ───────── core ─────────

def _generate_logic(req:GenerateRequest):
    imgs=[open(p,'rb').read() for p in req.images]
    msgs=build_messages(req,imgs)
    try: raw=ollama_chat(msgs)
    except Exception as e:
        log.exception("LLM fail"); raise HTTPException(500,str(e))
    md=inline(raw,{n:to_data(p,n) for n,p in zip(req.filenames,req.images)},req.filenames)
    return {"markdown":md,"raw":raw}

# ───────── endpoints ─────────
@app.post("/generate",response_model=GenerateResponse)
def gen(req:GenerateRequest):
    fn=_generate_logic if not PROBE_ROUTES else probe("gen")(_generate_logic)
    return fn(req)

@app.post("/generate/stream")
async def gen_stream(req:GenerateRequest):
    imgs=[open(p,'rb').read() for p in req.images]
    msgs=build_messages(req,imgs)
    async def streamer():
        try:
            for t in ollama_stream(msgs): yield t
        except Exception as e:
            yield f"\n[ERROR] {e}".encode()
    return StreamingResponse(streamer(),media_type="text/plain")

@app.post("/generate/killchain/stream")
async def gen_killchain_stream(req:GenerateRequest):
    imgs=[open(p,'rb').read() for p in req.images]
    msgs=build_messages(req,imgs,KILLCHAIN_PROMPT)
    async def streamer():
        try:
            for t in ollama_stream(msgs): yield t
        except Exception as e:
            yield f"\n[ERROR] {e}".encode()
    return StreamingResponse(streamer(),media_type="text/plain")

@app.post("/reports/save")
def save(rep:SavedReport):
    doc=rep.dict()|{"id":uuid4().hex,"ts":time.time()}
    TBL.insert(doc); return {"ok":True,"id":doc["id"]}

@app.get("/reports/{project}")
@app.get("/reports/{project}/{vid}")
def reports(project:str,vid:Optional[str]=None):
    if vid:
        doc=TBL.get((Q.project==project)&(Q.id==vid))
        if not doc: raise HTTPException(404)
        return doc
    return TBL.search(Q.project==project)

# ───────── shutdown ─────────
@app.on_event("shutdown")
def _cleanup(): gc.collect()