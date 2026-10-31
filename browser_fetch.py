"""Render public pages; synchronous Playwright is isolated from Uvicorn's Windows loop."""
import asyncio
import base64
import ipaddress
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, Error as PlaywrightError

def blocked_resource(url):
    try:
        p = urlsplit(url)
        if p.scheme not in ('http','https') or not p.hostname: return True
        host = p.hostname.lower().rstrip('.')
        if host == 'localhost' or host.endswith(('.local','.internal')): return True
        try: return not ipaddress.ip_address(host).is_global
        except ValueError: return False
    except ValueError: return True

def _render(url, root, timeout):
    from menu_importer import same_site, verified_url, MAX_BYTES
    verified_url(url)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(service_workers='block', accept_downloads=False)
            try:
                page = context.new_page()
                def route_request(route):
                    req = route.request
                    if blocked_resource(req.url) or (req.is_navigation_request() and not same_site(req.url, root)):
                        route.abort()
                    elif req.resource_type in ('media','font','websocket'):
                        route.abort()
                    else:
                        route.continue_()
                page.route('**/*', route_request)
                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=timeout)
                    page.wait_for_timeout(1000)
                    for _ in range(3):
                        page.evaluate('window.scrollBy(0, Math.max(700, window.innerHeight))')
                        page.wait_for_timeout(350)
                    html = page.content()
                    return html if len(html.encode('utf8')) <= MAX_BYTES else ''
                except PlaywrightError:
                    return ''
            finally:
                context.close()
        finally:
            browser.close()

async def render_page(url, root, *, timeout=20000):
    return await asyncio.to_thread(_render, url, root, timeout)




def _explore_sync(url, root, timeout=20000, max_states=5):
    from menu_importer import same_site, verified_url, MAX_BYTES
    verified_url(url)
    snapshots = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(service_workers='block', accept_downloads=False)
            try:
                page = context.new_page()
                def guard(route):
                    request = route.request
                    if blocked_resource(request.url) or (request.is_navigation_request() and not same_site(request.url, root)):
                        route.abort()
                    elif request.resource_type in ('media','font','websocket'):
                        route.abort()
                    else:
                        route.continue_()
                page.route('**/*', guard)
                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=timeout)
                    page.wait_for_timeout(850)
                    def capture(label):
                        html = page.content()
                        if len(html.encode('utf-8')) <= MAX_BYTES:
                            snapshots.append({'label':label,'html':html, 'screenshot': base64.b64encode(page.screenshot(type='jpeg', quality=45, full_page=False)).decode('ascii')})
                    capture('initial')
                    
                    controls = page.locator('[role="tab"], button[aria-expanded="false"]')
                    count = min(controls.count(), max_states - 1, 12)
                    for idx in range(count):
                        try:
                            control = controls.nth(idx)
                            if not control.is_visible() or not control.is_enabled(): continue
                            label = (control.inner_text(timeout=1200) or '').strip()[:80]
                            control.click(timeout=2000)
                            page.wait_for_timeout(500)
                            capture(label or f'expanded {idx+1}')
                        except PlaywrightError:
                            continue
                    return snapshots
                except PlaywrightError:
                    return snapshots
            finally:
                context.close()
        finally:
            browser.close()

async def explore_page(url, root, *, timeout=20000, max_states=5):
    return await asyncio.to_thread(_explore_sync, url, root, timeout, max_states)
