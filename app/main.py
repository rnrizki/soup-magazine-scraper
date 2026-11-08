import os
import io
import zipfile
import re
import concurrent.futures
from typing import List, Dict, Optional, Any, Tuple
from urllib.parse import urlparse
import requests
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

from bs4 import BeautifulSoup
from app.scraper import (
    MagazineScraper, clean_magazine_title, clean_text, DEFAULT_HEADERS,
    extract_single_magazine_rapidgator, extract_entries_from_html,
    is_cloudflare_challenge
)
from app.ai_chat import (
    ChatRequest, SelectorQueryRequest, run_ai_chat,
    DEFAULT_GEMINI_KEY, extract_dom_summary, call_gemini_api
)

app = FastAPI(
    title="SoupCover - High-Res Magazine Cover Scraper",
    description="Beautiful Soup + Requests powered GUI application for scraping high-res magazine covers and titles",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Models
class ScrapeRequest(BaseModel):
    url: str
    custom_selectors: Optional[Dict[str, str]] = None
    custom_headers: Optional[Dict[str, str]] = None
    custom_cookies: Optional[Dict[str, str]] = None
    check_dimensions: bool = True

class ScrapeHtmlRequest(BaseModel):
    html: str
    base_url: Optional[str] = "https://softarchive.download"
    custom_selectors: Optional[Dict[str, str]] = None
    check_dimensions: bool = True

class MultiScrapeRequest(BaseModel):
    urls: List[str]
    check_dimensions: bool = True

class ZipDownloadItem(BaseModel):
    url: Optional[str] = None
    high_res_url: Optional[str] = None
    image_url: Optional[str] = None
    title: Optional[str] = "Magazine Cover"
    filename: Optional[str] = None
    issue: Optional[str] = None
    id: Optional[Any] = None

class ZipDownloadRequest(BaseModel):
    items: List[ZipDownloadItem]

class BulkRapidgatorRequest(BaseModel):
    urls: Optional[List[str]] = None
    raw_html: Optional[str] = None
    base_url: Optional[str] = "https://softarchive.download"
    custom_cookies: Optional[Dict[str, str]] = None

# Health check
@app.api_route("/api/health", methods=["GET", "HEAD"])
def health():
    return {"status": "ok", "app": "soup-magazine-scraper", "version": "1.0.0"}

# ==========================================
# AI CHAT ASSISTANT (BEAUTIFULSOUP + GEMINI)
# ==========================================
@app.post("/api/chat")
def chat_with_ai(req: ChatRequest):
    try:
        result = run_ai_chat(req)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/chat/dom-summary")
def get_dom_summary(req: ScrapeHtmlRequest):
    try:
        summary = extract_dom_summary(req.html)
        return summary
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Gagal memproses HTML: {str(e)}")

@app.post("/api/chat/validate-key")
def validate_gemini_key(payload: Dict[str, str]):
    key = payload.get("api_key", "").strip() or DEFAULT_GEMINI_KEY
    try:
        reply, model = call_gemini_api("Jawab dengan kata 'OK' untuk verifikasi status.", [], key)
        return {"status": "valid", "model": model, "reply": reply.strip()}
    except Exception as e:
        return {"status": "invalid", "error": str(e)}

# Scrape from direct URL
@app.post("/api/scrape")
def scrape_url(req: ScrapeRequest):
    url = req.url.strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    try:
        scraper = MagazineScraper(
            custom_headers=req.custom_headers,
            custom_cookies=req.custom_cookies
        )
        data = scraper.extract_candidates(
            target_url=url,
            custom_selectors=req.custom_selectors,
            check_dimensions=req.check_dimensions
        )
        return data
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch website: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scraping error: {str(e)}")

# Scrape from pasted raw HTML (bypasses Cloudflare / anti-bot challenges)
@app.post("/api/scrape-html")
def scrape_html(req: ScrapeHtmlRequest):
    if not req.html or len(req.html.strip()) < 10:
        raise HTTPException(status_code=400, detail="HTML content is empty or too short")

    base_url = (req.base_url or "https://softarchive.download").strip()
    if not base_url.startswith("http://") and not base_url.startswith("https://"):
        base_url = "https://" + base_url

    try:
        scraper = MagazineScraper()
        data = scraper.extract_from_html(
            html=req.html,
            base_url=base_url,
            custom_selectors=req.custom_selectors,
            check_dimensions=req.check_dimensions
        )
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"HTML parsing error: {str(e)}")

# Scrape multiple URLs
@app.post("/api/scrape/multi")
def scrape_multi(req: MultiScrapeRequest):
    scraper = MagazineScraper()
    all_results = []
    
    for u in req.urls[:20]:
        url = u.strip()
        if not url:
            continue
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        try:
            res = scraper.extract_candidates(target_url=url, check_dimensions=req.check_dimensions)
            all_results.append(res)
        except Exception as e:
            all_results.append({
                "source_url": url,
                "error": str(e),
                "total_items": 0,
                "items": []
            })
    return {"results": all_results}

