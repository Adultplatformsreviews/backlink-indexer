# Backlink Indexer

A self-hosted dashboard for pasting external backlink URLs, pre-checking whether those pages are live, and helping search engines *discover* them. Own-site submission goes through Search Console and IndexNow. External URLs are listed on an indexer network you host. A URL is indexed only after a SERP check or a manual mark.

**This tool cannot force Google to index a page on someone else’s website.** Credits are reserved when a URL is accepted and consumed only when it is confirmed indexed. Not-indexable URLs are refunded immediately. Anything still unconfirmed after the refund window (7 days by default, Settings → refund window) is refunded. SERP checks still run on day 1, 3, 7, 14, and 28. A later confirmation is not charged again.

## Indexer network

The operator (Settings → Indexer Network) registers one or more hostnames:

1. Add the hostname. The app generates an IndexNow key and serves `https://<hostname>/{key}.txt` itself. Point the hostname’s DNS at this Vercel project, then click **Verify IndexNow key**.
2. In Google Search Console, add the hub domain as a property and add the service-account email as an **Owner**. Upload that service-account JSON on the hub domain. Each Cloud project is allowed 200 Indexing API URLs per day. Add more JSON keys to raise that cap by 200 each. IndexNow’s cap on a hub domain is 10,000 URLs per day.
3. Google push is off until you turn it on. The page shows this warning and it stays there: Google officially permits the Indexing API only for JobPosting and BroadcastEvent pages. Other use can get the project's quota revoked. Use at your own risk.
4. External URLs are grouped into pages of at most 20 at `https://<hostname>/l/<slug>`, added only to that hostname’s sitemap, and pushed to IndexNow. Google is notified only when the toggle is on. A page is pushed at most twice (again on day 3 if nothing on it is crawled yet).
5. The public `/hub` on this deployment is the fallback network: 25 URLs per page, `rel="nofollow ugc"`, and its sitemap lists only this app’s own pages. It does not call the Google Indexing API.

Own sites (Settings → Connections) use Google OAuth with the Search Console scope, IndexNow on each verified host, sitemap resubmit, and the URL Inspection API. Inspection can move a URL to crawled. It never marks a URL indexed. The row links to Search Console so a person can request indexing there.

### Migrations

Schema changes are applied automatically the first time the process opens the database. They only add tables and columns. The same statements are recorded in `backlink_indexer/migrations/001_indexer_network.sql` for review. Nothing is dropped.

### Environment variables

| Variable | Required for | Purpose |
| --- | --- | --- |
| `SERPAPI_KEY` | SERP checks and the index checker | SerpApi key. Can also be saved in Settings. Searches are cached 24 hours. |
| `ENCRYPTION_KEY` | Hub-domain service accounts, Search Console tokens, IndexNow keys | Passphrase used to encrypt those secrets at rest. |
| `GOOGLE_CLIENT_ID` | Settings → Connections | OAuth client for Search Console. Redirect URI: `/app/connections/google/callback`. |
| `GOOGLE_CLIENT_SECRET` | Settings → Connections | OAuth client secret. |
| `CRON_SECRET` | `/api/cron/*` | Send `Authorization: Bearer <CRON_SECRET>`. On Vercel this should be set. |
| `DATABASE_URL` (or `POSTGRES_URL`) | Durable deploys | Postgres connection. Without it the database is local SQLite. |

Optional: `INDEXER_HOST_INTERVAL` (seconds between pre-check requests to one host, default 1), `PRECHECK_BUDGET_SECONDS`, `INDEXER_SKIP_LIVE_PRECHECK=1` (tests only).

### Cron and the Vercel Hobby plan

`vercel.json` schedules pre-check every 15 minutes, drip / SERP / inspection hourly, and quota reset daily, plus the existing daily queue tick. **Vercel Hobby only allows cron jobs that run once per day** (and a small number of them). Hourly and 15-minute schedules need **Pro**. On Hobby, change those paths to a daily schedule or the deployment will be rejected. The routes themselves are safe to call more often when the plan allows it.

Reports count confirmed indexed URLs only. They never guess.

A self-hosted dashboard for pasting external backlink URLs, pre-checking whether those pages are live and actually link to your site, and helping search engines *discover* them as quickly as is legitimately possible.

**This tool cannot force Google to index a page on someone else’s website.** Google Search Console and the Google Indexing API only work for properties you have verified. Tools that advertise “instant Google indexing” for third-party backlinks are overselling: at best they list your URLs on crawlable pages, ping feeds, and use IndexNow on *hosts they control*.

What this app *does* do:

1. **Pre-checks** each URL: HTTP status, whether the page is live, `meta robots` / `X-Robots-Tag` noindex, Googlebot `robots.txt` disallow, canonical pointing elsewhere, and whether the HTML contains a link to your target domain (dofollow / nofollow / mixed).
2. **Discovery hub** — public HTML pages on a hostname you control. Sitemaps list only that hostname’s own pages. Third-party URLs appear on those pages with `rel="nofollow ugc"`. Google’s sitemap ping endpoint is deprecated and is not called.
3. **IndexNow** for hub pages and for hosts you have verified (the key file has to be on that host). Third-party hosts are not submitted to IndexNow.
4. **Optional Google Indexing API** for a hub-page URL only, and only when that hub domain’s “Google push” toggle is on. The Indexing API is officially limited to JobPosting and BroadcastEvent pages; other use can get the Cloud project’s quota revoked. It is never used to pretend a third-party URL was submitted to Google.
5. **Index status** via an optional SERP API (SerpAPI, ValueSERP, SearchApi.io, or a custom endpoint), plus a manual “Mark indexed / not indexed” toggle. Status is never invented.
6. **CSV/TXT upload**, projects and batches, and **CSV export**.

Default target domain is `feetfinder.com` (change per project or in Settings).

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
./run.sh
```

Or:

```bash
.venv/bin/uvicorn backlink_indexer.main:app --host 0.0.0.0 --port 8787
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
ExecStart=/opt/backlink-indexer/.venv/bin/uvicorn backlink_indexer.main:app --host 127.0.0.1 --port 8787
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
# From the repository root (the Dockerfile lives there).
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
