# Browser Fetch Service

Standalone HTTP wrapper around headless Chromium (Playwright), split out of
the main backend so the api/worker/crawl-worker images don't have to carry
Chromium's binary weight and startup cost — only this service does, and
only when it's actually called.

Used by `app.services.browser_fetch.fetch_rendered_html` for the handful of
sites whose content only exists after JS runs (see that module's docstring).
Point the main app at a running instance with `BROWSER_FETCH_SERVICE_URL`;
leaving it unset just skips that fallback.

## Run locally

```bash
pip install -r requirements.txt
playwright install --with-deps chromium
uvicorn main:app --reload --port 8080
```

## API

`POST /fetch` — body `{"url": "...", "wait_for_selector": "..." | null}`,
returns `{"html": "..." | null}`. Never returns a non-2xx for a render
failure (timeout, crash, navigation error, ...) — `html: null` is the
failure signal, matching `fetch_rendered_html`'s "never raises" contract.

`GET /health` — liveness check.

## Deploy

Build the `Dockerfile` in this directory and run it anywhere that can hold
a ~300-500MB Chromium install in memory while a request is in flight — e.g.
a Cloud Run service with `min-instances=0` (scales to zero between the rare
calls that need it) and enough memory allocated (512Mi-1Gi) for Chromium to
launch.

Pushes to `main` deploy automatically to Cloud Run via
`.github/workflows/deploy.yml`. See that file for the required GitHub
secrets.

## License

[PolyForm Noncommercial 1.0.0](LICENSE) — free for personal and
noncommercial use; commercial use requires separate permission.
