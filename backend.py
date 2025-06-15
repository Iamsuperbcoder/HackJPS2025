# backend.py  ───────────────────────────────────────────────────────────────
"""
ProBridge – Flask back-end
• PDF/DOCX/TXT summariser (GPT-3.5)
• Three-stage PDF fallback:
    1. PyPDF2 text            (no deps)
    2. PyMuPDF text           (pip install pymupdf)
    3. PyMuPDF → PIL → OCR    (needs Tesseract, *no* Poppler)
• Quiz generator with self-repairing JSON
"""

import os, re, json, tempfile, random, logging
from pathlib import Path
from typing import List, Dict, Any

from flask import Flask, request, jsonify, send_from_directory, session
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

# ── core libs --------------------------------------------------------------
from PyPDF2 import PdfReader, errors as pdf_err            # always present
try:
    import fitz                                            # PyMuPDF
except ImportError:
    fitz = None
try:
    import docx                                            # python-docx
except ImportError:
    docx = None
try:                                                       # OCR bits
    import pytesseract
    from PIL import Image
    pytesseract.pytesseract.tesseract_cmd = (
            os.getenv("TESSERACT_PATH") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    )
except ImportError:
    pytesseract = Image = None

# ── OpenAI -----------------------------------------------------------------
load_dotenv()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_KEY or not OPENAI_KEY.startswith("sk-"):
    raise RuntimeError("OPENAI_API_KEY missing or invalid – add it to .env")
from openai import OpenAI
client = OpenAI()

# ── Flask ------------------------------------------------------------------
app            = Flask(__name__)
app.secret_key = os.getenv("SESSION_KEY", os.urandom(24).hex())

# ── micro “DB” -------------------------------------------------------------
DB = Path("users.json"); DB.write_text("{}") if not DB.exists() else None
USERS = json.loads(DB.read_text())
def _save(): DB.write_text(json.dumps(USERS, indent=2))
def me():     return session.get("user")

# ───────────────────────────────── text extraction helpers ─────────────────
def _pdf_text_pymupdf(path:str) -> str:
    if not fitz: return ""
    try:
        with fitz.open(path) as doc:
            return " ".join(p.get_text() for p in doc)
    except Exception as e:
        logging.debug("PyMuPDF text failed: %s", e)
        return ""

def _pdf_ocr_pymupdf(path:str) -> str:
    """Render pages via PyMuPDF → PIL → Tesseract (no Poppler)"""
    if not (fitz and pytesseract and Image):               # missing deps
        return ""
    try:
        txt = []
        with fitz.open(path) as doc:
            for page in doc:
                pix = page.get_pixmap(dpi=300, colorspace=fitz.csRGB)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                txt.append(pytesseract.image_to_string(img))
        return " ".join(txt)
    except Exception as e:
        logging.warning("PyMuPDF OCR failed: %s", e)
        return ""

def extract_text(path:str, mime:str) -> str:
    """PDF → PyPDF2 → PyMuPDF text → PyMuPDF OCR  |  DOCX  |  plain-text"""
    if mime == "application/pdf":
        # 1️⃣ PyPDF2
        try:
            text = " ".join(p.extract_text() or "" for p in PdfReader(path).pages)
        except pdf_err.DependencyError:
            text = ""

        # 2️⃣ PyMuPDF text
        if len(text.strip()) < 30:
            text = _pdf_text_pymupdf(path)

        # 3️⃣ PyMuPDF OCR
        if len(text.strip()) < 30:
            text = _pdf_ocr_pymupdf(path)

        return text

    if mime.endswith(("msword", "document")) and docx:
        return " ".join(p.text for p in docx.Document(path).paragraphs)

    try:
        return open(path, "r", encoding="utf-8", errors="ignore").read()
    except Exception:
        return ""

# ───────────────────────────────── AI helpers ──────────────────────────────
def level_phrase(n:int)->str:
    return ("for an everyday 8-th grade reader." if n<=8 else
            "for a 10-th grade reader."         if n<=10 else
            "for a 12-th grade reader."         if n<=12 else
            "for a college reader."             if n<=16 else
            "for a master’s-level audience."    if n<=18 else
            "for a PhD-level audience.")

def rewrite(txt:str, domain:str, lvl:int)->str:
    prompt = (
        f"Rewrite the following {domain} document {level_phrase(lvl)} "
        "Start with one concise overview paragraph, then bullet-point key facts, "
        "and finish with numbered ‘Next Steps’. Do not mention grade levels."
    )
    r = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role":"user","content":prompt + "\n\n" + txt}],
        max_tokens=450, temperature=0.5
    )
    return r.choices[0].message.content.strip()

def ocr_image(path:str)->str:
    if pytesseract and Image:
        try: return pytesseract.image_to_string(Image.open(path))
        except Exception: pass
    return ""

# ─────────────────────────────── endpoints ────────────────────────────────
@app.post("/signup")
def signup():
    d=request.get_json(True); u,p=d["username"],d["password"]
    if u in USERS: return jsonify(msg="Username exists"),400
    USERS[u]=generate_password_hash(p); _save(); session["user"]=u
    return jsonify(ok=True)

@app.post("/login")
def login():
    d=request.get_json(True); u,p=d["username"],d["password"]
    if u not in USERS or not check_password_hash(USERS[u],p):
        return jsonify(msg="Bad credentials"),401
    session["user"]=u; return jsonify(ok=True)

