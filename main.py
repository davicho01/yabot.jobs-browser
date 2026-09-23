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

# page.content() serializes the light DOM only — any web-component that
# renders its real content into a shadow root (verified live: UltiPro/UKG
# Pro's "Ignite" design system, used by several ATS tenants) comes back as
# an empty custom-element shell no matter how long you wait, open or closed
# shadow root alike. This walks the real, live DOM tree in-page instead of
# asking Chromium for its own serialization, inlining every open shadow
# root's children at the point their host element would otherwise be empty
# — regex-based adapters downstream then see shadow content as if it were
# always part of the regular tree. Opt-in (see pierce_shadow below) since
# it's slower than page.content() and unnecessary for the ~90 other
# call sites that don't need it.
_SHADOW_PIERCING_SERIALIZE_SCRIPT = """
() => {
    const voidTags = new Set(['area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr']);
    const escapeAttr = (s) => s.replace(/&/g, '&amp;').replace(/"/g, '&quot;');
    function serialize(node) {
        if (node.nodeType === Node.TEXT_NODE) return node.textContent;
        if (node.nodeType !== Node.ELEMENT_NODE) return '';
        const tag = node.tagName.toLowerCase();
        let html = '<' + tag;
        for (const attr of node.attributes) html += ' ' + attr.name + '="' + escapeAttr(attr.value) + '"';
        if (voidTags.has(tag)) return html + '>';
        html += '>';
        if (node.shadowRoot) for (const child of Array.from(node.shadowRoot.childNodes)) html += serialize(child);
        for (const child of Array.from(node.childNodes)) html += serialize(child);
        return html + '</' + tag + '>';
    }
    return serialize(document.documentElement);
}
"""

app = FastAPI(title="Yabot Browser Fetch Service")


class FetchRequest(BaseModel):
    url: str
    wait_for_selector: str | None = None
    # See _SHADOW_PIERCING_SERIALIZE_SCRIPT above — only needed for tenants
    # whose real content lives inside a shadow root.
    pierce_shadow: bool = False


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
                    page.wait_for_selector(request.wait_for_selector, timeout=_DEFAULT_TIMEOUT_MS)
                html = page.evaluate(_SHADOW_PIERCING_SERIALIZE_SCRIPT) if request.pierce_shadow else page.content()
                return FetchResponse(html=html, final_url=page.url)
            finally:
                browser.close()
    except Exception:
        logger.warning("Rendered fetch of %s failed.", request.url, exc_info=True)
        return FetchResponse(html=None, final_url=None)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
