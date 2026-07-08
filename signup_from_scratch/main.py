#!/usr/bin/env python3
"""
Cloudflare Auto Signup — Main Orchestrator

Automates the full pipeline:
1. Generate temp email
2. Sign up for Cloudflare account (with Turnstile bypass)
3. Create Account API Token (Workers AI permissions)
4. Validate token against Workers AI API
5. Save results to JSON

Usage:
    python main.py                          # Create 1 account
    python main.py --accounts 5             # Create 5 accounts
    python main.py --fast                   # Skip Turnstile solve (submit-first)
    python main.py --proxy http://user:pass@host:port
    python main.py --config custom.json
    python main.py --validate-only --token cfut_xxx --account-id xxx

Requirements:
    - Python 3.10+
    - Google Chrome installed
    - nodriver, opencv-python-headless, httpx, Pillow, rich
    - Works on Windows, Linux, macOS
"""

import argparse
import asyncio
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import nodriver as uc

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.email_generator import EmailGenerator
from src.signup_flow import signup
from src.email_verifier import verify_cloudflare_email
from src.token_creator import create_token, create_token_api
from src.token_validator import validate_token
from src.live_dashboard import DashboardState, LiveDashboard
from src.utils import (
    generate_password,
    generate_username,
    load_config,
    save_result,
    load_results,
    timestamp,
    wait_with_progress,
    format_account,
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Automated Cloudflare account creation with Workers AI tokens"
    )
    parser.add_argument(
        "--accounts", "-n", type=int, default=1,
        help="Number of accounts to create (default: 1)"
    )
    parser.add_argument(
        "--config", "-c", type=str, default="config.json",
        help="Config file path (default: config.json)"
    )
    parser.add_argument(
        "--proxy", "-p", type=str, default=None,
        help="Proxy URL (http://user:pass@host:port) or file path with one proxy per line"
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Output JSON file (default: from config)"
    )
    parser.add_argument(
        "--delay", "-d", type=int, default=None,
        help="Delay between accounts in seconds (default: from config)"
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run browser in headless mode"
    )
    parser.add_argument(
        "--validate-only", action="store_true",
        help="Only validate an existing token (requires --token and --account-id)"
    )
    parser.add_argument("--token", type=str, help="Token to validate")
    parser.add_argument("--account-id", type=str, help="Account ID for validation")
    parser.add_argument(
        "--retry", type=int, default=3,
        help="Number of retry attempts per account (default: 3)"
    )
    parser.add_argument(
        "--workers", "-w", type=int, default=1,
        help="Number of concurrent account workers (default: 1; use carefully with proxies)"
    )
    parser.add_argument(
        "--mail-api", type=str, default=None,
        help="Mail API URL override (e.g. https://relay.example.com/new_address)"
    )
    parser.add_argument(
        "--no-dashboard", action="store_true",
        help="Disable Rich live dashboard even when rich is installed"
    )
    parser.add_argument(
        "--export-txt", type=str, default=None,
        help="Export valid results to a 9Router-friendly .txt file after the run"
    )
    parser.add_argument(
        "--fast", action="store_true",
        help="Submit-first mode: quick Turnstile interaction, submit immediately, retry only if blocked"
    )
    parser.add_argument(
        "--window-size", type=str, default="1400,1000",
        help="Browser window size WxH (default: 1400,1000)"
    )
    parser.add_argument(
        "--wait-captcha", action="store_true",
        help="Hybrid mode: pause at CAPTCHA for manual solve before continuing automation"
    )
    return parser.parse_args()


