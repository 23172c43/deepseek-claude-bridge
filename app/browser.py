import asyncio
from playwright.async_api import async_playwright, TimeoutError
from playwright_stealth import Stealth

class BrowserBridge:
    def __init__(self):
        self.playwright = None
        self.browser_context = None
        self.page = None
        self.lock = asyncio.Lock()

    async def initialize(self):
        print("Khởi động Playwright ẩn...")
        self.playwright = await async_playwright().start()
        self.browser_context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir="./deepseek_user_data",
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        await Stealth().apply_stealth_async(self.browser_context)
        self.page = self.browser_context.pages[0] if self.browser_context.pages else await self.browser_context.new_page()
        
        await self.page.goto("https://chat.deepseek.com")
        try:
            await self.page.wait_for_selector("textarea[placeholder='Message DeepSeek']", timeout=15000)
            print("✅ DeepSeek Engine đã sẵn sàng nhận lệnh!")
        except TimeoutError:
            print("❌ LỖI: Không tìm thấy khung chat. Có thể phiên đăng nhập đã hết hạn. Hãy chạy lại file login.py!")

    async def close(self):
        if self.browser_context:
            await self.browser_context.close()
        if self.playwright:
            await self.playwright.stop()

    async def send_message_to_deepseek(self, message: str) -> str:
        async with self.lock:
            chat_input = await self.page.wait_for_selector("textarea[placeholder='Message DeepSeek']")

            # 1. Chụp lại text (và số lượng bubble) TRƯỚC khi gửi, để biết đâu là "cũ"
            old_responses = await self.page.query_selector_all(".ds-assistant-message-main-content")
            old_count = len(old_responses)
            baseline_text = await old_responses[-1].inner_text() if old_responses else None

            await chat_input.fill(message)
            await chat_input.press("Enter")

            print("⏳ Đang đợi DeepSeek bắt đầu trả lời...")

            # 2. Đợi cho tới khi có bubble MỚI xuất hiện, hoặc bubble cuối đổi nội dung so với baseline
            start_wait = asyncio.get_event_loop().time()
            while True:
                responses = await self.page.query_selector_all(".ds-assistant-message-main-content")
                if responses:
                    new_count = len(responses)
                    current_text = await responses[-1].inner_text()
                    if new_count > old_count or (baseline_text is not None and current_text != baseline_text):
                        break
                    if baseline_text is None and len(current_text) > 0:
                        break
                if asyncio.get_event_loop().time() - start_wait > 20:
                    print("⚠️ Quá 20s vẫn chưa thấy DeepSeek bắt đầu trả lời — có thể Enter chưa kích hoạt generate.")
                    break
                await asyncio.sleep(0.3)

            print("⏳ Đang đợi DeepSeek gõ xong câu trả lời...")

            # 3. Từ đây mới bắt đầu logic chờ ổn định như cũ, nhưng trên bubble MỚI
            last_text = ""
            stable_count = 0
            while stable_count < 6:
                await asyncio.sleep(0.5)
                responses = await self.page.query_selector_all(".ds-assistant-message-main-content")
                if responses:
                    current_text = await responses[-1].inner_text()
                    if current_text == last_text and len(current_text) > 0:
                        stable_count += 1
                    else:
                        last_text = current_text
                        stable_count = 0

            print("✅ DeepSeek đã trả lời xong!")
            return last_text