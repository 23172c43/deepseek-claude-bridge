import subprocess
import time
import socket
import sys
import os

def is_port_open(port):
    """Kiểm tra xem port đã mở và sẵn sàng nhận kết nối chưa"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0

def main():
    print("🔄 Đang khởi động Server DeepSeek-Claude Bridge...")
    
    # Ghi log ra file để không bị kẹt bộ nhớ đệm (stdout=PIPE issue)
    log_file = open("server.log", "w")
    server_process = subprocess.Popen(
        ["uvicorn", "app.server:app", "--port", "8000"],
        stdout=log_file,
        stderr=log_file
    )

    print("⏳ Đang chờ server sẵn sàng...")
    start_time = time.time()
    timeout = 30
    
    while not is_port_open(8000):
        if time.time() - start_time > timeout:
            print("❌ Lỗi: Server không thể khởi động sau 30s. Xem file server.log để biết chi tiết.")
            server_process.kill()
            sys.exit(1)
        time.sleep(0.5)

    print("✅ Server đã sẵn sàng ở port 8000!")
    print("🚀 Bàn giao quyền điều khiển cho Claude CLI...")
    print("-" * 50)

    # Set biến môi trường bắt buộc cho Claude Code CLI
    os.environ["ANTHROPIC_BASE_URL"] = "http://localhost:8000"
    if "ANTHROPIC_API_KEY" not in os.environ:
         os.environ["ANTHROPIC_API_KEY"] = "sk-ant-dummy-12345"

    try:
        # Chạy Claude CLI 
        subprocess.run(["claude"])
    except KeyboardInterrupt:
        pass
    finally:
        print("\n🛑 Đang dọn dẹp và tắt server...")
        server_process.terminate()
        log_file.close()
        print("✅ Đã đóng ứng dụng an toàn.")

if __name__ == "__main__":
    main()