async def create_account(
    config: dict,
    proxy: str = None,
    headless: bool = False,
    browser: uc.Browser = None,
    fast: bool = False,
    window_size: str = "1400,1000",
    wait_captcha: bool = False,
) -> dict:
    """
    Create a single Cloudflare account with API token.

    Returns:
        dict with account info and token (or error)
    """
    # Generate credentials
    username = generate_username()
    domain = random.choice(config["mail_domains"])
    password = generate_password()
    token_name = config.get("token_name", "workers-ai-auto")
    mail_api = config.get("mail_api", "https://convergence-lobby-portal-planes.trycloudflare.com/new_address")

    # Create temp email
    # Keep email_gen alive — pass it to verify_cloudflare_email so it can
    # reuse _active_url (the relay that actually created the email).
    # Closing it here loses the relay context and breaks inbox polling.
    email_gen = EmailGenerator(mail_api, config["mail_domains"], fallback_url=config.get("mail_fallback"))
    try:
        mail = email_gen.create(username=username, domain=domain)
        email = mail["email"]
    except Exception as e:
        email_gen.close()
        return {"status": "error", "error": f"Email creation failed: {e}", "email": f"{username}@{domain}"}

    print(f"  📧 {email}")

    # Use provided browser or create new one
    own_browser = False
    if browser is None:
        # Parse window size
        try:
            w, h = map(int, window_size.split(","))
        except:
            w, h = 1400, 1000
        
        browser = await uc.start(
            headless=headless,
            lang="en-US",
            proxy=proxy,
            sandbox=False,  # required when running as root in VPS/Xvfb
            browser_args=[
                f"--window-size={w},{h}",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=ChromeWhatsNewUI",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        own_browser = True

    try:
        # Phase 0: Navigate to signup
        print("  [0/4] Pre-flight check...")
        try:
            page = await asyncio.wait_for(
                browser.get("https://dash.cloudflare.com/sign-up"),
                timeout=30,
            )
        except asyncio.TimeoutError:
            return {
                "status": "error",
                "email": email,
                "password": password,
                "error": "Page load timeout (30s) — check proxy or network",
            }
        await asyncio.sleep(8)
        print("    ✅ Page ready")
            
        # Phase 1: Signup
        print("  [1/4] Signing up...")
        signup_result = await signup(
            page, email, password, 
            retry_turnstile=not fast,
            wait_captcha=wait_captcha
        )

        if not signup_result.success:
            return {
                "status": "error",
                "email": email,
                "password": password,
                "error": f"Signup failed: {signup_result.error}",
            }

        account_id = signup_result.account_id
        print(f"  🆔 Account ID: {account_id}")

        # Verify Cloudflare email from temp inbox before token creation.
        # Pass email_gen so verify can reuse the same _active_url relay
        # that created the email (creating a new EmailGenerator loses context).
        print("  [2/4] Verifying Cloudflare email...")
        verify_result = await verify_cloudflare_email(
            page,
            mail_api=mail_api,
            jwt=mail.get("jwt", ""),
            timeout=config.get("email_verify_timeout", 120),
            poll_interval=config.get("email_verify_poll_interval", 5),
            email_gen=email_gen,
        )
        if verify_result.success:
            print("  ✅ Email verified")
        else:
            print(f"  ⚠️ Email verification failed: {verify_result.error}")

        # Now safe to close email_gen — verification polling is done
        email_gen.close()

        # Phase 2: Token creation — reuse same browser session.
        # After email verification, the direct dashboard API is the most stable path;
        # keep the UI flow as fallback/debug for unverified or API-blocked cases.
        print("  [3/4] Creating API token...")
        if verify_result.success:
            token_result = await create_token_api(page, account_id, token_name)
            if not token_result.success:
                print(f"  ⚠️ API token creation failed after verify: {token_result.error}; trying UI fallback")
                token_result = await create_token(page, account_id, token_name)
        else:
            token_result = await create_token(page, account_id, token_name)

        api_token = token_result.token if token_result.success else ""
        if api_token:
            print(f"  🔑 Token: {api_token[:30]}...")
        else:
            print(f"  ⚠️ Token creation failed: {token_result.error}")

        # Phase 3: Validation (even without token, save the account)
        token_valid = False
        model_count = 0
        if api_token:
            print("  [4/4] Validating token...")
            validation = validate_token(api_token, account_id)
            token_valid = validation.valid
            model_count = validation.workers_ai_models
            if token_valid:
                print(f"  ✅ Valid! {model_count} Workers AI models available")
            else:
                print(f"  ❌ Validation failed: {validation.error}")
        else:
            print("  [4/4] Skipping validation (no token)")

        result = {
            "email": email,
            "password": password,
            "jwt": mail.get("jwt", ""),
            "account_id": account_id,
            "api_token": api_token,
            "token_valid": token_valid,
            "workers_ai_models": model_count,
            "token_name": token_name,
            "email_verified": verify_result.success,
            "email_verify_error": "" if verify_result.success else verify_result.error,
            "status": "full" if token_valid else ("signup_only" if account_id else "error"),
            "created_at": timestamp(),
            "proxy_used": proxy or "direct",
        }
        return result

    except Exception as e:
        # Ensure email_gen is closed if we crashed before normal close
        try:
            email_gen.close()
        except Exception:
            pass
        return {
            "status": "error",
            "email": email,
            "password": password,
            "error": str(e),
            "created_at": timestamp(),
        }
    finally:
        if own_browser:
            try:
                browser.stop()
            except Exception:
                pass
            # Give nodriver/CDP websocket subprocess a moment to close cleanly before
            # starting the next account browser. This avoids intermittent
            # "no close frame received or sent" / "Connection closed" on bulk runs.
            await asyncio.sleep(3)


def export_txt(results: list[dict], output_path: str, proxy_pool: str = "None") -> int:
    """Export valid tokens in the exact 9Router form-friendly txt shape."""
    lines = ["Cloudflare Workers AI keys for 9Router", ""]
    count = 0
    for result in results:
        token = result.get("api_token", "")
        account_id = result.get("account_id", "")
        if token.startswith("cfut_") and account_id and result.get("token_valid"):
            count += 1
            lines.extend([
                f"[{count}]",
                f"Name: {result.get('email', f'cloudflare-key-{count}')}",
                f"API Key: {token}",
                f"Account ID: {account_id}",
                "Priority: 1",
                f"Proxy Pool: {proxy_pool}",
                "",
            ])
    lines.extend(["--- Summary ---", f"Valid keys: {count}"])
    Path(output_path).write_text("\n".join(lines) + "\n")
    return count


async def main():
    # Ensure cwd is the script's directory (CLI may run from elsewhere)
    os.chdir(Path(__file__).parent)
    args = parse_args()

    # Load config
    config = load_config(args.config)
    proxy_arg = args.proxy or config.get("proxy")
    output_file = args.output or config.get("output_file", "results.json")
    delay = args.delay if args.delay is not None else config.get("delay_between_accounts", 300)
    num_accounts = args.accounts
    max_retry = args.retry

    # Proxy rotation: if proxy_arg is a file path, load proxy list for rotation
    proxy_pool: list[str] = []
    single_proxy: str = None
    if proxy_arg:
        proxy_path = Path(proxy_arg)
        if proxy_path.exists() and proxy_path.is_file():
            # Load proxy list from file
            for line in proxy_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    proxy_pool.append(line)
            print(f"  🔄 Loaded {len(proxy_pool)} proxies from {proxy_arg}")
        else:
            # Single proxy URL
            single_proxy = proxy_arg
            proxy_pool = [proxy_arg]

    def get_proxy_for_account(index: int) -> str:
        """Round-robin proxy selection for bulk runs."""
        if not proxy_pool:
            return None
        return proxy_pool[index % len(proxy_pool)]

    # Override mail API from CLI
    if args.mail_api:
        config["mail_api"] = args.mail_api
        print(f"  📧 Mail API override: {args.mail_api}")

    # Validate-only mode
    if args.validate_only:
        if not args.token or not args.account_id:
            print("❌ --validate-only requires --token and --account-id")
            sys.exit(1)
        print("🔍 Validating token...")
        result = validate_token(args.token, args.account_id)
        print(f"  Valid: {result.valid}")
        print(f"  Models: {result.workers_ai_models}")
        if result.error:
            print(f"  Error: {result.error}")
        sys.exit(0 if result.valid else 1)

    print("=" * 60)
    print("☁️  Cloudflare Auto Signup — Workers AI Token Creator")
    print("=" * 60)
    print(f"  Accounts to create: {num_accounts}")
    print(f"  Proxy: {single_proxy or (f'{len(proxy_pool)} proxies (rotation)' if proxy_pool else 'None (direct)')}")
    print(f"  Delay between: {delay}s")
    print(f"  Output: {output_file}")
    print(f"  Headless: {args.headless or config.get('headless', False)}")
    print("=" * 60)

    # Create accounts
    workers = max(1, args.workers)
    created = 0
    failed = 0
    results = load_results(output_file)
    run_results: list[dict] = []
    save_lock = asyncio.Lock()
    queue: asyncio.Queue[int] = asyncio.Queue()
    for i in range(num_accounts):
        queue.put_nowait(i + 1)

    dashboard_state = DashboardState(total=num_accounts, workers=workers)

    async def run_one(worker_id: int, index: int) -> dict:
        dashboard_state.update(worker_id, "signup", "Starting signup", index=index)
        result: dict = {"status": "error", "error": "not_started"}
        success = False
        for attempt in range(max_retry):
            if attempt > 0:
                dashboard_state.update(worker_id, "signup", f"Retry {attempt}/{max_retry - 1}", index=index)
                await asyncio.sleep(30)

            result = await create_account(
                config=config,
                proxy=get_proxy_for_account(index),
                headless=args.headless or config.get("headless", False),
                fast=args.fast,
                window_size=args.window_size,
                wait_captcha=args.wait_captcha,
            )
            if result.get("email"):
                dashboard_state.update(worker_id, "validate", result.get("status", "done"), email=result["email"], index=index)

            if result.get("status") == "full":
                success = True
                break
            if result.get("status") == "signup_only":
                # Account created but no token — still save for forensic/debug visibility.
                success = True
                break

            error_str = str(result.get("error", "")).lower()

            # Fatal errors — retrying won't help
            if any(
                marker in error_str
                for marker in ("all mail relays failed", "connection refused", "connectionerror")
            ):
                break

            # Transient errors — wait and retry (DON'T break)
            if any(
                marker in error_str
                for marker in ("rate", "unable", "connection closed", "no close frame", "timeout")
            ):
                dashboard_state.update(worker_id, "failed", "Transient issue; retry cooldown", email=result.get("email", ""), index=index)
                await asyncio.sleep(delay)
                continue  # ← retry the loop

            # Unknown error — retry once more if attempts remain
            # (previously: else: break → killed ALL retries)
            if attempt < max_retry - 1:
                continue
            break

        async with save_lock:
            save_result(result, output_file)
            results.append(result)
            run_results.append(result)

        dashboard_state.finish(worker_id, success, format_account(result) if success else result.get("error", "unknown"))
        return result

    async def worker(worker_id: int):
        while not queue.empty():
            index = await queue.get()
            print(f"\n{'─' * 50}\n  Account {index}/{num_accounts} (Worker {worker_id})\n{'─' * 50}")
            try:
                await run_one(worker_id, index)
            finally:
                queue.task_done()
                if delay and not queue.empty():
                    dashboard_state.update(worker_id, "queued", f"Cooldown {delay}s")
                    await asyncio.sleep(delay)

    with LiveDashboard(dashboard_state, enabled=not args.no_dashboard) as live:
        worker_tasks = [asyncio.create_task(worker(wid)) for wid in range(1, workers + 1)]
        while any(not task.done() for task in worker_tasks):
            live.refresh()
            await asyncio.sleep(0.5)
        await asyncio.gather(*worker_tasks)
        live.refresh()

    created = sum(1 for r in run_results if r.get("status") in ("full", "signup_only"))
    failed = len(run_results) - created

    # Final summary
    print(f"\n{'=' * 60}")
    print(f"📊 Results: {created} created, {failed} failed")
    print(f"💾 Saved to: {output_file}")
    if args.export_txt:
        exported = export_txt(results, args.export_txt, proxy_pool=("None" if not proxy_pool else f"{len(proxy_pool)}-proxy-rotation"))
        print(f"🧩 9Router TXT export: {args.export_txt} ({exported} valid keys)")
    if run_results:
        print(f"\nAccounts:")
        for r in run_results:
            print(format_account(r))
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(main())
