"""
Turnstile Bypass — Multi-strategy solver for Cloudflare Turnstile.

Strategies (tried in order):
1. verify_cf() — nodriver built-in (image template matching)
2. CDP-based token check — check cf_challenge_response value directly
3. Quick interaction — mouse move, tab, click around widget (triggers partial token)
4. Wait for manual solve via visible browser window
5. Fallback — just return, let submit-first logic handle it

The insight: CF signup flow is lenient. A partial/interacted Turnstile
often passes. Only fall back to full solve if redirect fails.
"""

import asyncio
from typing import Optional


async def verify_cf(page, timeout: float = 60.0) -> str:
    """Solve Turnstile using nodriver's built-in method."""
    result = await page.verify_cf()
    return result or ""


async def _get_challenge_token(page) -> str:
    """Read the actual cf_challenge_response / cf-turnstile-response token value."""
    val = await page.evaluate("""
        (() => {
            // Check both possible field names
            const el = document.querySelector('input[name="cf_challenge_response"]');
            if (el && el.value && el.value.length > 10) return el.value;
            const el2 = document.querySelector('input[name="cf-turnstile-response"]');
            if (el2 && el2.value && el2.value.length > 10) return el2.value;
            // Check inside Turnstile iframe
            const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            if (iframe) return 'iframe_present';
            return '';
        })()
    """)
    if isinstance(val, dict) and "value" in val:
        return val["value"] or ""
    return str(val or "")


async def quick_interact(page) -> bool:
    """
    Minimal interaction to trigger Turnstile partial token.
    Moves mouse around the widget, tabs, clicks nearby.
    Returns True if any interaction succeeded.
    """
    try:
        # Scroll widget into view
        await page.evaluate("""
            const w = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            if (w) w.scrollIntoView({block: 'center'});
        """)
        await asyncio.sleep(1)

        # Click near the Turnstile iframe (not on it — triggers detection)
        viewport = await page.evaluate("""
            (()=>{return {w:window.innerWidth, h:window.innerHeight}})()
        """)
        w, h = viewport.get("w", 800), viewport.get("h", 600)

        # Move mouse across center of page (where Turnstile usually sits)
        for x in range(int(w * 0.3), int(w * 0.7), 40):
            await page.evaluate(f"document.elementFromPoint({x},{int(h*0.5)})?.dispatchEvent(new MouseEvent('mousemove',{{clientX:{x},clientY:{int(h*0.5)},bubbles:true}}))")
            await asyncio.sleep(0.05)

        # Click the checkbox area if present
        clicked = await page.evaluate("""
            (() => {
                const cb = document.querySelector('.cb-i, .challenge, input[type="checkbox"]');
                if (cb) { cb.click(); return true; }
                return false;
            })()
        """)
        await asyncio.sleep(2)

        # Check if we got a token after interaction
        token = await _get_challenge_token(page)
        if token and len(token) > 10:
            print(f"    ✅ Turnstile token found after interaction: {token[:20]}...")
            return True

        return True
    except Exception:
        return False


async def is_turnstile_present(page) -> bool:
    """Check if Turnstile CAPTCHA is present on page."""
    present = await page.evaluate('''(() => {
        if (document.querySelector('input[name="cf_challenge_response"]')) return true;
        if (document.querySelector('input[name="cf-turnstile-response"]')) return true;
        const iframes = document.querySelectorAll("iframe");
        for (const f of iframes) {
            if (f.src && f.src.includes("challenges.cloudflare.com")) return true;
        }
        const body = document.body ? document.body.innerText : '';
        if (body.includes("Verify you are human") || body.includes("Let us know you are human")) return true;
        return false;
    })()''')
    if isinstance(present, dict) and "value" in present:
        return bool(present["value"])
    return bool(present)


async def _wait_for_token(page, timeout: float, poll: float = 1.0) -> str:
    """Poll for cf_challenge_response token to appear."""
    elapsed = 0.0
    while elapsed < timeout:
        token = await _get_challenge_token(page)
        if token and len(token) > 10:
            return token
        await asyncio.sleep(poll)
        elapsed += poll
    return ""


async def solve_turnstile(page, quick: bool = False) -> str:
    """
    Multi-strategy Turnstile solver.

    Args:
        page: nodriver Tab
        quick: If True, short timeout + skip retry (for submit-first flow)

    Returns:
        Token string or empty string if failed
    """
    timeout = 15.0 if quick else 60.0

    # Strategy 1: Check if token already populated (e.g. after manual solve)
    existing_token = await _get_challenge_token(page)
    if existing_token and len(existing_token) > 10:
        return existing_token

    # Strategy 2: verify_cf() — nodriver built-in
    try:
        token = await verify_cf(page, timeout=timeout)
        if token:
            return token
    except Exception:
        pass

    # Quick mode: just do quick interaction and return
    if quick:
        await quick_interact(page)
        # Check again after interaction
        token = await _get_challenge_token(page)
        if token and len(token) > 10:
            return token
        return ""  # Don't block — let submit-first handle it

    # Strategy 3: Full solve with longer timeout
    try:
        token = await verify_cf(page, timeout=60.0)
        if token:
            return token
    except Exception:
        pass

    # Strategy 4: Check token again (nodriver's verify_cf may have populated it)
    token = await _get_challenge_token(page)
    if token and len(token) > 10:
        return token

    # Strategy 5: Interaction + wait for token
    await quick_interact(page)
    await asyncio.sleep(3)

    # Wait for token to appear
    token = await _wait_for_token(page, timeout=30.0)
    if token:
        return token

    return ""