# Image Proxy to bypass hotlink and CORS protection with optional download attachment
@app.get("/api/proxy-image")
def proxy_image(
    url: str = Query(..., description="Target image URL to proxy"),
    download: bool = Query(False, description="Trigger download as attachment"),
    filename: Optional[str] = Query(None, description="Filename for download")
):
    try:
        parsed = urlparse(url)
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        headers = dict(DEFAULT_HEADERS)
        headers["Referer"] = referer
        headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
        
        req = requests.get(url, headers=headers, stream=True, timeout=15)
        if req.status_code != 200:
            raise HTTPException(status_code=req.status_code, detail="Remote image returned non-200 status")
            
        content_type = req.headers.get("Content-Type", "image/jpeg")
        resp_headers = {
            "Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*"
        }

        if download:
            clean_name = filename or os.path.basename(parsed.path) or "magazine_cover.jpg"
            clean_name = re.sub(r'[^\w\-\. ]', '_', clean_name)
            if not os.path.splitext(clean_name)[1]:
                clean_name += ".jpg"
            resp_headers["Content-Disposition"] = f'attachment; filename="{clean_name}"'

        return StreamingResponse(
            req.iter_content(chunk_size=16384),
            media_type=content_type,
            headers=resp_headers
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Proxy error: {str(e)}")

# Download All as ZIP (Parallel multithreaded fetching)
@app.post("/api/download/zip")
def download_zip(payload: ZipDownloadRequest):
    if not payload.items:
        raise HTTPException(status_code=400, detail="No items to download")

    def fetch_item(idx: int, item: ZipDownloadItem) -> Optional[Tuple[str, bytes]]:
        img_url = item.url or item.high_res_url or item.image_url
        if not img_url:
            return None
        try:
            parsed = urlparse(img_url)
            referer = f"{parsed.scheme}://{parsed.netloc}/"
            headers = dict(DEFAULT_HEADERS)
            headers["Referer"] = referer
            
            resp = requests.get(img_url, headers=headers, timeout=12)
            if resp.status_code == 200 and resp.content:
                safe_title = re.sub(r'[^\w\-_\. ]', '_', item.title or f"cover_{idx+1}").strip()
                if not safe_title:
                    safe_title = f"cover_{idx+1}"
                
                ext = os.path.splitext(parsed.path)[1].lower()
                if ext not in ['.jpg', '.jpeg', '.png', '.webp', '.avif']:
                    ct = resp.headers.get("Content-Type", "").lower()
                    if "png" in ct:
                        ext = ".png"
                    elif "webp" in ct:
                        ext = ".webp"
                    elif "avif" in ct:
                        ext = ".avif"
                    else:
                        ext = ".jpg"
                filename = f"{idx+1:02d}_{safe_title[:60]}{ext}"
                return filename, resp.content
        except Exception:
            pass
        return None

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        items_to_process = [i for i in payload.items if (i.url or i.high_res_url or i.image_url)][:60]
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            future_to_idx = {
                executor.submit(fetch_item, idx, item): idx 
                for idx, item in enumerate(items_to_process)
            }
            saved_count = 0
            for future in concurrent.futures.as_completed(future_to_idx):
                res = future.result()
                if res:
                    fname, fcontent = res
                    zf.writestr(fname, fcontent)
                    saved_count += 1

        if saved_count == 0:
            zf.writestr("README.txt", "No images could be retrieved from the provided URLs.")

    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="magazine_covers_highres.zip"',
            "Access-Control-Expose-Headers": "Content-Disposition"
        }
    )

# Bulk Rapidgator & Filehost Links Extractor
@app.post("/api/bulk-rapidgator")
def bulk_rapidgator(req: BulkRapidgatorRequest):
    # Case 1: Raw HTML provided
    if req.raw_html and len(req.raw_html.strip()) > 10:
        base_url = (req.base_url or "https://softarchive.download").strip()
        items = extract_entries_from_html(req.raw_html, base_url)
        return {
            "mode": "html",
            "total_items": len(items),
            "found_links_count": sum(1 for i in items if i.get("rapidgator_url")),
            "items": items
        }

    # Case 2: List of URLs provided
    if not req.urls:
        raise HTTPException(status_code=400, detail="Please provide URLs or paste raw HTML")

    clean_urls = []
    for u in req.urls:
        u_str = u.strip()
        if u_str:
            if not u_str.startswith("http://") and not u_str.startswith("https://"):
                u_str = "https://" + u_str
            clean_urls.append(u_str)

    if not clean_urls:
        raise HTTPException(status_code=400, detail="No valid URLs provided")

    scraper = MagazineScraper(custom_cookies=req.custom_cookies)
    results = []

    def process_url(idx: int, target_url: str) -> Dict[str, Any]:
        try:
            final_url, html = scraper.fetch_html(target_url)
            soup = BeautifulSoup(html, "lxml")
            page_title = clean_text(soup.title.string if soup.title else "")
            
            if is_cloudflare_challenge(html, page_title):
                return {
                    "id": idx + 1,
                    "title": page_title or "Cloudflare Verification",
                    "rapidgator_url": "",
                    "rapidgator_links": [],
                    "nitroflare_links": [],
                    "file_size": "",
                    "source_url": target_url,
                    "status": "cloudflare_blocked",
                    "error": "Cloudflare security check blocked direct server fetch"
                }

            data = extract_single_magazine_rapidgator(html, final_url)
            data["id"] = idx + 1
            return data
        except Exception as e:
            return {
                "id": idx + 1,
                "title": "Failed to fetch",
                "rapidgator_url": "",
                "rapidgator_links": [],
                "nitroflare_links": [],
                "file_size": "",
                "source_url": target_url,
                "status": "error",
                "error": str(e)
            }

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_to_url = {
            executor.submit(process_url, idx, url): idx 
            for idx, url in enumerate(clean_urls[:50])
        }
        for future in concurrent.futures.as_completed(future_to_url):
            try:
                res = future.result()
                results.append(res)
            except Exception:
                pass

    results.sort(key=lambda x: x.get("id", 0))

    return {
        "mode": "urls",
        "total_items": len(results),
        "found_links_count": sum(1 for i in results if i.get("rapidgator_url")),
        "items": results
    }

# Serve Web GUI Frontend
STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def index_page():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>SoupCover Scraper is running! Place index.html in /static</h1>")
