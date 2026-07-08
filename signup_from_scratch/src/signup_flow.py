"""
Cloudflare Signup Flow — Submit-first strategy with hybrid CAPTCHA support.

Flow:
1. Navigate to sign-up page
2. Fill email + password
3. Quick Turnstile interaction (don't block on solve)
4. Submit form IMMEDIATELY
5. If redirect fails → proper Turnstile solve + resubmit
6. Hybrid mode: --wait-captcha pauses for manual CAPTCHA solve

The insight: CF signup is lenient. A partially-interacted Turnstile
often passes. Only fall back to full solve if the initial submit fails.
"""

import asyncio
import re
from typing import Optional

import nodriver as uc

from .turnstile_bypass import solve_turnstile, is_turnstile_present, _unwrap

CLOUDFLARE_SIGNUP_URL = "https://dash.cloudflare.com/sign-up"


class SignupResult:
    """Result of a signup attempt."""

    def __init__(
        self,
        success: bool,
        email: str = "",
        password: str = "",
        account_id: str = "",
        error: str = "",
        page_url: str = "",
    ):
        self.success = success
        self.email = email
        self.password = password
        self.account_id = account_id
        self.error = error
        self.page_url = page_url

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "email": self.email,
            "password": self.password,
            "account_id": self.account_id,
            "error": self.error,
        }


async def _fill_form(page: uc.Tab, email: str, password: str) -> Optional[str]:
    """Fill email + password. Returns error string or None."""
    # Email input — retry with delay (CF React form slow to mount in headless)
    email_input = None
    for attempt in range(6):
        email_input = await page.select('input[name="email"]', timeout=10)
        if email_input:
            break
        await asyncio.sleep(3)
    if not email_input:
        return "Email input not found"
    await email_input.click()
    await asyncio.sleep(0.5)
    await email_input.send_keys(email)
    await asyncio.sleep(1)

    # Password
    pw_input = None
    for attempt in range(4):
        pw_input = await page.select('input[name="password"]', timeout=5)
        if pw_input:
            break
        await asyncio.sleep(2)
    if not pw_input:
        return "Password input not found"
    await pw_input.click()
    await asyncio.sleep(0.5)
    await pw_input.send_keys(password)
    await asyncio.sleep(2)

    return None


async def _scroll_turnstile_into_view(page: uc.Tab) -> None:
    """Smart scroll: find Turnstile iframe and scroll it into center viewport."""
    await page.evaluate(
        """
        (() => {
            const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            if (iframe) {
                iframe.scrollIntoView({behavior: 'instant', block: 'center'});
                window.scrollBy(0, 150);
                return true;
            }
            const input = document.querySelector('input[name="cf-turnstile-response"], input[name="cf_challenge_response"]');
            if (input) {
                input.scrollIntoView({behavior: 'instant', block: 'center'});
                return true;
            }
            return false;
        })()
        """,
        return_by_value=True,
    )
    await asyncio.sleep(1)


async def _try_solve_turnstile(page: uc.Tab, quick: bool = False) -> str:
    """Try to solve Turnstile. Returns token or empty string."""
    turnstile_present = await is_turnstile_present(page)
    if not turnstile_present:
        return ""

    # Smart scroll to Turnstile widget
    await _scroll_turnstile_into_view(page)
    await asyncio.sleep(1)

    return await solve_turnstile(page, quick=quick)


async def _has_challenge_response(page: uc.Tab) -> bool:
    """Check if cf_challenge_response has a value."""
    from .turnstile_bypass import _get_challenge_token

    token = await _get_challenge_token(page)
    return bool(token and len(token) > 5)


async def _wait_for_manual_captcha(
    page: uc.Tab,
    timeout: int = 600,
    poll_interval: int = 3,
) -> bool:
    """
    Hybrid mode: wait for user to manually click/complete the CAPTCHA.
    Returns True when cf_challenge_response has a token.
    """
    print("    ⏸️  HYBRID MODE: Browser is paused at sign-up form.")
    print("    Please complete the CAPTCHA manually in the browser window.")
    print(f"    Auto-resuming when CAPTCHA solved (timeout: {timeout}s)...")

    for elapsed in range(0, timeout, poll_interval):
        solved = await _has_challenge_response(page)
        if solved:
            print(f"    ✅ CAPTCHA solved manually after {elapsed}s")
            return True
        if elapsed > 0 and elapsed % 30 == 0:
            print(f"    Still waiting for CAPTCHA... ({elapsed}s)")
        await asyncio.sleep(poll_interval)

    print(f"    ⚠️ CAPTCHA not solved within {timeout}s — continuing with best effort")
    return False


async def _submit_form(page: uc.Tab) -> Optional[str]:
    """Click submit button. Returns error string or None."""
    submit_btn = await page.select('button[type="submit"]', timeout=5)
    if not submit_btn:
        # Try finding submit button by text content
        try:
            submit_btn = await page.find("Sign Up", best_match=True, timeout=3)
        except Exception:
            pass
    if not submit_btn:
        return "Submit button not found"
    await submit_btn.scroll_into_view()
    await asyncio.sleep(1)
    await submit_btn.click()
    return None


