"""
GremlinAI — browser session wrapper.

Wraps Playwright's async Chromium API with:
- Automatic event listeners (console, pageerror, response, requestfailed)
- Credential substitution at execution time (never stored anywhere)
- CSS blur on password inputs before every screenshot/frame
- Auto-dialog dismissal
- Background JPEG frame streaming to the event bus
"""
from __future__ import annotations
import asyncio
import base64
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
)

from gremlinai.events import Event, EventType

if TYPE_CHECKING:
    from gremlinai.events import EventBus
    from gremlinai.models import AuthConfig

logger = logging.getLogger(__name__)

# CSS injected on every page to blur password fields in screenshots/frames
_BLUR_CSS = "input[type=password] { filter: blur(6px) !important; }"

# Placeholder tokens used in prompts/plans
_PH_USERNAME = "{{USERNAME}}"
_PH_PASSWORD = "{{PASSWORD}}"
_PH_EXTRA_PREFIX = "{{EXTRA_"


class BrowserSession:
    """
    Async Playwright Chromium session. One instance per run.
    """

    def __init__(
        self,
        run_dir: Path,
        username: str = "",
        password: str = "",
        extra_fields: dict[str, str] | None = None,
    ) -> None:
        self._run_dir = run_dir
        self._username = username
        self._password = password         # in-memory only, never logged/stored
        self._extra = extra_fields or {}

        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

        # Captured during the run
        self.console_errors: list[str] = []
        self.failed_requests: list[dict] = []

        self._frame_task: asyncio.Task | None = None
        self._streaming = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self, storage_state_path: str | None = None) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        ctx_kwargs: dict = {"viewport": {"width": 1280, "height": 800}}
        if storage_state_path:
            ctx_kwargs["storage_state"] = storage_state_path
        self._context = await self._browser.new_context(**ctx_kwargs)

        # Auto-dismiss unexpected dialogs
        self._context.on("page", self._on_new_page)
        # Auto-inject blur CSS for all documents and navigations
        await self._context.add_init_script(f"""
(() => {{
    const id = '__gremlin_blur_css__';
    if (!document.getElementById(id)) {{
        const s = document.createElement('style');
        s.id = id;
        s.textContent = `{_BLUR_CSS}`;
        (document.head || document.documentElement).appendChild(s);
    }}
}})();
""")
        self._page = await self._context.new_page()
        await self._attach_listeners(self._page)
        try:
            await self._page.add_style_tag(content=_BLUR_CSS)
        except Exception:
            pass

    async def _on_new_page(self, page: Page) -> None:
        await self._attach_listeners(page)
        await page.add_style_tag(content=_BLUR_CSS)
        # Switch focus to the new page
        self._page = page

    async def _attach_listeners(self, page: Page) -> None:
        page.on("console", lambda msg: self._on_console(msg))
        page.on("pageerror", lambda err: self.console_errors.append(f"PageError: {err}"))
        page.on("response", lambda r: self._on_response(r))
        page.on("requestfailed", lambda r: self.failed_requests.append(
            {"url": r.url, "failure": r.failure or "unknown"}
        ))
        page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))

    def _on_console(self, msg) -> None:
        if msg.type in ("error", "warning"):
            self.console_errors.append(f"[{msg.type}] {msg.text}")

    def _on_response(self, response) -> None:
        if response.status >= 400:
            self.failed_requests.append({"url": response.url, "status": response.status})

    # ── Navigation + actions ──────────────────────────────────────────────────

    async def goto(self, url: str) -> None:
        assert self._page
        await self._page.goto(url, wait_until="domcontentloaded", timeout=15_000)
        await self.wait_for_settled()

    async def click(self, selector: str) -> None:
        assert self._page
        await self._page.click(selector, timeout=5_000)
        await asyncio.sleep(0.3)

    async def type_text(self, selector: str, text: str) -> None:
        """Type text, substituting credential placeholders."""
        assert self._page
        real = self._substitute(text)
        await self._page.click(selector, timeout=5_000)
        await self._page.fill(selector, real)

    async def select_option(self, selector: str, value: str) -> None:
        assert self._page
        real = self._substitute(value)
        await self._page.select_option(selector, value=real)

    async def press_key(self, selector: str, key: str) -> None:
        assert self._page
        await self._page.press(selector, key)

    async def scroll(self, direction: str = "down", amount: int = 300) -> None:
        assert self._page
        delta = amount if direction == "down" else -amount
        await self._page.evaluate(f"window.scrollBy(0, {delta})")
        await asyncio.sleep(0.2)

    async def wait(self, ms: int = 1000) -> None:
        await asyncio.sleep(ms / 1000)

    async def wait_for_settled(self) -> None:
        """Wait for network idle (max 2s) + DOM stability."""
        assert self._page
        try:
            await self._page.wait_for_load_state("networkidle", timeout=2_000)
        except Exception:
            pass  # timeout is fine — page may be a SPA
        await asyncio.sleep(0.3)

    async def current_url(self) -> str:
        assert self._page
        return self._page.url

    # ── Screenshots ───────────────────────────────────────────────────────────

    async def screenshot(self, filename: str) -> bytes:
        assert self._page
        path = self._run_dir / filename
        # Re-inject blur CSS (might have been lost on navigation)
        try:
            await self._page.add_style_tag(content=_BLUR_CSS)
        except Exception:
            pass
        data = await self._page.screenshot(path=str(path), type="jpeg", quality=80)
        return data

    async def screenshot_bytes(self) -> bytes:
        if not self._page or self._page.is_closed():
            return b""
        try:
            return await self._page.screenshot(type="jpeg", quality=65)
        except Exception:
            return b""

    # ── Frame streaming ───────────────────────────────────────────────────────

    def start_streaming(self, run_id: str, bus: "EventBus") -> None:
        self._streaming = True
        self._frame_task = asyncio.create_task(
            self._stream_loop(run_id, bus), name="frame-stream"
        )

    def stop_streaming(self) -> None:
        self._streaming = False
        if self._frame_task:
            self._frame_task.cancel()

    async def emit_frame(self, run_id: str, bus: "EventBus") -> None:
        """Immediately emit a frame to update the UI on key actions."""
        if not self._page or self._page.is_closed():
            return
        try:
            data = await self.screenshot_bytes()
            if data:
                b64 = base64.b64encode(data).decode()
                url = self._page.url
                title = ""
                try:
                    title = await self._page.title()
                except Exception:
                    pass
                await bus.publish(Event(
                    type=EventType.FRAME,
                    run_id=run_id,
                    data={"jpeg_b64": b64, "url": url, "title": title},
                ))
        except Exception:
            pass

    async def _stream_loop(self, run_id: str, bus: "EventBus") -> None:
        from gremlinai.events import EventType, Event

        while self._streaming:
            try:
                data = await self.screenshot_bytes()
                if data:
                    b64 = base64.b64encode(data).decode()
                    url = ""
                    title = ""
                    try:
                        if self._page and not self._page.is_closed():
                            url = self._page.url
                            title = await self._page.title()
                    except Exception:
                        pass
                    await bus.publish(Event(
                        type=EventType.FRAME,
                        run_id=run_id,
                        data={"jpeg_b64": b64, "url": url, "title": title},
                    ))
            except Exception:
                pass
            await asyncio.sleep(0.25)   # ~4 fps responsive live preview

    # ── Storage state (auth persistence) ─────────────────────────────────────

    async def save_storage_state(self, path: str) -> None:
        assert self._context
        await self._context.storage_state(path=path)

    # ── Credential substitution ───────────────────────────────────────────────

    def _substitute(self, text: str) -> str:
        """Replace {{USERNAME}}, {{PASSWORD}}, {{EXTRA_*}} with real values."""
        text = text.replace(_PH_USERNAME, self._username)
        text = text.replace(_PH_PASSWORD, self._password)
        for k, v in self._extra.items():
            text = text.replace(f"{_PH_EXTRA_PREFIX}{k}}}", v)
        return text

    def scrub(self, text: str) -> str:
        """Redact known secrets from any string (for logs/events)."""
        if self._password and self._password in text:
            text = text.replace(self._password, "***")
        return text

    # ── Teardown ──────────────────────────────────────────────────────────────

    async def close(self) -> None:
        self.stop_streaming()
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
