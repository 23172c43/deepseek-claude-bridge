import asyncio
import os
import re
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
    CHAT_URL = os.environ.get(
        "DEEPSEEK_CHAT_URL",
        "https://chat.deepseek.com",
    )
    CHAT_INPUT_SELECTOR = "textarea[placeholder='Message DeepSeek']"
    RESPONSE_SELECTOR = ".ds-assistant-message-main-content"

    DEEPTHINK_RECHECK_EVERY = int(
        os.environ.get("DEEPTHINK_RECHECK_EVERY", "15")
    )
    RESPONSE_START_TIMEOUT = float(
        os.environ.get("RESPONSE_START_TIMEOUT", "30")
    )
    RESPONSE_COMPLETE_TIMEOUT = float(
        os.environ.get("RESPONSE_COMPLETE_TIMEOUT", "120")
    )
    STABLE_SECONDS = float(
        os.environ.get("RESPONSE_STABLE_SECONDS", "0.8")
    )
    POLL_INTERVAL_SECONDS = float(
        os.environ.get("RESPONSE_POLL_INTERVAL", "0.15")
    )
    INPUT_ACTION_TIMEOUT_MS = int(
        os.environ.get("INPUT_ACTION_TIMEOUT_MS", "5000")
    )
    FAILURE_COOLDOWN_SECONDS = float(
        os.environ.get("FAILURE_COOLDOWN_SECONDS", "30")
    )
    SHUTDOWN_TIMEOUT_SECONDS = float(
        os.environ.get("SHUTDOWN_TIMEOUT_SECONDS", "5")
    )

    DEEPTHINK_ENABLED = os.environ.get("DEEPTHINK_ENABLED", "0") != "0"
    NEW_CHAT_VIA_UI = os.environ.get("NEW_CHAT_VIA_UI", "1") != "0"

    def __init__(self, user_data_dir: str = "./deepseek_user_data"):
        self.playwright = None
        self.browser_context = None
        self.page = None

        self.lock = asyncio.Lock()

        self._request_count = 0
        self.user_data_dir = str(
            Path(user_data_dir).expanduser().resolve()
        )
        self.ready = False
        self.last_error = None
        self._unavailable_until = 0.0

    def cooldown_error(self):
        remaining = self._unavailable_until - time.monotonic()
        if remaining <= 0:
            return None

        detail = (
            self.last_error
            or "Ô chat DeepSeek tạm thời không nhận nội dung."
        )
        return (
            f"{detail} Bridge tạm nghỉ {remaining:.0f}s "
            "để tránh retry làm máy quá tải."
        )

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
        if (
            not self.DEEPTHINK_ENABLED
            or not self.page
            or self.page.is_closed()
        ):
            return

        try:
            button = self.page.locator(
                "div.ds-toggle-button",
                has=self.page.get_by_text(
                    "DeepThink",
                    exact=True,
                ),
            )
            await button.wait_for(
                state="visible",
                timeout=1500,
            )

            if await button.get_attribute("aria-pressed") == "false":
                await button.click()
                await self.page.wait_for_timeout(200)

        except Exception as exc:
            print(
                "⚠️ Không thể kiểm tra DeepThink "
                f"(tiếp tục): {exc}"
            )

    async def _wait_for_chat_input(self, timeout_ms: int = 10000):
        if not self.page or self.page.is_closed():
            raise BrowserNotReadyError(
                "Trang DeepSeek chưa được khởi tạo hoặc đã đóng."
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000

        try:
            chat_input = self.page.locator(
                self.CHAT_INPUT_SELECTOR
            ).first

            await chat_input.wait_for(
                state="visible",
                timeout=timeout_ms,
            )

            while True:
                try:
                    if await chat_input.is_editable():
                        return chat_input
                except PlaywrightError:
                    pass

                if loop.time() >= deadline:
                    break

                await asyncio.sleep(
                    min(
                        0.05,
                        max(deadline - loop.time(), 0),
                    )
                )

            raise PlaywrightTimeoutError(
                "DeepSeek chat input is not editable."
            )

        except PlaywrightTimeoutError as exc:
            self._mark_input_unavailable(
                "DeepSeek chat input is unavailable."
            )
            self.last_error = (
                "Không tìm thấy khung chat; "
                "phiên đăng nhập có thể đã hết hạn."
            )
            raise BrowserInputUnavailableError(
                self.last_error
            ) from exc

    async def _fill_chat_input(self, chat_input, message: str):
        try:
            await chat_input.fill(
                message,
                timeout=self.INPUT_ACTION_TIMEOUT_MS,
            )
            return

        except (PlaywrightTimeoutError, PlaywrightError):
            print(
                "⚠️ fill() không thành công; "
                "thử native textarea setter..."
            )

        try:
            if await chat_input.count() == 0:
                raise BrowserInputUnavailableError(
                    "Ô chat DeepSeek đã biến mất trước khi nhập."
                )
        except PlaywrightError as exc:
            raise BrowserInputUnavailableError(
                "Không thể kiểm tra lại ô chat DeepSeek."
            ) from exc

        try:
            await asyncio.wait_for(
                chat_input.evaluate(
                    """(element, value) => {
                        const descriptor =
                            Object.getOwnPropertyDescriptor(
                                HTMLTextAreaElement.prototype,
                                "value"
                            );

                        if (!descriptor || !descriptor.set) {
                            throw new Error(
                                "Không tìm thấy native textarea value setter"
                            );
                        }

                        descriptor.set.call(element, value);
                        element.dispatchEvent(
                            new Event("input", { bubbles: true })
                        );
                        element.focus();
                    }""",
                    message,
                ),
                timeout=max(
                    self.INPUT_ACTION_TIMEOUT_MS / 1000,
                    0.1,
                ),
            )

            if await chat_input.input_value() != message:
                raise BrowserInputUnavailableError(
                    "DeepSeek không ghi nhận nội dung sau fallback."
                )

        except (
            asyncio.TimeoutError,
            PlaywrightTimeoutError,
            PlaywrightError,
        ) as exc:
            raise BrowserInputUnavailableError(
                "Không thể cập nhật ô chat DeepSeek."
            ) from exc

    async def initialize(self):
        print("Khởi động Playwright ẩn...")
        self.ready = False
        self.last_error = None

        try:
            self.playwright = await async_playwright().start()

            launch_args = [
                "--disable-blink-features=AutomationControlled"
            ]

            if os.environ.get("BROWSER_NO_SANDBOX") == "1":
                launch_args.append("--no-sandbox")

            self.browser_context = (
                await self.playwright.chromium.launch_persistent_context(
                    user_data_dir=self.user_data_dir,
                    headless=(
                        os.environ.get("BROWSER_HEADLESS", "1") != "0"
                    ),
                    args=launch_args,
                )
            )

            await Stealth().apply_stealth_async(
                self.browser_context
            )

            self.page = (
                self.browser_context.pages[0]
                if self.browser_context.pages
                else await self.browser_context.new_page()
            )

            await self.page.goto(
                self.CHAT_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )

            await self._wait_for_chat_input()

            self.ready = True
            print("✅ DeepSeek Engine đã sẵn sàng nhận lệnh!")

            await self.ensure_deepthink_enabled()

        except Exception as exc:
            self.last_error = str(exc)
            await self.close()

            if isinstance(exc, BrowserBridgeError):
                raise

            raise BrowserNotReadyError(
                f"Không thể khởi tạo trình duyệt: {exc}"
            ) from exc

    async def check_ready(self, timeout_ms: int = 750) -> bool:
        if self.cooldown_error():
            return False

        if self.lock.locked():
            return self.ready

        if not self.page or self.page.is_closed():
            return False

        try:
            chat_input = self.page.locator(
                self.CHAT_INPUT_SELECTOR
            ).first

            await chat_input.wait_for(
                state="visible",
                timeout=timeout_ms,
            )

            if not await chat_input.is_editable():
                self._mark_input_unavailable(
                    "DeepSeek chat input is not editable."
                )
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

        context = self.browser_context
        playwright = self.playwright

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
                print(
                    "⚠️ Chromium đã đóng trước khi dọn context xong: "
                    f"{exc}"
                )

        if playwright:
            try:
                await asyncio.wait_for(
                    playwright.stop(),
                    timeout=self.SHUTDOWN_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                print(
                    "⚠️ Playwright driver đã đóng trong lúc dọn dẹp: "
                    f"{exc}"
                )

    async def _click_new_chat(self) -> bool:
        if (
            not self.NEW_CHAT_VIA_UI
            or not self.page
            or self.page.is_closed()
        ):
            return False

        candidates = [
            self.page.get_by_role(
                "button",
                name=re.compile(
                    r"new chat|new conversation|new",
                    re.I,
                ),
            ),
            self.page.locator(
                "button[aria-label*='New' i]"
            ),
            self.page.locator(
                "[data-testid*='new-chat' i]"
            ),
        ]

        for locator in candidates:
            try:
                count = await locator.count()

                for index in range(min(count, 3)):
                    candidate = locator.nth(index)

                    if (
                        await candidate.is_visible()
                        and await candidate.is_enabled()
                    ):
                        await candidate.click(timeout=1500)

                        await self.page.wait_for_timeout(150)

                        await self._wait_for_chat_input(
                            timeout_ms=5000
                        )

                        return True

            except Exception:
                continue

        return False

    async def start_new_conversation(self):
        self._raise_during_cooldown()

        if not self.page or self.page.is_closed():
            raise BrowserNotReadyError(
                self.last_error
                or "Trình duyệt chưa sẵn sàng."
            )

        try:
            if await self._click_new_chat():
                self.ready = True
                self.last_error = None
                return

            await self.page.goto(
                self.CHAT_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )

            await self._wait_for_chat_input()

            self.ready = True
            self.last_error = None

        except Exception as exc:
            self.ready = False
            self.last_error = str(exc)

            if isinstance(exc, BrowserBridgeError):
                raise

            raise BrowserNotReadyError(
                f"Không thể mở cuộc trò chuyện mới: {exc}"
            ) from exc

    @asynccontextmanager
    async def conversation(self, reset: bool = True):
        async with self.lock:
            if reset:
                await self.start_new_conversation()
            yield self

    async def send_message_to_deepseek(self, message: str) -> str:
        if not self.lock.locked():
            raise RuntimeError(
                "send_message_to_deepseek phải chạy bên trong conversation()."
            )

        if not isinstance(message, str) or not message.strip():
            raise ValueError(
                "Nội dung gửi đến DeepSeek không được rỗng."
            )

        self._raise_during_cooldown()

        self._request_count += 1

        if (
            self._request_count % self.DEEPTHINK_RECHECK_EVERY == 0
        ):
            await self.ensure_deepthink_enabled()

        input_error = None

        for attempt in range(2):
            chat_input = await self._wait_for_chat_input()

            old_responses = await self.page.query_selector_all(
                self.RESPONSE_SELECTOR
            )

            old_count = len(old_responses)

            baseline_text = (
                await old_responses[-1].inner_text()
                if old_responses
                else None
            )

            try:
                await self._fill_chat_input(
                    chat_input,
                    message,
                )

                await chat_input.press(
                    "Enter",
                    timeout=self.INPUT_ACTION_TIMEOUT_MS,
                )

            except (
                BrowserInputUnavailableError,
                PlaywrightTimeoutError,
                PlaywrightError,
            ) as exc:
                input_error = exc

                if attempt == 0:
                    print(
                        "🔄 Ô chat thay đổi trạng thái; "
                        "tạo lại conversation và thử một lần..."
                    )

                    try:
                        await self.start_new_conversation()
                    except BrowserBridgeError as recovery_exc:
                        input_error = recovery_exc
                        break

                    continue

                break

            input_error = None
            break

        if input_error is not None:
            error_message = (
                "DeepSeek không cho phép nhập hoặc gửi tin nhắn; "
                "hãy kiểm tra phiên đăng nhập và giao diện web."
            )

            self._mark_input_unavailable(error_message)

            raise BrowserInputUnavailableError(
                error_message
            ) from input_error

        print("⏳ Đang đợi DeepSeek bắt đầu trả lời...")

        loop = asyncio.get_running_loop()
        start_wait = loop.time()
        response_handle = None

        while (
            loop.time() - start_wait
            <= self.RESPONSE_START_TIMEOUT
        ):
            responses = await self.page.query_selector_all(
                self.RESPONSE_SELECTOR
            )

            if responses:
                candidate = responses[-1]
                current_text = await candidate.inner_text()

                is_new = (
                    len(responses) > old_count
                    or (
                        baseline_text is not None
                        and current_text != baseline_text
                    )
                    or (
                        baseline_text is None
                        and current_text.strip()
                    )
                )

                if is_new:
                    response_handle = candidate
                    break

            await asyncio.sleep(
                self.POLL_INTERVAL_SECONDS
            )

        if response_handle is None:
            raise BrowserResponseTimeout(
                "DeepSeek không tạo phản hồi mới sau "
                f"{self.RESPONSE_START_TIMEOUT:.0f}s."
            )

        print("⏳ Đang đợi DeepSeek trả lời xong...")

        last_text = await response_handle.inner_text()
        last_change = loop.time()
        completion_start = loop.time()

        while True:
            if (
                loop.time() - completion_start
                > self.RESPONSE_COMPLETE_TIMEOUT
            ):
                raise BrowserResponseTimeout(
                    "DeepSeek chưa hoàn thành sau "
                    f"{self.RESPONSE_COMPLETE_TIMEOUT:.0f}s; "
                    "không trả nội dung một phần để tránh tool call bị cắt."
                )

            await asyncio.sleep(
                min(
                    0.2,
                    max(self.POLL_INTERVAL_SECONDS, 0.05),
                )
            )

            try:
                current_text = await response_handle.inner_text()

            except Exception:
                responses = await self.page.query_selector_all(
                    self.RESPONSE_SELECTOR
                )

                if not responses:
                    raise BrowserResponseTimeout(
                        "Bubble phản hồi biến mất trước khi hoàn thành."
                    )

                response_handle = responses[-1]
                current_text = await response_handle.inner_text()

            if current_text != last_text:
                last_text = current_text
                last_change = loop.time()
                continue

            if (
                current_text.strip()
                and loop.time() - last_change
                >= self.STABLE_SECONDS
            ):
                print("✅ DeepSeek đã trả lời xong!")
                return current_text