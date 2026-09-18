import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from playwright_stealth import Stealth


class BrowserBridgeError(RuntimeError):
    """Base error raised by the browser-backed DeepSeek transport."""


class BrowserNotReadyError(BrowserBridgeError):
    pass


class BrowserInputUnavailableError(BrowserNotReadyError):
    """The web chat is visible but cannot currently accept a message."""


class BrowserResponseTimeout(BrowserBridgeError):
    pass


class BrowserBridge:
    CHAT_URL = os.environ.get("DEEPSEEK_CHAT_URL", "https://chat.deepseek.com")
    CHAT_INPUT_SELECTOR = "textarea[placeholder='Message DeepSeek']"
    RESPONSE_SELECTOR = ".ds-assistant-message-main-content"
    DEEPTHINK_RECHECK_EVERY = int(os.environ.get("DEEPTHINK_RECHECK_EVERY", "15"))
    RESPONSE_START_TIMEOUT = float(os.environ.get("RESPONSE_START_TIMEOUT", "30"))
    RESPONSE_COMPLETE_TIMEOUT = float(os.environ.get("RESPONSE_COMPLETE_TIMEOUT", "120"))
    STABLE_SECONDS = float(os.environ.get("RESPONSE_STABLE_SECONDS", "5"))
    INPUT_ACTION_TIMEOUT_MS = int(os.environ.get("INPUT_ACTION_TIMEOUT_MS", "10000"))
    FAILURE_COOLDOWN_SECONDS = float(os.environ.get("FAILURE_COOLDOWN_SECONDS", "30"))
    SHUTDOWN_TIMEOUT_SECONDS = float(os.environ.get("SHUTDOWN_TIMEOUT_SECONDS", "5"))
    DEEPTHINK_ENABLED = os.environ.get("DEEPTHINK_ENABLED", "1") != "0"

    def __init__(self, user_data_dir: str = "./deepseek_user_data"):
        self.playwright = None
        self.browser_context = None
        self.page = None
        self.lock = asyncio.Lock()
        self._request_count = 0
        self.user_data_dir = str(Path(user_data_dir).expanduser().resolve())
        self.ready = False
        self.last_error = None
        self._unavailable_until = 0.0

    def cooldown_error(self):
        remaining = self._unavailable_until - time.monotonic()
        if remaining <= 0:
            return None
        detail = self.last_error or "Ô chat DeepSeek tạm thời không nhận nội dung."
        return f"{detail} Bridge tạm nghỉ {remaining:.0f}s để tránh retry làm máy quá tải."

    def _mark_input_unavailable(self, message: str):
        self.ready = False
        self.last_error = message
        self._unavailable_until = max(
            self._unavailable_until,
            time.monotonic() + self.FAILURE_COOLDOWN_SECONDS,
        )

    def _raise_during_cooldown(self):
        message = self.cooldown_error()
        if message:
            raise BrowserInputUnavailableError(message)

    async def ensure_deepthink_enabled(self):
        """Best-effort toggle; failure here must not mark the browser unhealthy."""
        if not self.DEEPTHINK_ENABLED or not self.page or self.page.is_closed():
            return
        try:
            deepthink_btn = self.page.locator(
                "div.ds-toggle-button",
                has=self.page.get_by_text("DeepThink", exact=True),
            )
            await deepthink_btn.wait_for(state="visible", timeout=3000)
            if await deepthink_btn.get_attribute("aria-pressed") == "false":
                await deepthink_btn.click()
                await self.page.wait_for_timeout(300)
        except Exception as exc:
            print(f"⚠️ Không thể kiểm tra DeepThink (tiếp tục không DeepThink): {exc}")

    async def _wait_for_chat_input(self, timeout_ms: int = 15000):
        if not self.page or self.page.is_closed():
            raise BrowserNotReadyError("Trang DeepSeek chưa được khởi tạo hoặc đã đóng.")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        try:
            chat_input = await self.page.wait_for_selector(
                self.CHAT_INPUT_SELECTOR,
                state="visible",
                timeout=timeout_ms,
            )
            while loop.time() < deadline:
                try:
                    if await chat_input.is_editable():
                        return chat_input
                except PlaywrightError:
                    # DeepSeek can replace the textarea while hydrating the page.
                    remaining_ms = max(int((deadline - loop.time()) * 1000), 1)
                    chat_input = await self.page.wait_for_selector(
                        self.CHAT_INPUT_SELECTOR,
                        state="visible",
                        timeout=min(remaining_ms, 1000),
                    )
                await asyncio.sleep(0.1)
            raise PlaywrightTimeoutError("DeepSeek chat input is not editable.")
        except PlaywrightTimeoutError as exc:
            self._mark_input_unavailable("DeepSeek chat input is unavailable.")
            self.last_error = "Không tìm thấy khung chat; phiên đăng nhập có thể đã hết hạn."
            raise BrowserInputUnavailableError(self.last_error) from exc

    async def initialize(self):
        print("Khởi động Playwright ẩn...")
        self.ready = False
        self.last_error = None
        try:
            self.playwright = await async_playwright().start()
            launch_args = ["--disable-blink-features=AutomationControlled"]
            if os.environ.get("BROWSER_NO_SANDBOX") == "1":
                launch_args.append("--no-sandbox")

            self.browser_context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                headless=os.environ.get("BROWSER_HEADLESS", "1") != "0",
                args=launch_args,
            )
            await Stealth().apply_stealth_async(self.browser_context)
            self.page = (
                self.browser_context.pages[0]
                if self.browser_context.pages
                else await self.browser_context.new_page()
            )
            await self.page.goto(self.CHAT_URL, wait_until="domcontentloaded", timeout=30000)
            await self._wait_for_chat_input()
            self.ready = True
            print("✅ DeepSeek Engine đã sẵn sàng nhận lệnh!")
            await self.ensure_deepthink_enabled()
        except Exception as exc:
            self.last_error = str(exc)
            await self.close()
            if isinstance(exc, BrowserBridgeError):
                raise
            raise BrowserNotReadyError(f"Không thể khởi tạo trình duyệt: {exc}") from exc

    async def check_ready(self, timeout_ms: int = 750) -> bool:
        if self.cooldown_error():
            return False
        if self.lock.locked():
            return self.ready
        if not self.page or self.page.is_closed():
            return False
        try:
            chat_input = await self.page.wait_for_selector(
                self.CHAT_INPUT_SELECTOR,
                state="visible",
                timeout=timeout_ms,
            )
            if not await chat_input.is_editable():
                self._mark_input_unavailable("DeepSeek chat input is not editable.")
                return False
            self.ready = True
            self.last_error = None
            return True
        except Exception as exc:
            self.ready = False
            self.last_error = str(exc)
            return False

    async def close(self):
        self.ready = False
        context, playwright = self.browser_context, self.playwright
        self.browser_context = None
        self.playwright = None
        self.page = None
        if context:
            try:
                await asyncio.wait_for(
                    context.close(),
                    timeout=self.SHUTDOWN_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                print(f"⚠️ Chromium đã đóng trước khi dọn context xong: {exc}")
        if playwright:
            try:
                await asyncio.wait_for(
                    playwright.stop(),
                    timeout=self.SHUTDOWN_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                print(f"⚠️ Playwright driver đã đóng trong lúc dọn dẹp: {exc}")

    async def start_new_conversation(self):
        """Reset hidden web-chat state; the API request supplies the complete transcript."""
        self._raise_during_cooldown()
        if not self.page or self.page.is_closed():
            raise BrowserNotReadyError(self.last_error or "Trình duyệt chưa sẵn sàng.")
        try:
            await self.page.goto(self.CHAT_URL, wait_until="domcontentloaded", timeout=30000)
            await self._wait_for_chat_input()
        except Exception as exc:
            self.ready = False
            self.last_error = str(exc)
            if isinstance(exc, BrowserBridgeError):
                raise
            raise BrowserNotReadyError(f"Không thể mở cuộc trò chuyện mới: {exc}") from exc

    @asynccontextmanager
    async def conversation(self, reset: bool = True):
        """Keep an entire request (including repair turns) isolated and serialized."""
        async with self.lock:
            if reset:
                await self.start_new_conversation()
            yield self

    async def send_message_to_deepseek(self, message: str) -> str:
        """Send inside conversation() and return only a newly-created response."""
        if not self.lock.locked():
            raise RuntimeError("send_message_to_deepseek phải chạy bên trong conversation().")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("Nội dung gửi đến DeepSeek không được rỗng.")

        self._raise_during_cooldown()
        self._request_count += 1
        if self._request_count % self.DEEPTHINK_RECHECK_EVERY == 0:
            await self.ensure_deepthink_enabled()

        chat_input = await self._wait_for_chat_input()
        old_responses = await self.page.query_selector_all(self.RESPONSE_SELECTOR)
        old_count = len(old_responses)
        baseline_text = await old_responses[-1].inner_text() if old_responses else None

        try:
            await chat_input.fill(message, timeout=self.INPUT_ACTION_TIMEOUT_MS)
            await chat_input.press("Enter", timeout=self.INPUT_ACTION_TIMEOUT_MS)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            error_message = (
                "DeepSeek không cho phép nhập hoặc gửi tin nhắn trong thời gian chờ; "
                "hãy kiểm tra phiên đăng nhập và giao diện web."
            )
            self._mark_input_unavailable(error_message)
            raise BrowserInputUnavailableError(error_message) from exc
        print("⏳ Đang đợi DeepSeek bắt đầu trả lời...")

        loop = asyncio.get_running_loop()
        start_wait = loop.time()
        response_handle = None
        while loop.time() - start_wait <= self.RESPONSE_START_TIMEOUT:
            responses = await self.page.query_selector_all(self.RESPONSE_SELECTOR)
            if responses:
                candidate = responses[-1]
                current_text = await candidate.inner_text()
                if len(responses) > old_count or (
                    baseline_text is not None and current_text != baseline_text
                ) or (baseline_text is None and current_text.strip()):
                    response_handle = candidate
                    break
            await asyncio.sleep(0.25)

        if response_handle is None:
            raise BrowserResponseTimeout(
                f"DeepSeek không tạo phản hồi mới sau {self.RESPONSE_START_TIMEOUT:.0f}s."
            )

        print("⏳ Đang đợi DeepSeek trả lời xong...")
        last_text = await response_handle.inner_text()
        last_change = loop.time()
        completion_start = loop.time()

        while True:
            if loop.time() - completion_start > self.RESPONSE_COMPLETE_TIMEOUT:
                raise BrowserResponseTimeout(
                    f"DeepSeek chưa hoàn thành sau {self.RESPONSE_COMPLETE_TIMEOUT:.0f}s; "
                    "không trả nội dung một phần để tránh tool call bị cắt."
                )
            await asyncio.sleep(0.4)
            try:
                current_text = await response_handle.inner_text()
            except Exception:
                responses = await self.page.query_selector_all(self.RESPONSE_SELECTOR)
                if not responses:
                    raise BrowserResponseTimeout("Bubble phản hồi biến mất trước khi hoàn thành.")
                response_handle = responses[-1]
                current_text = await response_handle.inner_text()

            if current_text != last_text:
                last_text = current_text
                last_change = loop.time()
                continue
            if current_text.strip() and loop.time() - last_change >= self.STABLE_SECONDS:
                print("✅ DeepSeek đã trả lời xong!")
                return current_text
