import os
import re
import io
import time
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse
import requests
from bs4 import BeautifulSoup
from PIL import Image

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp', '.avif', '.gif', '.bmp')

DEFAULT_SOFTARCHIVE_COOKIES = {
    "anon_id": "2556f19a97dd4449",
    "PHPSESSID": "5a388a6e4348647619a063ecc5105022",
    "flarum_remember": "ESiq8Qit95xaGL7fc16mBk6lGy7Hg1lKoHK7yKrx",
    "sa_remember": "40673e6bd28ccf6f.ae95e88dab7b279b5a187f9a32a9146e7e7238cb98707160883a20b6b33ef4ab",
    "sid": "ffdaf7958c01ef0b.2c7e680f185ed9254270f1644268e893c5bfd727d0a7dbd1bda2bfeab61a9d0c",
    "cf_clearance": "Cs5XYVZywYQm2s3pO66twM-vgWCZ9Dn6Rl4QKqeZyrAoNMMK8hf_Ys7DcukwssCNMtfqZtYOah-ojRglJpudsIiYgoDZVC1NF2C_y8neIuSbD_aF12o7ekGX5GqHmmFuZ52EMIVIhzMYO90FGYMpUyhNhlaqDNbtuw8FIHeZ5EceXrWckne4eNcK-77Hxd_GpyIuRHKUrgCHj1Ge93TiEgdoBFljprlekcPxNO0hhAFDoiwKOV1HeCmK1g7zhwNrpewy96spqAfmeJRWH_2LZe_C87z7Y4LZEsuIhzCQei9rlBEC3KvaIG7BOnfkwysqYNGb8YIR6_NRjHz0Lta9GOgwR31R9YWt_bcbwcDjmXIgY9Std4b25vGr9tf5MAVBK5Y"
}

COOKIE_STORE_PATHS = [
    "/tmp/softarchive_session.json",
    os.path.join(os.path.dirname(__file__), "softarchive_session.json"),
]

def get_active_softarchive_cookies() -> Dict[str, str]:
    """
    Get latest active cookies. Checks dynamic session storage files first,
    falling back to DEFAULT_SOFTARCHIVE_COOKIES.
    """
    import json
    for p in COOKIE_STORE_PATHS:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    stored = json.load(f)
                    if isinstance(stored, dict) and stored.get("cf_clearance"):
                        res = dict(DEFAULT_SOFTARCHIVE_COOKIES)
                        res.update(stored)
                        return res
            except Exception:
                pass
    return dict(DEFAULT_SOFTARCHIVE_COOKIES)

def extract_urls_from_text(text: str) -> List[str]:
    """
    Extract all HTTP/HTTPS URLs from any text string.
    Handles numbered lists (e.g. '1. https://...', '2.https://...'), markdown links, quotes, commas, etc.
    """
    if not text:
        return []
    matches = re.findall(r'https?://[^\s<>"\')]+', text)
    cleaned = []
    for m in matches:
        clean_url = re.sub(r'[\.,;]+$', '', m).strip()
        if clean_url and clean_url not in cleaned:
            cleaned.append(clean_url)
    return cleaned

def make_soup(html: str) -> BeautifulSoup:
    """Safely build BeautifulSoup tree with lxml or html.parser fallback."""
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")

def clean_text(text: Optional[str]) -> str:
    """Clean whitespace and common noise from scraped text."""
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def clean_magazine_title(raw_title: str) -> str:
    """Strip website branding and common spam keywords from magazine titles."""
    if not raw_title:
        return ""
    t = clean_text(raw_title)
    # Remove common site suffix like " - WorldMags", " | Magzter", " :: Read Free"
    t = re.sub(r'\s*[-–—|•:]\s*(WorldMags|Magzter|Issuu|SoftArchive|Sanet|Free Magazines|PDF Magazines|Magazine Lib|TruePDF|Downmagaz|Read Magazines|Download PDF|Online).*$', '', t, flags=re.IGNORECASE)
    # Remove "Free Download", "PDF", "True PDF" suffixes
    t = re.sub(r'\b(TruePDF|True\s*PDF|PDF|Free Download|Download|Full Issue)\b', '', t, flags=re.IGNORECASE)
    return t.strip()

