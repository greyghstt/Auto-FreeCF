"""
Turnstile Bypass — Multi-strategy solver for Cloudflare Turnstile.

Strategies (tried in order):
1. Check if token already populated (e.g. after manual solve or passive solve)
2. verify_cf() — nodriver built-in (image template matching → mouse click)
   Then poll for cf_challenge_response token to appear (verify_cf returns None)
3. CDP Input.dispatchMouseEvent — real mouse movement around widget (not JS-dispatched)
4. Wait for token to appear after interaction (poll up to 30s)
5. Fallback — return empty string, let submit-first logic handle it

Key findings from nodriver source code research:
- verify_cf() only does template_location() → mouse_click(). Returns None, NOT a token.
- page.evaluate() without return_by_value=True returns RemoteObject, not a plain value.
  Must use return_by_value=True and handle the RemoteObject wrapper.
- 'iframe_present' must NEVER be returned as a token (14 chars > 10 = false positive).
"""

import asyncio
from typing import Optional

import nodriver as uc
from nodriver import cdp


def _unwrap(val):
    """Unwrap nodriver evaluate result to primitive value.

    nodriver evaluate() can return:
    - RemoteObject (when return_by_value=False) → has .value attribute
    - deep_serialized_value → has .value attribute
    - plain value (when return_by_value=True and value is truthy)
    - ExceptionDetails (when JS throws)
    - None
    """
    if val is None:
        return None
    # ExceptionDetails — return None, caller handles
    if isinstance(val, cdp.runtime.ExceptionDetails):
        return None
    # RemoteObject namedtuple — has .value
    if hasattr(val, "value") and not isinstance(val, (dict, list, str, int, float, bool)):
        return val.value
    # DeepSerializedValue — has .value
    if hasattr(val, "deep_serialized_value") and val.deep_serialized_value:
        return val.deep_serialized_value.value
    # Already unwrapped dict/list/primitive
    if isinstance(val, dict) and "value" in val:
        return val["value"]
    return val


async def _get_challenge_token(page) -> str:
    """Read the actual cf_challenge_response / cf-turnstile-response token value.

    Returns:
        Token string if populated, empty string otherwise.
        NEVER returns 'iframe_present' — that's not a token.
    """
    val = await page.evaluate(
        """
        (() => {
            // Check both possible field names
            const el = document.querySelector('input[name="cf_challenge_response"]');
            if (el && el.value && el.value.length > 10) return el.value;
            const el2 = document.querySelector('input[name="cf-turnstile-response"]');
            if (el2 && el2.value && el2.value.length > 10) return el2.value;
            return '';
        })()
        """,
        return_by_value=True,
    )
    result = _unwrap(val)
    if result is None:
        return ""
    s = str(result).strip()
    # Reject 'iframe_present' and other non-token strings
    if s == "iframe_present" or len(s) < 10:
        return ""
    return s


async def _verify_cf_and_poll(page, timeout: float = 30.0) -> str:
    """
    Call nodriver's verify_cf() (template matching + mouse click on checkbox),
    then poll for the cf_challenge_response token to appear.

    verify_cf() returns None — it only clicks. The token appears asynchronously
    after Cloudflare processes the click. We must poll.
    """
    try:
        # verify_cf does: template_location → mouse_click
        # It returns None, so we don't capture the return value
        await page.verify_cf()
    except Exception as e:
        print(f"    ⚠️ verify_cf() error: {e}")

    # Poll for token to appear after the click
    token = await _wait_for_token(page, timeout=timeout, poll=1.0)
    return token


