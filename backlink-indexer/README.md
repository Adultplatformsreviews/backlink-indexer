# Backlink Indexer

A self-hosted dashboard for pasting external backlink URLs, pre-checking whether those pages are live and actually link to your site, and helping search engines *discover* them as quickly as is legitimately possible.

**This tool cannot force Google to index a page on someone else’s website.** Google Search Console and the Google Indexing API only work for properties you have verified. Tools that advertise “instant Google indexing” for third-party backlinks are overselling: at best they list your URLs on crawlable pages, ping feeds, and use IndexNow on *hosts they control*.

What this app *does* do:

1. **Pre-checks** each URL: HTTP status, whether the page is live, `meta robots` / `X-Robots-Tag` noindex, Googlebot `robots.txt` disallow, canonical pointing elsewhere, and whether the HTML contains a link to your target domain (dofollow / nofollow / mixed).
2. **Discovery hub** — a public HTML page, XML sitemap, and RSS/Atom feeds that list submitted URLs so crawlers can find them from a domain *you* own. You can ping a WebSub hub and Bing’s sitemap ping. Google’s sitemap ping endpoint is deprecated and is not called.
3. **IndexNow** for URLs on hosts you control (your hub, plus any host you mark as owned and can serve the key file on). Third-party backlink hosts are skipped; IndexNow will reject them anyway.
4. **Optional Google Indexing API** for URLs on *your own* Search Console–verified site, via a service-account JSON. Clearly labelled own-site only.
5. **Index status** via an optional SERP API (SerpAPI, ValueSERP, SearchApi.io, or a custom endpoint), plus a manual “Mark indexed / not indexed” toggle. Status is never invented.
6. **CSV/TXT upload**, projects and batches, and **CSV export**.

Default target domain is `feetfinder.com` (change per project or in Settings).

## Run locally

```bash
cd backlink-indexer
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
./run.sh
```

Or:

```bash
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8787
```

Open **http://127.0.0.1:8787** (or `http://<this-machine>:8787`).

Optional environment variables are documented in `.env.example`. To password-protect the dashboard (hub/feeds stay public):

```bash
export APP_USERNAME=zahoor
export APP_PASSWORD='choose-a-strong-password'
./run.sh
```

SQLite and uploaded keys live in `./data/` (override with `APP_DATA_DIR`).

## Deploy so the hub is publicly crawlable

Crawlers can only use the hub, sitemap, and feeds if they are on a public HTTPS hostname. A laptop on localhost is fine for pre-checks, but not for discovery.

### Cheap VPS (Nginx + systemd)

1. Point a hostname (for example `indexer.yourdomain.com`) at the VPS.
2. Install Python 3.11+, copy this project, create the venv, `pip install -r requirements.txt`.
3. Run under systemd:

```ini
# /etc/systemd/system/backlink-indexer.service
[Service]
WorkingDirectory=/opt/backlink-indexer
Environment=PORT=8787
Environment=APP_PUBLIC_BASE_URL=https://indexer.yourdomain.com
Environment=DEFAULT_TARGET_DOMAIN=feetfinder.com
Environment=APP_USERNAME=zahoor
Environment=APP_PASSWORD=change-me
ExecStart=/opt/backlink-indexer/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8787
Restart=always
User=www-data
```

4. Nginx reverse proxy with Let’s Encrypt:

```nginx
server {
    server_name indexer.yourdomain.com;
    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

5. In the app: Settings → Public base URL = `https://indexer.yourdomain.com`. Generate an IndexNow key. Submit the sitemap URL in Bing Webmaster Tools.

### Render

This repo includes `render.yaml`. Create a Web Service from the repo, set `APP_PUBLIC_BASE_URL` to the `onrender.com` (or custom) URL, and attach a persistent disk at `/opt/render/project/src/data` if you want the SQLite database to survive deploys.

### Railway

Use the included `Procfile`. Set `APP_PUBLIC_BASE_URL` to the Railway public URL (or a custom domain) and add a volume for `APP_DATA_DIR`.

### Docker

```bash
docker build -t backlink-indexer .
docker run -d --name backlink-indexer -p 8787:8787 \
  -e APP_PUBLIC_BASE_URL=https://indexer.yourdomain.com \
  -v indexer-data:/app/data \
  backlink-indexer
```

## Settings you need for each optional feature

| Feature | What you provide |
| --- | --- |
| Discovery hub pings | A public base URL on a domain you own |
| IndexNow | A generated key (served at `/{key}.txt` on this app) and, for other sites, the same key file on that host. Mark those hosts under **Owned hosts**. |
| Google Indexing API | Service-account JSON with access to your Search Console property. Own-site URLs only. |
| Automatic “check indexed” | A SERP API key (SerpAPI, ValueSERP, or SearchApi.io). Otherwise use the manual toggle. |

## Typical workflow

1. Create a project and set the target domain (`feetfinder.com`).
2. Paste backlink URLs or upload a CSV/TXT (a `url` column is used if present).
3. Click **Run pre-checks**. Drop or fix URLs that are dead, noindexed, robots-blocked, canonicalized away, or that do not actually link to you.
4. Deploy the app on a public host, set the public base URL, and click **Ping WebSub + Bing sitemap** on the Discovery hub page.
5. Use IndexNow / Google Indexing API only for pages on sites you control.
6. Check index status with a SERP key or mark it manually. Export CSV when needed.

## Limitations

- Fetching a page with this app’s user-agent is not the same as Googlebot’s view. A site that cloaks or blocks unknown bots may look different in pre-checks than it does to Google.
- `robots.txt` checks use the Googlebot user-agent string against the published file; the app does **not** fetch pages while pretending to be Googlebot.
- Private, loopback, and cloud-metadata addresses are blocked.
- IndexNow and the Google Indexing API will not accept third-party backlink URLs.
- SERP “indexed?” checks look for the URL in a `site:` query’s top results. A miss is not absolute proof the page is unindexed.
- There is no background re-check scheduler in this version; re-run pre-checks or index checks from the dashboard.
- Many sites (Cloudflare, etc.) return **403** to datacenter IPs or unknown crawlers, so a pre-check can disagree with what Googlebot sees. The app does not pretend to be Googlebot.
- In testing (November 2026), Bing's legacy `https://www.bing.com/ping?sitemap=` endpoint returned HTTP 410. Prefer submitting the hub sitemap in Bing Webmaster Tools and using IndexNow for hosts you control. WebSub (`pubsubhubbub.appspot.com`) still accepted a publish ping (HTTP 204).

## License

For personal / internal use by the operator of this instance.
