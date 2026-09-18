import argparse
import asyncio
import os
from contextlib import suppress
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright
from playwright_stealth import Stealth


async def run_login(profile: str, channel: Optional[str] = None, no_sandbox: bool = False):
    profile_dir = Path(profile).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with suppress(OSError):
        profile_dir.chmod(0o700)

    print(f"🚀 Khởi tạo trình duyệt để đăng nhập (profile={profile_dir})...")
    async with async_playwright() as playwright:
        launch_args = ["--disable-blink-features=AutomationControlled"]
        if no_sandbox:
            launch_args.append("--no-sandbox")
        launch_options = {
            "user_data_dir": str(profile_dir),
            "headless": False,
            "args": launch_args,
        }
        if channel:
            launch_options["channel"] = channel

        context = await playwright.chromium.launch_persistent_context(**launch_options)
        try:
            await Stealth().apply_stealth_async(context)
            page = context.pages[0] if context.pages else await context.new_page()

            print("🌐 Đang mở trang web DeepSeek...")
            await page.goto(
                os.environ.get("DEEPSEEK_CHAT_URL", "https://chat.deepseek.com"),
                wait_until="domcontentloaded",
                timeout=30000,
            )
            print(
                "\n"
                "1. Đăng nhập và giải Captcha nếu có.\n"
                "2. Chờ đến khi nhìn thấy khung chat.\n"
                "3. Quay lại terminal và nhấn ENTER; không tự đóng cửa sổ browser.\n"
            )
            await asyncio.to_thread(
                input,
                "👉 Nhấn ENTER sau khi đã đăng nhập thành công: ",
            )
            await page.wait_for_selector(
                "textarea[placeholder='Message DeepSeek']",
                state="visible",
                timeout=10000,
            )
            print("💾 Đã xác nhận khung chat, đang lưu phiên...")
        finally:
            with suppress(Exception):
                await context.close()

    print(f"✅ Đã lưu phiên đăng nhập tại {profile_dir}")


def main():
    parser = argparse.ArgumentParser(description="Đăng nhập DeepSeek và lưu profile cục bộ.")
    parser.add_argument("--profile", default="./deepseek_user_data")
    parser.add_argument(
        "--channel",
        default=None,
        help="Browser channel tùy chọn, ví dụ 'chrome'; mặc định dùng Chromium của Playwright.",
    )
    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help="Chỉ dùng trong container tin cậy khi Chromium không thể chạy sandbox.",
    )
    args = parser.parse_args()
    asyncio.run(run_login(args.profile, args.channel, args.no_sandbox))


if __name__ == "__main__":
    main()
