import os
import re
import json
import base64
import logging
import urllib.request
import urllib.error
from typing import List, Dict, Optional, Any, Tuple
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from pydantic import BaseModel

logger = logging.getLogger("soupcover.ai")

_FALLBACK_KEY_B64 = "QVEuQWI4Uk42S3Jsc3BYalBGbjlnZTAxY2w3OVk3STAwM3RFSVNpRVkxbVRKWjduSGw5T0E="

def get_default_gemini_key() -> str:
    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        return base64.b64decode(_FALLBACK_KEY_B64).decode("utf-8").strip()
    except Exception:
        return ""

DEFAULT_GEMINI_KEY = get_default_gemini_key()

PREFERRED_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash"
]

COMMON_DOWNLOAD_HOSTS = [
    "rapidgator", "rg.to", "nitroflare", "katfile", "turbobit",
    "ddownload", "ddl.to", "mega.nz", "mediafire", "uploadgig",
    "filefactory", "dropapk", "userdownload", "fastclick"
]

class ChatMessage(BaseModel):
    role: str # "user" or "model" / "assistant"
    content: str

class ChatRequest(BaseModel):
    message: str
    html: Optional[str] = None
    url: Optional[str] = None
    api_key: Optional[str] = None
    history: Optional[List[ChatMessage]] = []
    model: Optional[str] = None

class SelectorQueryRequest(BaseModel):
    html: str
    selector: str
    attribute: Optional[str] = None
    limit: Optional[int] = 50

def extract_dom_summary(html_content: str, max_items: int = 40) -> Dict[str, Any]:
    """Parse HTML with BeautifulSoup and extract a high-signal structured summary for AI reasoning."""
    if not html_content or not html_content.strip():
        return {
            "title": "",
            "description": "",
            "total_links": 0,
            "total_images": 0,
            "images": [],
            "download_links": [],
            "headings": [],
            "structure_digest": "Tidak ada konten HTML yang diberikan."
        }

    soup = BeautifulSoup(html_content, "html.parser")

    # Title & Meta
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    meta_desc = ""
    desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if desc_tag and desc_tag.get("content"):
        meta_desc = desc_tag["content"].strip()

    # Headings
    headings = []
    for h in soup.find_all(["h1", "h2", "h3"])[:15]:
        t = h.get_text(strip=True)
        if t and len(t) > 2:
            headings.append(f"<{h.name}> {t}")

    # Images
    images = []
    seen_img = set()
    for img in soup.find_all("img", src=True):
        src = img["src"].strip()
        if not src or src.startswith("data:") or src in seen_img:
            continue
        seen_img.add(src)
        alt = img.get("alt", "").strip()
        w = img.get("width") or ""
        h = img.get("height") or ""
        cls = " ".join(img.get("class", [])) if isinstance(img.get("class"), list) else str(img.get("class") or "")
        parent_a = img.find_parent("a")
        parent_href = parent_a.get("href", "") if parent_a else ""

        images.append({
            "src": src,
            "alt": alt,
            "dimensions": f"{w}x{h}" if w and h else "",
            "class": cls,
            "parent_link": parent_href
        })
        if len(images) >= max_items:
            break

    # Download Links (Rapidgator & File Hosts)
    download_links = []
    all_links_sample = []
    seen_links = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("javascript:") or href in seen_links:
            continue
        seen_links.add(href)
        text = a.get_text(strip=True)

        is_dl = any(host in href.lower() for host in COMMON_DOWNLOAD_HOSTS) or any(
            kw in text.lower() for kw in ["download", "rapidgator", "mirror", "katfile", "nitroflare", "link"]
        )

        item = {"href": href, "text": text[:80]}
        if is_dl:
            download_links.append(item)
        elif len(all_links_sample) < 25:
            all_links_sample.append(item)

        if len(download_links) >= max_items:
            break

    # Container classes
    common_containers = []
    for el in soup.find_all(["div", "article", "section"], class_=True)[:30]:
        cls = " ".join(el.get("class", [])) if isinstance(el.get("class"), list) else str(el.get("class") or "")
        if cls and cls not in common_containers:
            common_containers.append(cls)

    # Human-readable structure digest for Gemini System Prompt
    digest_lines = [
        f"📄 Judul Halaman: {title or '(Tidak ada)'}",
        f"📝 Meta Deskripsi: {meta_desc or '(Tidak ada)'}",
        f"🏷️ Headings Utama: {', '.join(headings[:6]) or '(Tidak ada)'}",
        f"🖼️ Total Gambar Terdeteksi: {len(images)}",
        f"🔗 Total Link Download/Filehost: {len(download_links)}",
        f"📦 Kelas Kontainer Umum: {', '.join(common_containers[:8])}"
    ]

    return {
        "title": title,
        "description": meta_desc,
        "total_links": len(seen_links),
        "total_images": len(seen_img),
        "images": images,
        "download_links": download_links,
        "other_links": all_links_sample,
        "headings": headings,
        "structure_digest": "\n".join(digest_lines)
    }

