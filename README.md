# SoupCover · High-Res Magazine Cover & Title Scraper

A web GUI application powered by **Beautiful Soup 4** and **Requests** designed specifically for scraping high-resolution magazine cover images, publication titles, issue metadata, and publication dates from any website.

Hosted live at: **[https://soup.lmrei.id](https://soup.lmrei.id)**

---

## 🌟 Features

- **Automated Magazine Cover Detection**:
  - Heuristic analysis targeting portrait-oriented publication covers (ratio ~1:1.3 to 1:1.5).
  - Inspects `srcset`, `data-highres`, `data-zoom-image`, `data-original`, and `data-full-image` attributes to pick the highest resolution image available.
  - De-thumbing algorithms for WordPress, Shopify, Amazon, Cloudinary, and Imgix image CDNs (strips `-300x450` resized suffixes to access full originals).
- **Magazine Name & Issue Extraction**:
  - Automatically parses publication titles, issue numbers (e.g. *Issue #45*, *Vol. 12*), and publication months/years.
  - Cleans site branding, spam suffixes, and SEO noise.
- **Built-In Dimension & Size Probing**:
  - Fast HTTP range requests with Pillow to determine actual image pixel dimensions (`width × height`), file size (`KB / MB`), and format (`JPEG`, `PNG`, `WEBP`) without downloading gigabytes of image data.
  - High-res badges (`HD`, `Ultra HD`) highlighted in emerald.
- **Hotlink & CORS Bypassing Image Proxy**:
  - Integrated image proxy (`/api/proxy-image?url=...`) ensures cover thumbnails render seamlessly in your browser even from anti-hotlinking websites.
- **Batch Export & Downloads**:
  - One-click **Download All as ZIP** with clean file naming (`{index}_{Magazine_Title}.jpg`).
  - Single-image high-res download.
  - Export structured metadata to **JSON** and **CSV**.
- **User-Friendly Web GUI**:
  - Built with modern Tailwind CSS and Lucide icons.
  - Fullscreen zoom lightbox modal.
  - Custom CSS selector overrides for power users.
  - Custom cookie and session support for member-only/authenticated sites.

---

## 🚀 Local Development

### 1. Requirements
- Python 3.10+
- pip

### 2. Setup
```bash
git clone https://github.com/rnrizki/soup-magazine-scraper.git
cd soup-magazine-scraper

python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

pip install -r requirements.txt
```

### 3. Run Development Server
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
Open your browser at `http://localhost:8000`.

---

## 🐳 Docker Deployment

```bash
docker build -t soup-magazine-scraper .
docker run -d -p 8000:8000 --name soup-cover soup-magazine-scraper
```

---

## 📡 API Endpoints

- `POST /api/scrape`: Scrapes single URL for magazine covers and names.
- `POST /api/scrape/multi`: Batch scrape across multiple URLs.
- `GET /api/proxy-image?url=...`: Proxies remote image to bypass hotlink protection.
- `POST /api/download/zip`: Packages selected covers into a downloadable ZIP file.
- `GET /api/health`: Healthcheck endpoint for Docker/Traefik (`200 OK`).

---

## 📄 License
MIT License. Built by [rnrizki](https://github.com/rnrizki).