def extract_issue_info(text: str) -> Optional[str]:
    """Detect issue number, volume, or publication date string."""
    if not text:
        return None
    patterns = [
        r'(?:Issue|No\.?|Vol(?:ume)?\.?|Edition)\s*#?\s*\d+(?:[\/\-]\d+)?',
        r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+(?:\d{1,2},?\s+)?\d{4}',
        r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}',
        r'(?:Spring|Summer|Autumn|Fall|Winter)\s+\d{4}',
        r'\b\d{4}[-\/]\d{1,2}\b'
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(0).strip()
    return None

def is_cloudflare_challenge(html: str, title: str) -> bool:
    """Detect if page returned is a Cloudflare / anti-bot challenge page."""
    indicators = [
        "attention required! | cloudflare",
        "quick security check",
        "just a moment...",
        "checking your browser before accessing",
        "enable cookies to continue",
        "cloudflare ray id",
        "challenge-platform",
        "cf-turnstile"
    ]
    check_text = (title + " " + html[:3000]).lower()
    return any(ind in check_text for ind in indicators)

def parse_srcset_highest_res(srcset_str: str, base_url: str) -> Optional[str]:
    """Extract highest resolution URL candidate from srcset string."""
    if not srcset_str:
        return None
    entries = []
    for item in srcset_str.split(','):
        parts = item.strip().split()
        if not parts:
            continue
        cand_url = parts[0]
        score = 0
        if len(parts) > 1:
            desc = parts[1].lower()
            if desc.endswith('w'):
                try:
                    score = int(desc[:-1])
                except ValueError:
                    score = 0
            elif desc.endswith('x'):
                try:
                    score = int(float(desc[:-1]) * 1000)
                except ValueError:
                    score = 0
        entries.append((score, cand_url))
    if entries:
        entries.sort(key=lambda x: x[0], reverse=True)
        return urljoin(base_url, entries[0][1])
    return None

def transform_to_high_res(img_url: str) -> str:
    """
    Transform thumbnail / resized URLs into potential full-resolution URLs.
    Handles WordPress, Shopify, Amazon, Cloudinary, Imgix, etc.
    """
    if not img_url:
        return img_url
    
    # 1. WordPress resized images (e.g. cover-300x420.jpg -> cover.jpg)
    transformed = re.sub(r'-\d{2,4}x\d{2,4}(?=\.(jpg|jpeg|png|webp|avif))', '', img_url, flags=re.IGNORECASE)
    
    # 2. Shopify thumb sizes (e.g. cover_medium.jpg -> cover_master.jpg or cover.jpg)
    transformed = re.sub(r'_(?:pico|icon|thumb|small|compact|medium|large|grande|1024x1024|2048x2048)(?=\.(jpg|jpeg|png|webp))', '_master', transformed, flags=re.IGNORECASE)
    
    # 3. Amazon image scaling (e.g. ._SX300_.jpg -> .jpg)
    transformed = re.sub(r'\._[A-Z0-9_,]+_\.(jpg|jpeg|png|webp)', r'.\1', transformed, flags=re.IGNORECASE)
    
    # 4. Common path heuristics: /thumb/ -> /covers/ or /large/
    for thumb_marker in ('/thumb/', '/thumbs/', '/thumbnail/', '/thumbnails/', '/small/'):
        if thumb_marker in transformed:
            transformed = transformed.replace(thumb_marker, '/large/')
            break
            
    # 5. Imgix / CDN query params removal (w=, h=, resize=, fit=)
    parsed = urlparse(transformed)
    if parsed.query:
        qs = parse_qs(parsed.query, keep_blank_values=True)
        keys_to_remove = {'w', 'width', 'h', 'height', 'resize', 'fit', 'max-w', 'max-h', 'size'}
        new_qs = {k: v for k, v in qs.items() if k.lower() not in keys_to_remove}
        new_query = urlencode(new_qs, doseq=True)
        transformed = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

    return transformed

