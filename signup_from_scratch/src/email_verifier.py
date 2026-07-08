"""Cloudflare email verification via temp-mail inbox."""

from __future__ import annotations

import asyncio
import html
import re
import time
from typing import Any, Optional
from urllib.parse import unquote

import nodriver as uc

from .turnstile_bypass import _unwrap
from .email_generator import EmailGenerator


class EmailVerifyResult:
    def __init__(self, success: bool, error: str = "", link: str = ""):
        self.success = success
        self.error = error
        self.link = link


CF_VERIFY_PATTERNS = [
    # Common Cloudflare dashboard/email verification links.
    r'https://dash\.cloudflare\.com/[^\"<>\s]+',
    r'https://www\.cloudflare\.com/[^\"<>\s]+',
    r'https://cloudflare\.com/[^\"<>\s]+',
    # Worker URL pattern for email verification
    r'https://[a-z0-9-]+\.workers\.dev/[^\"<>\s]+',
]


def _mail_blob(mail: dict[str, Any]) -> str:
    parts = []
    for key in ("from", "sender", "subject", "text", "html", "body", "raw", "snippet"):
        val = mail.get(key)
        if val:
            parts.append(str(val))
    return "\n".join(parts)


def _is_cloudflare_verification(mail: dict[str, Any]) -> bool:
    blob = _mail_blob(mail).lower()
    return "cloudflare" in blob and any(
        word in blob for word in ("verify", "verification", "confirm", "activate", "email", "welcome")
    )


def extract_verification_link(mail: dict[str, Any]) -> str:
    """Extract the most likely Cloudflare verification link from parsed mail."""
    blob = html.unescape(_mail_blob(mail))
    blob = blob.replace("\\/", "/")

    candidates: list[str] = []
    for pattern in CF_VERIFY_PATTERNS:
        candidates.extend(re.findall(pattern, blob, flags=re.I))

    cleaned = []
    for url in candidates:
        url = url.rstrip("').,;]>\"\\")
        url = unquote(url)
        low = url.lower()
        # Keep likely action links, drop generic marketing/docs links.
        if any(k in low for k in ("verify", "confirm", "activation", "email", "token", "challenge", "welcome")):
            cleaned.append(url)

    # Fallback: if only dash.cloudflare.com links are present, use the first one.
    if not cleaned:
        for url in candidates:
            url = url.rstrip("').,;]>\"\\")
            if "dash.cloudflare.com" in url.lower():
                cleaned.append(unquote(url))

    return cleaned[0] if cleaned else ""


async def verify_cloudflare_email(
    page: uc.Tab,
    mail_api: str,
    jwt: str,
    timeout: int = 120,
    poll_interval: int = 5,
    email_gen: Optional[EmailGenerator] = None,
) -> EmailVerifyResult:
    """
    Poll temp inbox, open Cloudflare verification link in the same browser session.

    Args:
        page: nodriver Tab (same browser session as signup)
        mail_api: Mail API URL (used only if email_gen is None)
        jwt: JWT token from email creation (may be 'owner_token::address' format)
        timeout: Total polling timeout in seconds
        poll_interval: Base polling interval (adaptive intervals override this)
        email_gen: Pre-existing EmailGenerator with _active_url already set
                    from email creation. If None, creates a new one (loses context).
    """
    if not jwt:
        return EmailVerifyResult(False, error="missing_mail_jwt")

    print("  [verify] Waiting for Cloudflare verification email...")

    # Reuse the EmailGenerator from creation to preserve _active_url
    # (the URL that actually created the email). Creating a new one loses
    # this context and may hit a different relay that doesn't know this JWT.
    gen = email_gen
    if gen is None:
        gen = EmailGenerator(mail_api, [])
    start = time.time()

    # Adaptive polling: start fast, slow down over time
    intervals = [3, 3, 5, 5, 10, 10, 15]

    try:
        poll_idx = 0
        while time.time() - start < timeout:
            current_interval = intervals[min(poll_idx, len(intervals) - 1)]

            try:
                mails = gen.check_inbox(jwt, limit=20, offset=0)
            except Exception as e:
                print(f"  [verify] inbox error: {e}")
                await asyncio.sleep(current_interval)
                poll_idx += 1
                continue

            for mail in mails:
                mail_id = str(mail.get("id") or mail.get("mail_id") or mail.get("uid") or mail.get("_id") or "")

                full = mail
                # Some list endpoints only include metadata; fetch body by id when possible.
                if mail_id and not any(full.get(k) for k in ("text", "html", "body", "raw")):
                    try:
                        full = gen.get_mail(jwt, mail_id)
                    except Exception:
                        full = mail

                if not _is_cloudflare_verification(full):
                    continue

                link = extract_verification_link(full)
                if not link:
                    continue

                print("  [verify] Cloudflare verification link found; opening...")
                await page.get(link)
                await asyncio.sleep(15)

                body_raw = _unwrap(
                    await page.evaluate(
                        "document.body ? document.body.innerText : ''",
                        return_by_value=True,
                    )
                )
                body = str(body_raw).lower()
                url = str(
                    _unwrap(await page.evaluate("location.href", return_by_value=True))
                ).lower()

                if any(k in body for k in ("verified", "success", "email has been verified", "already verified")):
                    return EmailVerifyResult(True, link=link)
                if "dash.cloudflare.com" in url and "login" not in url:
                    return EmailVerifyResult(True, link=link)

                if "expired" in body or "invalid" in body:
                    print("  [verify] Link expired, will retry polling for a fresher email...")
                    continue

                # Assume success if link opened without error
                return EmailVerifyResult(True, link=link)

            await asyncio.sleep(current_interval)
            poll_idx += 1

        return EmailVerifyResult(False, error=f"verification_email_not_found_after_{timeout}s")
    finally:
        # Only close if we created it (don't close caller's generator)
        if email_gen is None:
            gen.close()


__all__ = ["EmailVerifyResult", "verify_cloudflare_email", "extract_verification_link"]