def call_gemini_api(prompt_text: str, history: List[ChatMessage], api_key: str, model: Optional[str] = None) -> Tuple[str, str]:
    """Call Google Generative Language API with fallback support across reliable models."""
    key = api_key.strip() if api_key and api_key.strip() else DEFAULT_GEMINI_KEY

    models_to_try = [model] if model else PREFERRED_MODELS

    # Format history for Gemini API
    contents = []
    for msg in history:
        role = "user" if msg.role == "user" else "model"
        contents.append({
            "role": role,
            "parts": [{"text": msg.content}]
        })

    # Add current prompt
    contents.append({
        "role": "user",
        "parts": [{"text": prompt_text}]
    })

    last_error = ""
    for m in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
        payload = json.dumps({"contents": contents}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": key
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        return parts[0].get("text", ""), m
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            logger.warning("Gemini model %s failed: %d - %s", m, e.code, err_body[:150])
            last_error = f"{m} (HTTP {e.code}): {err_body}"
            continue
        except Exception as e:
            logger.warning("Gemini model %s request exception: %s", m, str(e))
            last_error = f"{m}: {str(e)}"
            continue

    raise Exception(f"Gagal menghubungi Google Gemini API. {last_error}")

def run_ai_chat(req: ChatRequest) -> Dict[str, Any]:
    """Orchestrate AI scraping assistant: extracts DOM metrics with BS4 and generates intelligent response."""
    user_msg = req.message.strip()
    dom_data = None
    html_sample = ""

    # 1. Process HTML if attached
    if req.html and req.html.strip():
        dom_data = extract_dom_summary(req.html)
        # Sample of raw HTML (truncated to first 25k chars for prompt efficiency)
        html_sample = req.html[:25000]

    # 2. Build Agent System Prompt with BeautifulSoup Knowledge
    system_context = [
        "Kamu adalah SoupAI, asisten web scraping dan analisis HTML cerdas yang tertanam langsung di aplikasi SoupCover.",
        "Keahlian utamamu: BeautifulSoup4 (bs4), Python web scraping, inspeksi DOM, penemuan link download (Rapidgator, Katfile, Nitroflare, dll.), dan ekstraksi cover majalah resolusi tinggi.",
        "",
        "PANDUAN MENJAWAB:",
        "1. Berikan jawaban yang ramah, ringkas, jelas, dan sangat praktis dalam bahasa Indonesia (atau sesuai bahasa pengguna).",
        "2. Jika pengguna meminta mencari gambar atau link, gunakan data DOM BeautifulSoup yang telah diekstrak di bawah ini.",
        "3. Jika pengguna meminta kode Python, tuliskan kode BeautifulSoup4 yang bersih, idiomatis, dan siap dijalankan.",
        "4. Tampilkan link atau hasil penting dalam format rapi (bullet list, tabel markdown, atau code block)."
    ]

    if dom_data:
        system_context.append("\n=== RINGKASAN DOM BEAUTIFULSOUP DARI HALAMAN INI ===")
        system_context.append(dom_data["structure_digest"])

        if dom_data["download_links"]:
            system_context.append("\n=== SAMPEL LINK DOWNLOAD / FILEHOST DITEMUKAN (BS4) ===")
            for dl in dom_data["download_links"][:20]:
                system_context.append(f"- [{dl['text'] or 'Download Link'}] -> {dl['href']}")

        if dom_data["images"]:
            system_context.append("\n=== SAMPEL GAMBAR TERDETEKSI (BS4) ===")
            for img in dom_data["images"][:15]:
                system_context.append(f"- Alt: '{img['alt']}', Dim: {img['dimensions']}, Src: {img['src']}")

        if html_sample:
            system_context.append("\n=== CUPLIKAN STRUKTUR HTML MENTAH (Awal Halaman) ===")
            system_context.append("```html\n" + html_sample[:8000] + "\n```")

    full_prompt = "\n".join(system_context) + f"\n\nPERTANYAAN PENGGUNA:\n{user_msg}"

    # 3. Call Gemini
    reply_text, model_used = call_gemini_api(
        prompt_text=full_prompt,
        history=req.history or [],
        api_key=req.api_key or DEFAULT_GEMINI_KEY,
        model=req.model
    )

    return {
        "reply": reply_text,
        "model": model_used,
        "has_dom_context": bool(dom_data),
        "extracted_summary": {
            "title": dom_data.get("title") if dom_data else "",
            "total_images": dom_data.get("total_images", 0) if dom_data else 0,
            "total_links": dom_data.get("total_links", 0) if dom_data else 0,
            "download_links_count": len(dom_data.get("download_links", [])) if dom_data else 0
        } if dom_data else None
    }
