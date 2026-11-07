import re
import io
import time
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse
import requests
from bs4 import BeautifulSoup
from PIL import Image

# User-Agent rotation to prevent anti-bot blocks
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
    t = re.sub(r'\s*[-–—|•:]\s*(WorldMags|Magzter|Issuu|Free Magazines|PDF Magazines|Magazine Lib|TruePDF|Downmagaz|Read Magazines|Download PDF|Online).*$', '', t, flags=re.IGNORECASE)
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
        # If removing size params leaves a valid query or empty
        new_query = urlencode(new_qs, doseq=True)
        transformed = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))

    return transformed

def probe_image_dimensions(img_url: str, session: requests.Session) -> Tuple[Optional[int], Optional[int], Optional[int], str]:
    """
    Probe image headers and first bytes to determine width, height, size in bytes, and format
    without downloading the entire large image file whenever possible.
    """
    headers = dict(DEFAULT_HEADERS)
    headers["Range"] = "bytes=0-65535"  # Read initial 64KB for image metadata
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
        headers = dict(DEFAULT_HEADERS)
        if custom_headers:
            headers.update(custom_headers)
        self.session.headers.update(headers)
        if custom_cookies:
            self.session.cookies.update(custom_cookies)

    def fetch_html(self, url: str) -> Tuple[str, str]:
        """Fetch page content, returning final URL and HTML text."""
        res = self.session.get(url, timeout=15)
        res.raise_for_status()
        return res.url, res.text

    def extract_candidates(
        self,
        target_url: str,
        custom_selectors: Optional[Dict[str, str]] = None,
        check_dimensions: bool = True
    ) -> Dict[str, Any]:
        """
        Scrape magazine covers and titles from a target URL.
        Supports automatic heuristic detection and custom CSS selector overrides.
        """
        final_url, html = self.fetch_html(target_url)
        soup = BeautifulSoup(html, "lxml")
        
        page_title = clean_text(soup.title.string if soup.title else "")
        results = []

        # 1. Check custom selectors if provided by user
        if custom_selectors and custom_selectors.get("item_container"):
            results = self._scrape_with_selectors(soup, final_url, custom_selectors)
        else:
            # 2. Heuristic multi-item / catalog extraction
            results = self._scrape_catalog_heuristics(soup, final_url)
            
            # 3. If catalog heuristics found nothing or page is a single issue / detail view
            if not results:
                single_item = self._scrape_single_issue(soup, final_url, page_title)
                if single_item:
                    results.append(single_item)

        # Enhance with high-res candidate checking and dimensions
        enhanced_results = []
        for idx, item in enumerate(results):
            high_res_url = item.get("high_res_url") or transform_to_high_res(item.get("image_url", ""))
            item["high_res_url"] = high_res_url
            
            # Attempt to verify high-res URL availability
            if high_res_url != item.get("image_url"):
                try:
                    head_res = self.session.head(high_res_url, timeout=4, allow_redirects=True)
                    if head_res.status_code != 200:
                        # Fallback to original image_url if transformed 404s
                        item["high_res_url"] = item.get("image_url")
                except Exception:
                    item["high_res_url"] = item.get("image_url")

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

            # Extract clean issue info if not already found
            if not item.get("issue"):
                item["issue"] = extract_issue_info(item.get("title", "")) or extract_issue_info(page_title)

            item["id"] = idx + 1
            enhanced_results.append(item)

        return {
            "source_url": final_url,
            "page_title": page_title,
            "total_items": len(enhanced_results),
            "items": enhanced_results
        }

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
        
        # 1. Check parent link to image (common pattern: <a href="full-cover.jpg"><img src="thumb.jpg"></a>)
        parent_a = img_tag.find_parent('a')
        if parent_a and parent_a.get('href'):
            href = parent_a['href'].strip()
            if any(href.lower().endswith(ext) or ext in href.lower() for ext in IMAGE_EXTENSIONS):
                return urljoin(base_url, href)

        # 2. Check srcset / data-srcset
        for attr in ('srcset', 'data-srcset', 'data-src-retina'):
            if img_tag.get(attr):
                best_srcset = parse_srcset_highest_res(img_tag[attr], base_url)
                if best_srcset:
                    return best_srcset

        # 3. Check high-res lazy attributes
        highres_attrs = [
            'data-zoom-image', 'data-highres', 'data-large', 'data-original',
            'data-full-image', 'data-full', 'data-orig-file', 'data-src-zoom',
            'data-src', 'data-lazy-src', 'data-image', 'src'
        ]
        for attr in highres_attrs:
            val = img_tag.get(attr)
            if val and not val.startswith('data:image'):
                return urljoin(base_url, val.strip())

        return None

    def _scrape_catalog_heuristics(self, soup: BeautifulSoup, base_url: str) -> List[Dict[str, Any]]:
        """Heuristically find magazine cover cards/items in grid/list pages."""
        items = []
        seen_images = set()

        # Potential card container selectors
        container_classes = [
            'article', 'figure', 'div.card', 'div.item', 'div.issue', 'div.cover',
            'div.magazine', 'div.post', 'li.product', 'div.product', 'div.entry',
            '.magazine-item', '.issue-card', '.book-item', '.cover-wrap', '.thumb'
        ]
        
        # Collect candidate elements
        elements = []
        for selector in container_classes:
            elements.extend(soup.select(selector))

        # Filter elements that contain both an image and some text
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
            for tag in ('h1', 'h2', 'h3', 'h4', 'h5', '.title', '.name', 'strong', 'a'):
                t_elem = el.select_one(tag)
                if t_elem:
                    t_text = clean_text(t_elem.get_text())
                    if len(t_text) > 2 and not any(skip in t_text.lower() for skip in ['read more', 'download', 'pdf', 'zoom']):
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

            if len(items) >= 60:  # Reasonable ceiling per scrape
                break

        return items

    def _scrape_single_issue(self, soup: BeautifulSoup, base_url: str, page_title: str) -> Optional[Dict[str, Any]]:
        """Extract primary cover image and magazine title from a single issue / detail page."""
        title = ""
        # 1. H1 title
        h1 = soup.find('h1')
        if h1:
            title = clean_magazine_title(h1.get_text())
        
        # 2. OpenGraph title
        if not title:
            og_title = soup.find('meta', property='og:title')
            if og_title and og_title.get('content'):
                title = clean_magazine_title(og_title['content'])

        if not title:
            title = clean_magazine_title(page_title)

        # Find cover image
        cover_img_url = None

        # 1. OpenGraph image (often high-res preview)
        og_image = soup.find('meta', property='og:image')
        if og_image and og_image.get('content'):
            cover_img_url = urljoin(base_url, og_image['content'].strip())

        # 2. Main content image or elements with 'cover' class
        if not cover_img_url:
            cover_candidates = soup.select(
                'img.cover, .cover img, [class*="cover"] img, [id*="cover"] img, article img, figure img, .main-image img'
            )
            for c in cover_candidates:
                cand = self._extract_best_img_url(c, base_url)
                if cand:
                    cover_img_url = cand
                    break

        # 3. First large image on page
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