def probe_image_dimensions(img_url: str, session: requests.Session) -> Tuple[Optional[int], Optional[int], Optional[int], str]:
    """
    Probe image headers and first bytes to determine width, height, size in bytes, and format
    without downloading the entire large image file whenever possible.
    """
    headers = dict(DEFAULT_HEADERS)
    headers["Range"] = "bytes=0-65535"
    try:
        r = session.get(img_url, headers=headers, timeout=5, stream=True)
        content_length = r.headers.get("Content-Range")
        total_size = None
        if content_length and '/' in content_length:
            try:
                total_size = int(content_length.split('/')[-1])
            except ValueError:
                total_size = None
        if not total_size and "Content-Length" in r.headers:
            try:
                total_size = int(r.headers["Content-Length"])
            except ValueError:
                total_size = None

        chunk = r.raw.read(65536)
        if chunk:
            try:
                img = Image.open(io.BytesIO(chunk))
                w, h = img.size
                return w, h, total_size, img.format or "UNKNOWN"
            except Exception:
                pass
    except Exception:
        pass
    return None, None, None, "UNKNOWN"

class MagazineScraper:
    def __init__(self, custom_headers: Optional[Dict[str, str]] = None, custom_cookies: Optional[Dict[str, str]] = None):
        self.session = requests.Session()
        self.headers = dict(DEFAULT_HEADERS)
        if custom_headers:
            self.headers.update(custom_headers)
        self.session.headers.update(self.headers)
        self.cookies = custom_cookies or {}
        if custom_cookies:
            self.session.cookies.update(custom_cookies)

    def fetch_html(self, url: str) -> Tuple[str, str]:
        """
        Fetch page content with browser impersonation.
        Uses curl_cffi when available to bypass Cloudflare TLS fingerprints.
        Automatically applies SoftArchive clearance cookies when target domain matches.
        """
        req_cookies = dict(self.cookies)
        domain = urlparse(url).netloc.lower()
        if any(h in domain for h in ["softarchive", "sanet"]):
            active_cookies = get_active_softarchive_cookies()
            for k, v in active_cookies.items():
                if k not in req_cookies:
                    req_cookies[k] = v

        # Try curl_cffi with chrome131 and chrome120 TLS impersonation first
        if HAS_CURL_CFFI:
            for imp in ["chrome131", "chrome124", "chrome120"]:
                try:
                    cffi_res = cffi_requests.get(
                        url,
                        headers=self.headers,
                        cookies=req_cookies,
                        impersonate=imp,
                        timeout=15
                    )
                    if cffi_res.status_code == 200:
                        # If not cloudflare challenge, return immediately
                        if not is_cloudflare_challenge(cffi_res.text, ""):
                            return cffi_res.url, cffi_res.text
                        # If it is cloudflare challenge, keep trying or fallback
                except Exception:
                    pass

        # Fallback to standard requests session
        res = self.session.get(url, cookies=req_cookies, headers=self.headers, timeout=15)
        return res.url, res.text

    def extract_from_html(
        self,
        html: str,
        base_url: str,
        custom_selectors: Optional[Dict[str, str]] = None,
        check_dimensions: bool = True
    ) -> Dict[str, Any]:
        """
        Extract magazine covers directly from raw HTML string.
        Enables pasting HTML from browser when Cloudflare blocks direct scraping.
        """
        soup = make_soup(html)
        page_title = clean_text(soup.title.string if soup.title else "")

        # Check for Cloudflare challenge
        is_blocked = is_cloudflare_challenge(html, page_title)
        if is_blocked:
            return {
                "source_url": base_url,
                "page_title": page_title,
                "total_items": 0,
                "items": [],
                "is_cloudflare_blocked": True,
                "warning": "Cloudflare / Bot Challenge detected on this page. If you are scraping a direct URL, please open the website in your browser, press Ctrl+U (View Source), copy the HTML, and paste it into the 'Paste HTML Source' tab in SoupCover."
            }

        results = []
        if custom_selectors and custom_selectors.get("item_container"):
            results = self._scrape_with_selectors(soup, base_url, custom_selectors)
        else:
            results = self._scrape_catalog_heuristics(soup, base_url)
            if not results:
                single_item = self._scrape_single_issue(soup, base_url, page_title)
                if single_item:
                    results.append(single_item)

        enhanced_results = []
        for idx, item in enumerate(results):
            high_res_url = item.get("high_res_url") or transform_to_high_res(item.get("image_url", ""))
            item["high_res_url"] = high_res_url
            
            # Check dimensions if requested
            if check_dimensions and item.get("high_res_url"):
                w, h, size_bytes, fmt = probe_image_dimensions(item["high_res_url"], self.session)
                if w and h:
                    item["width"] = w
                    item["height"] = h
                    item["resolution"] = f"{w} × {h}"
                    item["aspect_ratio"] = round(h / w, 2) if w > 0 else 1.0
                    item["is_portrait"] = h > w
                    item["is_high_res"] = (w >= 1000 or h >= 1400)
                if size_bytes:
                    item["file_size_bytes"] = size_bytes
                    item["file_size_formatted"] = f"{round(size_bytes / 1024, 1)} KB" if size_bytes < 1048576 else f"{round(size_bytes / 1048576, 2)} MB"
                item["format"] = fmt

            if not item.get("issue"):
                item["issue"] = extract_issue_info(item.get("title", "")) or extract_issue_info(page_title)

            item["id"] = idx + 1
            enhanced_results.append(item)

        return {
            "source_url": base_url,
            "page_title": page_title,
            "total_items": len(enhanced_results),
            "items": enhanced_results,
            "is_cloudflare_blocked": False
        }

    def extract_candidates(
        self,
        target_url: str,
        custom_selectors: Optional[Dict[str, str]] = None,
        check_dimensions: bool = True
    ) -> Dict[str, Any]:
        """Scrape magazine covers and titles from a target URL."""
        final_url, html = self.fetch_html(target_url)
        return self.extract_from_html(
            html=html,
            base_url=final_url,
            custom_selectors=custom_selectors,
            check_dimensions=check_dimensions
        )

    def _scrape_with_selectors(self, soup: BeautifulSoup, base_url: str, selectors: Dict[str, str]) -> List[Dict[str, Any]]:
        """Scrape items using explicit user-defined CSS selectors."""
        items = []
        container_sel = selectors.get("item_container", "")
        title_sel = selectors.get("title", "")
        img_sel = selectors.get("image", "")
        issue_sel = selectors.get("issue", "")

        containers = soup.select(container_sel)
        for c in containers:
            title = ""
            if title_sel:
                t_el = c.select_one(title_sel)
                if t_el:
                    title = clean_magazine_title(t_el.get_text())
            
            img_url = ""
            if img_sel:
                img_el = c.select_one(img_sel)
                if img_el:
                    img_url = self._extract_best_img_url(img_el, base_url)

            issue = ""
            if issue_sel:
                iss_el = c.select_one(issue_sel)
                if iss_el:
                    issue = clean_text(iss_el.get_text())

            if img_url:
                items.append({
                    "title": title or "Magazine Issue",
                    "image_url": img_url,
                    "high_res_url": transform_to_high_res(img_url),
                    "issue": issue or extract_issue_info(title)
                })
        return items

    def _extract_best_img_url(self, img_tag: Any, base_url: str) -> Optional[str]:
        """Extract best resolution image from tag attributes (srcset, data-src, src, zoom, parent <a>)."""
        if not img_tag:
            return None
        
        # 1. Check srcset / data-srcset first (highest resolution)
        for attr in ('srcset', 'data-srcset', 'data-src-retina'):
            if img_tag.get(attr):
                best_srcset = parse_srcset_highest_res(img_tag[attr], base_url)
                if best_srcset:
                    return best_srcset

        # 2. Check high-res lazy attributes
        highres_attrs = [
            'data-zoom-image', 'data-highres', 'data-large', 'data-original',
            'data-full-image', 'data-full', 'data-orig-file', 'data-src-zoom',
            'data-src', 'data-lazy-src', 'data-echo', 'data-image', 'src'
        ]
        for attr in highres_attrs:
            val = img_tag.get(attr)
            if val and not val.startswith('data:image'):
                return urljoin(base_url, val.strip())

        # 3. Check parent link to image (only if href is an actual image file, not an HTML page)
        parent_a = img_tag.find_parent('a')
        if parent_a and parent_a.get('href'):
            href = parent_a['href'].strip()
            parsed_path = urlparse(href).path.lower()
            if any(parsed_path.endswith(ext) for ext in IMAGE_EXTENSIONS) and not any(p in href.lower() for p in ['/wiki/file:', '/attachment/', '.html', '.php']):
                return urljoin(base_url, href)

        return None

    def _scrape_catalog_heuristics(self, soup: BeautifulSoup, base_url: str) -> List[Dict[str, Any]]:
        """Heuristically find magazine cover cards/items in grid/list pages."""
        items = []
        seen_images = set()

        # Potential card container selectors (includes SoftArchive, Sanet, WordPress, Shopify, Magzter)
        container_classes = [
            'article', 'figure', 'div.cell', 'div.story', 'div.news', 'div.short_story',
            'div.card', 'div.item', 'div.issue', 'div.cover', 'div.magazine', 'div.post',
            'li.product', 'div.product', 'div.entry', '.magazine-item', '.issue-card',
            '.book-item', '.cover-wrap', '.thumb', '.post_box', '.list-item', '.grid-item'
        ]
        
        elements = []
        for selector in container_classes:
            elements.extend(soup.select(selector))

        for el in elements:
            img = el.find('img')
            if not img:
                continue

            img_url = self._extract_best_img_url(img, base_url)
            if not img_url or img_url in seen_images:
                continue

            # Ignore tiny icons / logos / avatars
            width = img.get('width')
            height = img.get('height')
            if width and height:
                try:
                    if int(width) < 80 or int(height) < 80:
                        continue
                except ValueError:
                    pass

            # Extract title: look for heading or link text inside the element
            title = ""
            for tag in ('h1', 'h2.title', '.title', 'h2', 'h3', 'h4', '.cell_title', '.story_title', '.name', 'strong', 'a'):
                t_elem = el.select_one(tag)
                if t_elem:
                    t_text = clean_text(t_elem.get_text())
                    if len(t_text) > 2 and not any(skip in t_text.lower() for skip in ['read more', 'download', 'pdf', 'zoom', 'comments']):
                        title = clean_magazine_title(t_text)
                        break

            # Fallback title from img alt
            if not title and img.get('alt'):
                alt_text = clean_text(img['alt'])
                if len(alt_text) > 3 and not any(skip in alt_text.lower() for skip in ['logo', 'icon', 'cover']):
                    title = clean_magazine_title(alt_text)

            if not title:
                title = clean_text(img.get('alt', '')) or "Magazine Issue"

            seen_images.add(img_url)
            items.append({
                "title": title,
                "image_url": img_url,
                "high_res_url": transform_to_high_res(img_url),
                "issue": extract_issue_info(title)
            })

            if len(items) >= 80:  # Allow up to 80 items per scrape
                break

        # Universal fallback: if specific container classes didn't match, inspect all content images
        if not items:
            for img in soup.find_all('img'):
                cand = self._extract_best_img_url(img, base_url)
                if not cand or cand in seen_images:
                    continue
                if any(skip in cand.lower() for skip in ['logo', 'icon', 'avatar', 'badge', 'banner', 'button', 'spinner', 'advertisement']):
                    continue
                
                # Check dimensions if present on tag
                width = img.get('width')
                height = img.get('height')
                if width and height:
                    try:
                        if int(width) < 60 or int(height) < 60:
                            continue
                    except ValueError:
                        pass

                # Walk up DOM tree to find closest title/heading
                curr = img
                card_title = ""
                for _ in range(6):
                    curr = curr.parent
                    if not curr or curr.name in ['body', 'html']:
                        break
                    for tag in ('h1', 'h2', 'h3', 'h4', '.title', '.name', 'strong', 'a'):
                        t_el = curr.select_one(tag)
                        if t_el:
                            t_text = clean_text(t_el.get_text())
                            if len(t_text) > 4 and not any(s in t_text.lower() for s in ['search', 'menu', 'login', 'read more', 'categories', 'download']):
                                card_title = clean_magazine_title(t_text)
                                break
                    if card_title:
                        break

                if not card_title and img.get('alt'):
                    alt = clean_text(img['alt'])
                    if len(alt) > 3 and not any(s in alt.lower() for s in ['logo', 'icon', 'cover']):
                        card_title = clean_magazine_title(alt)

                if not card_title:
                    card_title = "Magazine Issue"

                seen_images.add(cand)
                items.append({
                    "title": card_title,
                    "image_url": cand,
                    "high_res_url": transform_to_high_res(cand),
                    "issue": extract_issue_info(card_title)
                })
                if len(items) >= 80:
                    break

        return items

    def _scrape_single_issue(self, soup: BeautifulSoup, base_url: str, page_title: str) -> Optional[Dict[str, Any]]:
        """Extract primary cover image and magazine title from a single issue / detail page."""
        title = ""
        h1 = soup.find('h1')
        if h1:
            title = clean_magazine_title(h1.get_text())
        
        if not title:
            og_title = soup.find('meta', property='og:title')
            if og_title and og_title.get('content'):
                title = clean_magazine_title(og_title['content'])

        if not title:
            title = clean_magazine_title(page_title)

        cover_img_url = None
        og_image = soup.find('meta', property='og:image')
        if og_image and og_image.get('content'):
            cover_img_url = urljoin(base_url, og_image['content'].strip())

        if not cover_img_url:
            cover_candidates = soup.select(
                'img.cover, .cover img, [class*="cover"] img, [id*="cover"] img, article img, figure img, .main-image img'
            )
            for c in cover_candidates:
                cand = self._extract_best_img_url(c, base_url)
                if cand:
                    cover_img_url = cand
                    break

        if not cover_img_url:
            for img in soup.find_all('img'):
                cand = self._extract_best_img_url(img, base_url)
                if cand and not any(no in cand.lower() for no in ['logo', 'icon', 'banner', 'avatar', 'badge']):
                    cover_img_url = cand
                    break

        if cover_img_url:
            return {
                "title": title or "Magazine Title",
                "image_url": cover_img_url,
                "high_res_url": transform_to_high_res(cover_img_url),
                "issue": extract_issue_info(title) or extract_issue_info(page_title)
            }
        return None

