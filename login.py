import asyncio
from playwright.async_api import async_playwright
# Dùng class Stealth mới thay cho stealth_async cũ
from playwright_stealth import Stealth 

async def run_login():
    print("🚀 Khởi tạo trình duyệt để đăng nhập...")
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir="./deepseek_user_data",
            headless=False, # Mở giao diện (UI) để tự tay thao tác
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox"
            ]
        )
        
        # Áp dụng Stealth vào context bằng cú pháp mới của bản 2.x
        await Stealth().apply_stealth_async(context)
        
        page = context.pages[0] if context.pages else await context.new_page()
        
        print("🌐 Đang mở trang web DeepSeek...")
        await page.goto("https://chat.deepseek.com")
        
        print("\n" + "="*70)
        print("🛑 DỪNG LẠI VÀ CHÚ Ý 🛑")
        print("1. Hãy thao tác trên cửa sổ trình duyệt vừa mở để đăng nhập vào tài khoản DeepSeek.")
        print("2. Giải mã Captcha (nếu có) và chờ đến khi nhìn thấy giao diện khung chat xuất hiện.")
        print("3. TUYỆT ĐỐI KHÔNG TỰ ĐÓNG TRÌNH DUYỆT BẰNG DẤU X (sẽ làm mất dữ liệu lưu).")
        print("4. Khi đã vào được khung chat thành công, hãy quay lại terminal này và nhấn phím ENTER.")
        print("="*70 + "\n")
        
        input("👉 Nhấn ENTER ở đây sau khi bạn đã đăng nhập thành công và thấy khung chat: ")
        
        print("💾 Đang lưu dữ liệu phiên làm việc...")
        await context.close()
        
        print("✅ Đã lưu phiên đăng nhập thành công vào thư mục ./deepseek_user_data!")

if __name__ == "__main__":
    asyncio.run(run_login())