async def _wait_for_redirect(page: uc.Tab, max_wait: int = 30) -> str:
    """Wait for signup redirect. Returns final URL."""
    for _ in range(max_wait):
        await asyncio.sleep(1)
        url = str(_unwrap(await page.evaluate("location.href", return_by_value=True)))
        if "/sign-up" not in url:
            return url
    return str(_unwrap(await page.evaluate("location.href", return_by_value=True)))


async def _extract_account_id(page: uc.Tab, url: str) -> Optional[str]:
    """Extract Account ID from URL or DOM."""
    # Try URL first
    match = re.search(r"/([a-f0-9]{32})", url)
    if match:
        return match.group(1)

    # Try DOM
    account_id = _unwrap(
        await page.evaluate(
            """
            (() => {
                const el = document.querySelector('[data-account-id], [data-testid="account-id"]');
                if (el) return el.textContent || el.getAttribute('data-account-id');
                return null;
            })()
            """,
            return_by_value=True,
        )
    )
    if account_id:
        return str(account_id).strip()

    return None


async def _check_errors(page: uc.Tab) -> str:
    """Extract error messages from page."""
    error_msgs = _unwrap(
        await page.evaluate(
            """
            Array.from(document.querySelectorAll('p, [role="alert"], .error, .notification'))
                .map(e => (e.textContent || '').trim())
                .filter(t => t.length > 5 && (t.includes('unable') || t.includes('limit') ||
                    t.includes('Incorrect') || t.includes('try again') || t.includes('blocked')))
            """,
            return_by_value=True,
        )
    )
    if error_msgs and isinstance(error_msgs, list):
        return "; ".join([str(m) for m in error_msgs if m])
    return ""


async def signup(
    page: uc.Tab,
    email: str,
    password: str,
    max_wait: int = 30,
    retry_turnstile: bool = True,
    wait_captcha: bool = False,
) -> SignupResult:
    """
    Execute Cloudflare signup with submit-first strategy.

    1. Fill form
    2. Quick Turnstile interaction (non-blocking)
    3. Submit immediately
    4. If still on sign-up page → proper solve + resubmit
    5. Extract Account ID

    Args:
        page: nodriver Tab
        email: Email address
        password: Password
        max_wait: Max seconds to wait for redirect
        retry_turnstile: Whether to retry with proper solve on failure
        wait_captcha: Hybrid mode — pause for manual CAPTCHA solve

    Returns:
        SignupResult with account_id on success
    """
    # ─── Navigate ───
    await page.get(CLOUDFLARE_SIGNUP_URL)
    await asyncio.sleep(8)

    # ─── Fill Form ───
    error = await _fill_form(page, email, password)
    if error:
        return SignupResult(False, email=email, error=error)

    # ─── Hybrid mode: wait for manual CAPTCHA ───
    if wait_captcha:
        await _scroll_turnstile_into_view(page)
        await _wait_for_manual_captcha(page)
    else:
        # ─── Quick Turnstile (non-blocking) ───
        token = await _try_solve_turnstile(page, quick=True)
        if token:
            print(f"    ✅ Turnstile solved: {token[:20]}...")
        else:
            print(f"    ⚡ Turnstile quick-interact (submit-first)")

    # ─── Submit Form ───
    error = await _submit_form(page)
    if error:
        return SignupResult(False, email=email, error=error)

    # ─── Wait for Redirect ───
    url = await _wait_for_redirect(page, max_wait)
    await asyncio.sleep(5)

    # ─── Check Result ───
    account_id = await _extract_account_id(page, url)

    if account_id:
        return SignupResult(
            True,
            email=email,
            password=password,
            account_id=account_id,
            page_url=url,
        )

    # ─── Still on sign-up page? Retry with proper solve ───
    if retry_turnstile and "/sign-up" in url:
        print(f"    🔄 Submit-first failed. Retry with full Turnstile solve...")

        # Reload and refill
        await page.get(CLOUDFLARE_SIGNUP_URL)
        await asyncio.sleep(5)

        error = await _fill_form(page, email, password)
        if error:
            return SignupResult(False, email=email, error=f"Retry fill failed: {error}")

        token = await _try_solve_turnstile(page, quick=False)
        print(f"    {'✅' if token else '⚠️ No token — forcing submit'} Turnstile retry")

        error = await _submit_form(page)
        if error:
            return SignupResult(False, email=email, error=f"Retry submit failed: {error}")

        url = await _wait_for_redirect(page, max_wait)
        await asyncio.sleep(5)

        account_id = await _extract_account_id(page, url)
        if account_id:
            return SignupResult(
                True,
                email=email,
                password=password,
                account_id=account_id,
                page_url=url,
            )

    # ─── Final failure ───
    error_msgs = await _check_errors(page)
    error = error_msgs if error_msgs else f"Redirect failed: {url[:80]}"
    return SignupResult(False, email=email, error=error)