# ==========================================
# Rapidgator & Filehost Extractor Features
# ==========================================

from urllib.parse import unquote

def extract_filehost_links(html_text: str, soup: Optional[BeautifulSoup] = None) -> Dict[str, Any]:
    """
    Extract Rapidgator, DDownload, Nitroflare, Katfile, Turbobit, and other filehost links
    from HTML text and BeautifulSoup DOM.
    """
    rapidgator_links = set()
    ddownload_links = set()
    nitroflare_links = set()
    katfile_links = set()
    turbobit_links = set()
    rosefile_links = set()
    fikper_links = set()
    other_links = set()

    # 1. Regex search across raw HTML for direct Rapidgator URLs
    rg_matches = re.findall(
        r'https?://(?:www\.)?(?:rapidgator\.net|rg\.to)/file/[a-zA-Z0-9_\-\./]+(?:\.html)?',
        html_text,
        re.IGNORECASE
    )
    for m in rg_matches:
        rapidgator_links.add(m.rstrip('.'))

    # 2. Direct regex for DDownload
    ddl_matches = re.findall(
        r'https?://(?:www\.)?(?:ddownload\.com|ddl\.to)/[a-zA-Z0-9_\-\./]+',
        html_text,
        re.IGNORECASE
    )
    for m in ddl_matches:
        ddownload_links.add(m.rstrip('.'))

    # 3. Direct regex for Nitroflare
    nf_matches = re.findall(
        r'https?://(?:www\.)?nitroflare\.com/view/[a-zA-Z0-9_\-\./]+',
        html_text,
        re.IGNORECASE
    )
    for m in nf_matches:
        nitroflare_links.add(m.rstrip('.'))

    # 4. Direct regex for Katfile
    kf_matches = re.findall(
        r'https?://(?:www\.)?katfile\.com/[a-zA-Z0-9_\-\./]+',
        html_text,
        re.IGNORECASE
    )
    for m in kf_matches:
        katfile_links.add(m.rstrip('.'))

    # 5. Direct regex for Turbobit
    tb_matches = re.findall(
        r'https?://(?:www\.)?(?:turbobit\.net|turb\.to)/[a-zA-Z0-9_\-\./]+(?:\.html)?',
        html_text,
        re.IGNORECASE
    )
    for m in tb_matches:
        turbobit_links.add(m.rstrip('.'))

    # 6. Direct regex for Rosefile & Fikper
    rf_matches = re.findall(r'https?://(?:www\.)?rosefile\.net/[a-zA-Z0-9_\-\./]+', html_text, re.IGNORECASE)
    for m in rf_matches:
        rosefile_links.add(m.rstrip('.'))

    fp_matches = re.findall(r'https?://(?:www\.)?fikper\.com/[a-zA-Z0-9_\-\./]+', html_text, re.IGNORECASE)
    for m in fp_matches:
        fikper_links.add(m.rstrip('.'))

    # 7. Check encoded redirect links (e.g. /redirect?url=https%3A%2F%2Frapidgator...)
    redirect_matches = re.findall(
        r'(?:url|link|target|goto|href)=(https%3A%2F%2F[^&"\'\s>]+)',
        html_text,
        re.IGNORECASE
    )
    for r in redirect_matches:
        try:
            unquoted = unquote(r).rstrip('.')
            low_u = unquoted.lower()
            if 'rapidgator.net/file/' in low_u or 'rg.to/file/' in low_u:
                rapidgator_links.add(unquoted)
            elif 'ddownload.com/' in low_u or 'ddl.to/' in low_u:
                ddownload_links.add(unquoted)
            elif 'nitroflare.com/view/' in low_u:
                nitroflare_links.add(unquoted)
            elif 'katfile.com/' in low_u:
                katfile_links.add(unquoted)
            elif 'turbobit.net/' in low_u or 'turb.to/' in low_u:
                turbobit_links.add(unquoted)
        except Exception:
            pass

    # 8. Check anchor tags and data attributes if soup is available
    if soup:
        for a in soup.find_all('a'):
            href = a.get('href', '').strip()
            # Discard non-download links like FAQ or reseller purchase
            if '/faq/' in href.lower() or 'buying-a-premium' in href.lower():
                continue

            low_href = href.lower()
            if 'rapidgator.net/file/' in low_href or 'rg.to/file/' in low_href:
                rapidgator_links.add(href.rstrip('.'))
            elif 'ddownload.com/' in low_href or 'ddl.to/' in low_href:
                ddownload_links.add(href.rstrip('.'))
            elif 'nitroflare.com/view/' in low_href:
                nitroflare_links.add(href.rstrip('.'))
            elif 'katfile.com/' in low_href:
                katfile_links.add(href.rstrip('.'))
            elif 'turbobit.net/' in low_href or 'turb.to/' in low_href:
                turbobit_links.add(href.rstrip('.'))
            elif 'rosefile.net/' in low_href:
                rosefile_links.add(href.rstrip('.'))
            elif 'fikper.com/' in low_href:
                fikper_links.add(href.rstrip('.'))

            # Data attributes
            for attr in ('data-url', 'data-href', 'data-link', 'data-target'):
                dval = a.get(attr, '').strip()
                low_dval = dval.lower()
                if '/faq/' in low_dval or 'buying-a-premium' in low_dval:
                    continue
                if 'rapidgator.net/file/' in low_dval or 'rg.to/file/' in low_dval:
                    rapidgator_links.add(dval.rstrip('.'))
                elif 'ddownload.com/' in low_dval or 'ddl.to/' in low_dval:
                    ddownload_links.add(dval.rstrip('.'))
                elif 'nitroflare.com/view/' in low_dval:
                    nitroflare_links.add(dval.rstrip('.'))
                elif 'katfile.com/' in low_dval:
                    katfile_links.add(dval.rstrip('.'))
                elif 'turbobit.net/' in low_dval or 'turb.to/' in low_dval:
                    turbobit_links.add(dval.rstrip('.'))

    all_links = sorted(list(
        rapidgator_links | ddownload_links | nitroflare_links | katfile_links | turbobit_links | rosefile_links | fikper_links | other_links
    ))

    return {
        "rapidgator": sorted(list(rapidgator_links)),
        "ddownload": sorted(list(ddownload_links)),
        "nitroflare": sorted(list(nitroflare_links)),
        "katfile": sorted(list(katfile_links)),
        "turbobit": sorted(list(turbobit_links)),
        "rosefile": sorted(list(rosefile_links)),
        "fikper": sorted(list(fikper_links)),
        "other": sorted(list(other_links)),
        "all_links": all_links
    }

