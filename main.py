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

# Some WAF-fronted tenants (verified live: careers.ibm.com's AWS WAF JS
# challenge) take longer than a quick heuristic to actually clear - the
# challenge itself, then a separate AJAX call for the real page content,
# can together exceed 15s even though the page is genuinely just slow, not
# broken. 35s leaves headroom under the service's own 90s Cloud Run request
# timeout (see deploy.yml) even in the worst case of both the goto wait and
# a subsequent wait_for_selector each running to their own full timeout.
_DEFAULT_TIMEOUT_MS = 35_000

# careers.ibm.com's AWS WAF (Bot Control) lets a plain page load through but
# never lets the SearchJobs results themselves render for this browser, even
# well past every timeout above - consistent with fingerprinting Playwright's
# default automation signals (navigator.webdriver=true, no window.chrome, a
# UA containing "HeadlessChrome", empty plugins/mimeTypes) rather than a
# timing issue. These are the standard, well-established patches for each of
# those signals - not exhaustive stealth (no WebGL/canvas spoofing), but the
# ones a Bot-Control-style check actually looks at first.
_REALISTIC_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);
"""

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
            browser = playwright.chromium.launch(
                args=["--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )
            try:
                context = browser.new_context(
                    user_agent=_REALISTIC_USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                )
                context.add_init_script(_STEALTH_INIT_SCRIPT)
                page = context.new_page()
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
                    try:
                        page.wait_for_selector(request.wait_for_selector, timeout=_DEFAULT_TIMEOUT_MS)
                    except PlaywrightTimeoutError:
                        # Diagnostic only (temporary): Cloud Run's stderr
                        # capture truncates a single long log line hard
                        # (confirmed: a 4000-char body snippet in one
                        # logger.warning call arrived cut off after ~15
                        # bytes) - log short, targeted signals plus the
                        # body in small chunks instead of one huge line.
                        content = page.content()
                        lower = content.lower()
                        signals = {
                            "has_job_link": "/careers/jobdetail/" in lower,
                            "has_no_results_text": any(
                                s in lower for s in ("no jobs found", "no results", "0 results", "no matching")
                            ),
                            "a_tag_count": content.count("<a "),
                            "script_tag_count": content.count("<script"),
                        }
                        logger.warning("wait_for_selector timeout for %s: len=%d %s", request.url, len(content), signals)
                        body_start = content.find("<body")
                        if body_start != -1:
                            chunk = content[body_start : body_start + 900]
                            for i in range(0, len(chunk), 150):
                                logger.warning("body[%d]=%r", body_start + i, chunk[i : i + 150])
                        raise
                return FetchResponse(html=page.content(), final_url=page.url)
            finally:
                browser.close()
    except Exception:
        logger.warning("Rendered fetch of %s failed.", request.url, exc_info=True)
        return FetchResponse(html=None, final_url=None)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