async def quick_interact(page) -> bool:
    """
    Real mouse interaction via CDP Input.dispatchMouseEvent to trigger
    Turnstile partial token. Uses actual CDP mouse events (not JS-dispatched)
    to avoid detection.

    Returns True if a token was found after interaction.
    """
    try:
        # Scroll widget into view
        await page.evaluate(
            """
            const w = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            if (w) w.scrollIntoView({block: 'center'});
            """,
            return_by_value=True,
        )
        await asyncio.sleep(1)

        # Get viewport dimensions
        viewport = _unwrap(
            await page.evaluate(
                "(() => ({w: window.innerWidth, h: window.innerHeight}))()",
                return_by_value=True,
            )
        )
        if not isinstance(viewport, dict):
            viewport = {"w": 800, "h": 600}
        w, h = viewport.get("w", 800), viewport.get("h", 600)

        # Real CDP mouse movement across center of page (where Turnstile sits)
        # Input.dispatchMouseEvent is the proper CDP way — not JS dispatchEvent
        center_y = int(h * 0.5)
        for x in range(int(w * 0.3), int(w * 0.7), 40):
            await page.send(
                cdp.input_.dispatch_mouse_event(
                    type_="mouseMoved",
                    x=x,
                    y=center_y,
                )
            )
            await asyncio.sleep(0.05)

        # Click near the Turnstile checkbox area
        # Try to find the iframe bounding box and click its center
        box = _unwrap(
            await page.evaluate(
                """
                (() => {
                    const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
                    if (iframe) {
                        const r = iframe.getBoundingClientRect();
                        return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height/2});
                    }
                    return null;
                })()
                """,
                return_by_value=True,
            )
        )
        if isinstance(box, str):
            try:
                import json

                coords = json.loads(box)
                cx, cy = int(coords["x"]), int(coords["y"])
                await page.send(
                    cdp.input_.dispatch_mouse_event(
                        type_="mousePressed",
                        x=cx,
                        y=cy,
                        button=cdp.input_.MouseButton.LEFT,
                        click_count=1,
                    )
                )
                await asyncio.sleep(0.1)
                await page.send(
                    cdp.input_.dispatch_mouse_event(
                        type_="mouseReleased",
                        x=cx,
                        y=cy,
                        button=cdp.input_.MouseButton.LEFT,
                        click_count=1,
                    )
                )
                print(f"    🖱️ Clicked Turnstile at ({cx},{cy})")
            except Exception as e:
                print(f"    ⚠️ Click failed: {e}")
        else:
            # Fallback: click center of page
            cx, cy = int(w * 0.5), center_y
            await page.send(
                cdp.input_.dispatch_mouse_event(
                    type_="mousePressed",
                    x=cx,
                    y=cy,
                    button=cdp.input_.MouseButton.LEFT,
                    click_count=1,
                )
            )
            await asyncio.sleep(0.1)
            await page.send(
                cdp.input_.dispatch_mouse_event(
                    type_="mouseReleased",
                    x=cx,
                    y=cy,
                    button=cdp.input_.MouseButton.LEFT,
                    click_count=1,
                )
            )

        await asyncio.sleep(2)

        # Check if we got a token after interaction
        token = await _get_challenge_token(page)
        if token:
            print(f"    ✅ Turnstile token found after CDP interaction: {token[:20]}...")
            return True

        return False
    except Exception as e:
        print(f"    ⚠️ quick_interact error: {e}")
        return False


async def is_turnstile_present(page) -> bool:
    """Check if Turnstile CAPTCHA is present on page."""
    present = _unwrap(
        await page.evaluate(
            """(() => {
        if (document.querySelector('input[name="cf_challenge_response"]')) return true;
        if (document.querySelector('input[name="cf-turnstile-response"]')) return true;
        const iframes = document.querySelectorAll("iframe");
        for (const f of iframes) {
            if (f.src && f.src.includes("challenges.cloudflare.com")) return true;
        }
        const body = document.body ? document.body.innerText : '';
        if (body.includes("Verify you are human") || body.includes("Let us know you are human")) return true;
        return false;
    })()""",
            return_by_value=True,
        )
    )
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
    timeout = 15.0 if quick else 45.0

    # Strategy 1: Check if token already populated (passive solve or manual)
    existing_token = await _get_challenge_token(page)
    if existing_token:
        return existing_token

    # Strategy 2: verify_cf() — nodriver built-in template matching + click
    # Then poll for token (verify_cf returns None, token appears async)
    token = await _verify_cf_and_poll(page, timeout=timeout)
    if token:
        return token

    # Quick mode: do CDP interaction, check once, return
    if quick:
        await quick_interact(page)
        token = await _get_challenge_token(page)
        if token:
            return token
        return ""  # Don't block — let submit-first handle it

    # Strategy 3: CDP interaction + extended wait for token
    # (different from strategy 2: uses real mouse events, not template matching)
    await quick_interact(page)
    await asyncio.sleep(3)

    # Wait for token to appear with longer timeout
    token = await _wait_for_token(page, timeout=45.0)
    if token:
        return token

    # Strategy 4: Last resort — try verify_cf again with longer timeout
    # (sometimes the first click didn't register, second click does)
    token = await _verify_cf_and_poll(page, timeout=15.0)
    if token:
        return token

    return ""


__all__ = [
    "solve_turnstile",
    "is_turnstile_present",
    "quick_interact",
    "_get_challenge_token",
    "_wait_for_token",
]