@app.get("/logout")
def logout():
    session.clear(); return jsonify(ok=True)

@app.get("/whoami")
def whoami(): return jsonify(user=me())

# ── summarise / prescription ----------------------------------------------
@app.post("/summarize")
def summarize():
    lvl=int(request.form.get("level","10"))
    f  =request.files["file"]
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        f.save(tmp.name); raw=extract_text(tmp.name, f.mimetype)

    if len(raw.strip())<30:
        return jsonify(domain="unknown",
                       summary="Sorry, I couldn’t extract readable text from that document.")

    dom="medical" if any(w in raw.lower() for w in ("patient","diagnosis","mg")) else "legal"
    return jsonify(domain=dom, summary=rewrite(raw, dom, lvl))

@app.post("/prescription")
def prescription():
    if not pytesseract:
        return jsonify(rx="OCR not available on this server.")
    f=request.files["file"]
    with tempfile.NamedTemporaryFile(delete=False,suffix=".img") as tmp:
        f.save(tmp.name); txt=ocr_image(tmp.name)
    if len(txt.strip())<20:
        return jsonify(rx="Sorry — I couldn’t read that prescription clearly.")
    prompt=(
            "You are a pharmacy assistant. From the OCR text of a prescription, provide:\n"
            "• **Drug name**\n• **What it treats / how it works**\n• **Dosage & timing**\n"
            "• **Important precautions or side-effects**\n"
            "Answer as concise Markdown bullet lists without referencing OCR.\n\n"+txt
    )
    r=client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role":"user","content":prompt}],
        max_tokens=300, temperature=0.4
    )
    return jsonify(rx=r.choices[0].message.content.strip())

# ── translate / chat -------------------------------------------------------
@app.post("/translate")
def translate():
    d=request.get_json(True); text,lang=d["text"],d["lang"]
    if not lang: return jsonify(translated="")
    r=client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role":"user",
                   "content":f"Translate to {lang} keeping bullet formatting:\n\n{text}"}],
        max_tokens=800, temperature=0.3
    )
    return jsonify(translated=r.choices[0].message.content.strip())

@app.post("/chat")
def chat():
    d=request.get_json(True); q,ctx=d["question"],d["context"]
    r=client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role":"system","content":"Answer using only the provided summary."},
                  {"role":"assistant","content":ctx},
                  {"role":"user","content":q}],
        max_tokens=300, temperature=0.4
    )
    return jsonify(answer=r.choices[0].message.content.strip())

@app.post("/chat-image")
def chat_image():
    if not pytesseract:
        return jsonify(answer="OCR not available on this server.")
    img=request.files["image"]; ctx=request.form.get("context","")
    with tempfile.NamedTemporaryFile(delete=False,suffix=".img") as tmp:
        img.save(tmp.name); txt=ocr_image(tmp.name)
    r=client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role":"system","content":"If the image is a Rx, extract details; else say you can’t read it."},
                  {"role":"assistant","content":ctx},
                  {"role":"user","content":"OCR:\n"+txt}],
        max_tokens=250, temperature=0.4
    )
    return jsonify(answer=r.choices[0].message.content.strip())

# ── quiz generator ---------------------------------------------------------
def _clean_quiz(raw:str)->List[Dict[str,Any]]:
    try:
        data=json.loads(raw)
    except Exception:
        return []
    fixed=[]
    for q in data:
        if not isinstance(q,dict): continue
        qtext=str(q.get("question","")).strip()
        qtype=str(q.get("type","mcq")).lower()
        ans  =str(q.get("answer","")).strip()
        opts =q.get("options",[])
        if qtype not in ("mcq","written"): qtype="mcq"
        if qtype=="mcq":
            if not (opts and isinstance(opts,list)):
                opts=[ans]+[f"Option {c}" for c in "BCD"]
            random.shuffle(opts)
        fixed.append(dict(question=qtext,type=qtype,options=opts,answer=ans))
    return fixed

@app.post("/make_quiz")
def make_quiz():
    req=request.get_json(True)
    summary,diff,num=req["summary"],req["difficulty"],req["num"]
    base = (f"Create a {diff.lower()} quiz of {num} questions based ONLY on the text below. "
            "Use a mix of multiple-choice (label options A-D) and short-answer questions. "
            "Return STRICT JSON array with keys question,type ('mcq'|'written'),options,answer. "
            "NO markdown or commentary.\n\nTEXT:\n"+summary)
    def ask(prompt:str)->str:
        r=client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}],
            max_tokens=800, temperature=0.7
        ).choices[0].message.content
        return re.sub(r"```(?:json)?|```","",r).strip()
    raw=ask(base)
    for _ in range(3):
        if len(_clean_quiz(raw))==num: break
        raw=ask("Your previous JSON was malformed. Return ONLY the JSON array.\n"+raw)
    return jsonify(_clean_quiz(raw))

# ── static files -----------------------------------------------------------
@app.route("/",defaults={"path":""})
@app.route("/<path:path>")
def send_front(path):
    if path=="" or not path.endswith((".html",".js",".css")):
        path="index.html"
    return send_from_directory("frontend", path)

# ── run --------------------------------------------------------------------
if __name__=="__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(debug=True, port=5000)
