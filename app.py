import io, re, json, tempfile
from typing import Optional, List
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pdfminer.high_level import extract_text as pdf_text
import docx2txt

# OCR fallback (won't crash if missing libs at build time)
try:
    from pdf2image import convert_from_bytes
    import pytesseract
    OCR_OK = True
except Exception:
    OCR_OK = False

app = FastAPI(title="BOSS Analyzer Endpoint", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

WORD = {
    "one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,
    "eleven":11,"twelve":12,"thirteen":13,"fourteen":14,"fifteen":15,"sixteen":16,"seventeen":17,
    "eighteen":18,"nineteen":19,"twenty":20,"thirty":30,"forty":40,"fifty":50,"sixty":60,
    "seventy":70,"eighty":80,"ninety":90,"hundred":100
}
def word_to_num(x:str)->Optional[int]:
    if not x: return None
    x = re.sub(r"[^a-z]", "", x.lower())
    return WORD.get(x)

EXPLICIT_TWO = re.compile(r"\b(net\s*2|within\s*\(?2\)?\s*(?:business\s*)?days?)\b", re.I)

def find_payment_days(text: str)->Optional[int]:
    relevant = ". ".join([t for t in re.split(r"[\r\n.]+", text) if re.search(r"(payment|invoice|receipt|payable|net|due|days?)", t, re.I)])
    picks: List[tuple] = []
    for m in re.finditer(r"\bnet[-\s]?((\d{1,3})|([a-z]+)\s*\(\s*(\d{1,3})\s*\)|([a-z]+))\b", relevant, re.I):
        n = m.group(2) or m.group(4) or m.group(5)
        n = int(n) if (n and n.isdigit()) else word_to_num(n)
        if n is None: continue
        if n == 2: picks.append((2,3))
        elif 5 <= n <= 365: picks.append((n,8))
    for m in re.finditer(r"\b(?:due\s+(?:in|within)|within)\s+(?:\(?([a-z]+)\)?\s*)?(?:\(?(\d{1,3})\)?)?\s*(?:calendar\s*)?days?\b", relevant, re.I):
        n = m.group(2) or m.group(1)
        n = int(n) if (n and n.isdigit()) else word_to_num(n)
        if n is None: continue
        if n == 2: picks.append((2,3))
        elif 5 <= n <= 365: picks.append((n,7))
    for m in re.finditer(r"\bpast\s+due\s+(?:after\s+)?(\d{1,3})\s*days?\b", relevant, re.I):
        n = int(m.group(1))
        if 5 <= n <= 365: picks.append((n,5))
    if not picks: return None
    picks.sort(key=lambda x: (-x[1], x[0]))
    n = picks[0][0]
    if n == 2 and not EXPLICIT_TWO.search(relevant):
        return None
    return n

def find_noa(text:str)->bool:
    return bool(re.search(r"(notice\s+of\s+assignment|assignment\s+notice|assignment\s+of\s+accounts)", text, re.I)
                and re.search(r"\brev\s*capital\b", text, re.I))

def find_nonsolicit(text:str)->bool:
    return bool(re.search(r"(non[-\s]?solicit|non[-\s]?hire|client\s+hire|liquidated\s+damages|conversion\s+fee)", text, re.I))

def find_conversions(text:str)->bool:
    return bool(re.search(r"(conversion|temp[-\s]?to[-\s]?perm)", text, re.I))

def find_indemnity(text:str)->str:
    block = re.search(r".{0,200}indemnif(?:y|ication).{0,400}", text, re.I)
    if not block: return "none"
    return "mutual" if re.search(r"(each\s+party|mutual(?:ly)?\s+indemn|both\s+parties)", block.group(0), re.I) else "one-sided"

def extract_text_from_pdf(bytes_data: bytes)->str:
    try:
        txt = pdf_text(io.BytesIO(bytes_data)) or ""
        if clean(txt): return txt
    except Exception:
        pass
    if OCR_OK:
        try:
            from pdf2image import convert_from_bytes
            import pytesseract
            pages = convert_from_bytes(bytes_data, fmt="png", dpi=200)
            ocr_txt = []
            for img in pages[:20]:
                ocr_txt.append(pytesseract.image_to_string(img))
            return "\n".join(ocr_txt)
        except Exception:
            pass
    return ""

def extract_text(file: UploadFile)->str:
    name = (file.filename or "").lower()
    data = file.file.read()
    if name.endswith(".txt"):
        return data.decode(errors="ignore")
    if name.endswith(".docx"):
        with tempfile.NamedTemporaryFile(suffix=".docx") as tmp:
            tmp.write(data); tmp.flush()
            return docx2txt.process(tmp.name) or ""
    if name.endswith(".pdf"):
        return extract_text_from_pdf(data)
    try:
        return data.decode()
    except Exception:
        return ""

@app.post("/analyze")
async def analyze(file: UploadFile = File(...)):
    text_raw = extract_text(file)
    text_norm = clean(text_raw)
    if not text_norm:
        raise HTTPException(422, detail="Could not read text (try a clearer PDF/DOCX or enable OCR build).")

    days = find_payment_days(text_norm)
    noa = find_noa(text_norm)
    ns  = find_nonsolicit(text_norm)
    conv = find_conversions(text_norm)
    indem = find_indemnity(text_norm)

    lights = []
    if days is None: lights.append({"label":"Payment Terms","status":"fail","note":"(not found)"})
    elif days<=30:  lights.append({"label":"Payment Terms","status":"pass","note":f"({days} days)"})
    elif days<=60:  lights.append({"label":"Payment Terms","status":"warn","note":f"({days} days)"})
    else:           lights.append({"label":"Payment Terms","status":"fail","note":f"({days} days)"})
    lights.append({"label":"NOA to Rev Capital","status":"pass" if noa else "fail","note":"(found)" if noa else "(not found)"})
    lights.append({"label":"Client Hire / Non-Solicit","status":"pass" if ns else "warn","note":"(present)" if ns else "(missing)"})
    lights.append({"label":"Conversions","status":"pass" if conv else "warn","note":"(present)" if conv else "(missing)"})
    lights.append({"label":"Indemnity","status":"pass" if indem in ("none","mutual") else "fail","note":"(mutual)" if indem=="mutual" else ("(none)" if indem=="none" else "(one-sided)")})
    lights.append({"label":"Insurance","status":"warn","note":"(client minimums not auto-evaluated here)"})

    cards = []
    cards.append({"title":"Payment Terms", "body": "Reason: Terms — (not found)" if days is None else f"Reason: Terms — {days} days"})
    cards.append({"title":"Notice of Assignment", "body": "Reason: NOA referencing Rev Capital present." if noa else "Reason: NOA not found — must include verbatim “Rev Capital” language."})
    cards.append({"title":"Client Hire / Non-Solicit", "body": "Present" if ns else "Missing"})
    cards.append({"title":"Conversions (Temp-to-Perm)", "body": "Present" if conv else "Missing"})
    cards.append({"title":"Indemnity", "body": "Mutual" if indem=="mutual" else ("None (preferred)" if indem=="none" else "One-sided (client-favored)")})

    return {"ok": True, "lights": lights, "cards": cards}
