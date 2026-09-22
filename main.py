"""Standalone browser-rendering service.

The only thing in this repo that needs headless Chromium, deployed as its
own service so the api/worker/crawl-worker images stay small and fast to
build — only this service's Dockerfile installs Playwright's Chromium
binary, and only this service's memory/CPU footprint reflects running a
real browser.

Called by app.services.browser_fetch — see that module for the calling
contract: a render failure here just comes back as html: null, never an
error the caller has to handle specially.
"""

import logging

from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("browser_fetch_service")

_DEFAULT_TIMEOUT_MS = 15_000

app = FastAPI(title="Yabot Browser Fetch Service")


class FetchRequest(BaseModel):
    url: str
    wait_for_selector: str | None = None


class FetchResponse(BaseModel):
    html: str | None
    final_url: str | None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/fetch", response_model=FetchResponse)
def fetch(request: FetchRequest) -> FetchResponse:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                try:
                    page.goto(request.url, timeout=_DEFAULT_TIMEOUT_MS, wait_until="networkidle")
                except PlaywrightTimeoutError:
                    # Some sites (e.g. an AWS WAF JS challenge in front of
                    # careers.ibm.com) never go network-idle - background
                    # polling/analytics keeps firing indefinitely even once
                    # the challenge has resolved and real content has
                    # rendered - so this wait always times out regardless of
                    # whether the page actually loaded. The page has still
                    # navigated by now; salvage whatever's in the DOM rather
                    # than discarding it outright. wait_for_selector below is
                    # a stricter, deliberate "did hydration actually finish"
                    # check for callers that need one, and keeps failing hard
                    # on its own timeout (caught below) as before.
                    logger.warning("networkidle wait timed out for %s; using page content as-is.", request.url)
                if request.wait_for_selector:
                    page.wait_for_selector(request.wait_for_selector, timeout=_DEFAULT_TIMEOUT_MS)
                return FetchResponse(html=page.content(), final_url=page.url)
            finally:
                browser.close()
    except Exception:
        logger.warning("Rendered fetch of %s failed.", request.url, exc_info=True)
        return FetchResponse(html=None, final_url=None)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
