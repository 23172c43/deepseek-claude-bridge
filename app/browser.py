import asyncio
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth


class BrowserBridge:
    # Kiểm tra lại DeepThink mỗi N request thay vì mỗi lần (tốn round-trip)
    # hoặc chỉ 1 lần lúc init (DeepSeek có thể tự reset toggle giữa phiên).
    DEEPTHINK_RECHECK_EVERY = 15

    # Vòng lặp chờ "text ổn định" không được phép chạy vô hạn — nếu DeepSeek
    # treo giữa chừng, request cũ giữ nguyên self.lock mãi mãi -> mọi request
    # sau bị chặn theo. Đây là bug nghiêm trọng nhất trong bản cũ.
    MAX_STABLE_WAIT_SECONDS = 90

    def __init__(self, user_data_dir: str = "./deepseek_user_data"):
        self.playwright = None
        self.browser_context = None
        self.page = None
        self.lock = asyncio.Lock()
        self._request_count = 0
        # Mỗi phiên bridge PHẢI dùng profile riêng: Chromium khóa user_data_dir
        # bằng singleton lock, 2 process trỏ cùng thư mục này sẽ crash ở initialize().
        # Quan trọng hơn: mỗi profile = 1 tab = 1 cuộc chat DeepSeek riêng, nên 2 cửa
        # sổ Claude Code sẽ KHÔNG bị trộn lẫn ngữ cảnh (path/file của project khác nhau).
        self.user_data_dir = user_data_dir

    async def ensure_deepthink_enabled(self):
        """
        Kiểm tra trạng thái DeepThink. Nếu đang đóng (aria-pressed="false") thì bật lên.
        Nếu đang mở rồi thì bỏ qua.
        """
        try:
            print("🔍 Đang kiểm tra trạng thái DeepThink...")
            deepthink_btn = self.page.locator(
                "div.ds-toggle-button",
                has=self.page.locator("span._6dbc175", has_text="DeepThink"),
            )
            await deepthink_btn.wait_for(state="visible", timeout=5000)
            is_pressed = await deepthink_btn.get_attribute("aria-pressed")

            if is_pressed == "false":
                print("⚙️ DeepThink đang tắt, tiến hành bật...")
                await deepthink_btn.click()
                await self.page.wait_for_timeout(500)
                print("✅ Đã bật DeepThink thành công!")
            else:
                print("🚀 DeepThink đã bật sẵn, bỏ qua.")
        except Exception as e:
            # Selector phụ thuộc DOM DeepSeek (span._6dbc175 là hash class,
            # sẽ đổi khi họ build lại UI). Không để lỗi ở đây làm treo cả request.
            print(f"⚠️ Không thể xử lý DeepThink: {e}")

    async def initialize(self):
        print("Khởi động Playwright ẩn...")
        self.playwright = await async_playwright().start()
        self.browser_context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        await Stealth().apply_stealth_async(self.browser_context)
        self.page = (
            self.browser_context.pages[0]
            if self.browser_context.pages
            else await self.browser_context.new_page()
        )

        await self.page.goto("https://chat.deepseek.com")
        try:
            await self.page.wait_for_selector(
                "textarea[placeholder='Message DeepSeek']", timeout=15000
            )
            print("✅ DeepSeek Engine đã sẵn sàng nhận lệnh!")
            await self.ensure_deepthink_enabled()
        except PlaywrightTimeoutError:
            print(
                "❌ LỖI: Không tìm thấy khung chat. Có thể phiên đăng nhập đã hết hạn. "
                "Hãy chạy lại file login.py!"
            )

    async def close(self):
        if self.browser_context:
            await self.browser_context.close()
        if self.playwright:
            await self.playwright.stop()

    async def send_message_to_deepseek(self, message: str) -> str:
        async with self.lock:
            self._request_count += 1
            if self._request_count % self.DEEPTHINK_RECHECK_EVERY == 0:
                await self.ensure_deepthink_enabled()

            chat_input = await self.page.wait_for_selector(
                "textarea[placeholder='Message DeepSeek']"
            )

            # 1. Chụp lại text (và số lượng bubble) TRƯỚC khi gửi, để biết đâu là "cũ"
            old_responses = await self.page.query_selector_all(".ds-assistant-message-main-content")
            old_count = len(old_responses)
            baseline_text = await old_responses[-1].inner_text() if old_responses else None

            await chat_input.fill(message)
            await chat_input.press("Enter")

            print("⏳ Đang đợi DeepSeek bắt đầu trả lời...")

            # 2. Đợi cho tới khi có bubble MỚI xuất hiện, hoặc bubble cuối đổi nội dung so với baseline
            loop = asyncio.get_running_loop()
            start_wait = loop.time()
            while True:
                responses = await self.page.query_selector_all(".ds-assistant-message-main-content")
                if responses:
                    new_count = len(responses)
                    current_text = await responses[-1].inner_text()
                    if new_count > old_count or (baseline_text is not None and current_text != baseline_text):
                        break
                    if baseline_text is None and len(current_text) > 0:
                        break
                if loop.time() - start_wait > 20:
                    print("⚠️ Quá 20s vẫn chưa thấy DeepSeek bắt đầu trả lời — có thể Enter chưa kích hoạt generate.")
                    break
                await asyncio.sleep(0.3)

            print("⏳ Đang đợi DeepSeek gõ xong câu trả lời...")

            # 3. Chờ ổn định, CÓ WATCHDOG: nếu DeepSeek treo giữa chừng (mất mạng,
            # rate-limit, UI đứng), không được giữ lock vô thời hạn -> trả về những
            # gì đã có thay vì làm nghẽn mọi request phía sau.
            last_text = ""
            stable_count = 0
            stable_start = loop.time()
            timed_out = False
            while stable_count < 6:
                if loop.time() - stable_start > self.MAX_STABLE_WAIT_SECONDS:
                    print(
                        f"⚠️ Vượt quá {self.MAX_STABLE_WAIT_SECONDS}s chờ ổn định — "
                        f"trả về nội dung hiện có, bỏ qua phần còn lại."
                    )
                    timed_out = True
                    break
                await asyncio.sleep(0.5)
                responses = await self.page.query_selector_all(".ds-assistant-message-main-content")
                if responses:
                    current_text = await responses[-1].inner_text()
                    if current_text == last_text and len(current_text) > 0:
                        stable_count += 1
                    else:
                        last_text = current_text
                        stable_count = 0

            if not timed_out:
                print("✅ DeepSeek đã trả lời xong!")
            return last_text