def extract_page_file_size(text: str) -> Optional[str]:
    """Extract file size string (e.g. 26 MB, 1.2 GB, 117 MB)."""
    m = re.search(r'\b(\d+(?:\.\d+)?\s*(?:GB|MB|KB))\b', text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None

def extract_single_magazine_rapidgator(html: str, url: str) -> Dict[str, Any]:
    """Parse single magazine page for Title, Rapidgator link, and metadata."""
    soup = make_soup(html)
    page_title = clean_text(soup.title.string if soup.title else "")

    # Extract title
    title = ""
    h1 = soup.find('h1')
    if h1:
        title = clean_magazine_title(h1.get_text())
    if not title:
        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content'):
            title = clean_magazine_title(og_title['content'])
    if not title:
        title = clean_magazine_title(page_title)

    # Extract links
    filehosts = extract_filehost_links(html, soup)
    rg_links = filehosts.get("rapidgator", [])
    primary_rg = rg_links[0] if rg_links else ""
    ddl_links = filehosts.get("ddownload", [])
    primary_ddl = ddl_links[0] if ddl_links else ""
    nf_links = filehosts.get("nitroflare", [])
    primary_nf = nf_links[0] if nf_links else ""
    kf_links = filehosts.get("katfile", [])
    primary_kf = kf_links[0] if kf_links else ""
    tb_links = filehosts.get("turbobit", [])
    primary_tb = tb_links[0] if tb_links else ""
    all_links = filehosts.get("all_links", [])

    # Extract file size
    file_size = extract_page_file_size(soup.get_text()[:4000]) or ""

    hosts_found = [h for h, l in [("RG", rg_links), ("DDL", ddl_links), ("NF", nf_links), ("KF", kf_links), ("TB", tb_links)] if l]

    return {
        "title": title or "Magazine Title",
        "rapidgator_url": primary_rg,
        "rapidgator_links": rg_links,
        "ddownload_url": primary_ddl,
        "ddownload_links": ddl_links,
        "nitroflare_url": primary_nf,
        "nitroflare_links": nf_links,
        "katfile_url": primary_kf,
        "katfile_links": kf_links,
        "turbobit_url": primary_tb,
        "turbobit_links": tb_links,
        "all_links": all_links,
        "hosts_found": hosts_found,
        "file_size": file_size,
        "source_url": url,
        "status": "found" if (primary_rg or primary_ddl or all_links) else "not_found"
    }

def extract_entries_from_html(html: str, base_url: str) -> List[Dict[str, Any]]:
    """
    Extract magazine entries from raw HTML (whether a single detail page or a multi-entry catalog page).
    If the page has multiple .cell / .story / .post elements, extract title and filehost links for each.
    """
    soup = make_soup(html)
    containers = soup.select('div.cell, div.story, div.news, div.short_story, div.card, div.post, article')
    items = []

    if len(containers) > 1:
        for idx, c in enumerate(containers):
            title = ""
            detail_link = ""
            for tag in ('h2 a', '.cell_title a', '.title a', 'h2', 'h1', 'h3', '.title'):
                t_el = c.select_one(tag)
                if t_el:
                    title = clean_magazine_title(t_el.get_text())
                    if t_el.name == 'a' and t_el.get('href'):
                        detail_link = urljoin(base_url, t_el['href'])
                    break
            
            c_html = str(c)
            links_data = extract_filehost_links(c_html, c)
            rg_list = links_data.get("rapidgator", [])
            primary_rg = rg_list[0] if rg_list else ""
            ddl_list = links_data.get("ddownload", [])
            primary_ddl = ddl_list[0] if ddl_list else ""
            nf_list = links_data.get("nitroflare", [])
            kf_list = links_data.get("katfile", [])
            tb_list = links_data.get("turbobit", [])
            all_links = links_data.get("all_links", [])
            file_size = extract_page_file_size(c.get_text()) or ""

            hosts_found = [h for h, l in [("RG", rg_list), ("DDL", ddl_list), ("NF", nf_list), ("KF", kf_list), ("TB", tb_list)] if l]

            if title:
                items.append({
                    "id": idx + 1,
                    "title": title,
                    "rapidgator_url": primary_rg,
                    "rapidgator_links": rg_list,
                    "ddownload_url": primary_ddl,
                    "ddownload_links": ddl_list,
                    "nitroflare_url": nf_list[0] if nf_list else "",
                    "nitroflare_links": nf_list,
                    "katfile_url": kf_list[0] if kf_list else "",
                    "katfile_links": kf_list,
                    "turbobit_url": tb_list[0] if tb_list else "",
                    "turbobit_links": tb_list,
                    "all_links": all_links,
                    "hosts_found": hosts_found,
                    "file_size": file_size,
                    "source_url": detail_link or base_url,
                    "status": "found" if (primary_rg or primary_ddl or all_links) else ("has_detail_link" if detail_link else "not_found")
                })
        if items:
            return items

    single = extract_single_magazine_rapidgator(html, base_url)
    single["id"] = 1
    return [single]
