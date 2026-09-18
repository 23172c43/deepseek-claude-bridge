import argparse
import subprocess
import time
import socket
import sys
import os
import json
import urllib.request
import urllib.error


def is_port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0


def is_our_server(port, timeout=2.0):
    """Xác nhận port đang mở là ĐÚNG bridge này, không phải process khác chiếm port."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("bridge") == "deepseek-claude-agent"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError):
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Khởi động DeepSeek-Claude Bridge. Mỗi cửa sổ Claude Code cần "
                     "--port và --profile RIÊNG, nếu không sẽ trộn lẫn hội thoại "
                     "DeepSeek giữa các project và Chromium sẽ crash vì profile bị khóa."
    )
    parser.add_argument("--port", type=int, default=8000, help="Port cho server (mặc định 8000)")
    parser.add_argument(
        "--profile", default="./deepseek_user_data",
        help="Thư mục profile Chromium (mặc định ./deepseek_user_data). "
             "Phiên thứ 2 trở đi PHẢI dùng thư mục khác, ví dụ ./deepseek_user_data_2."
    )
    args = parser.parse_args()
    port = args.port
    profile_dir = args.profile

    if not os.path.isdir(profile_dir):
        print(
            f"❌ Không tìm thấy profile '{profile_dir}'. Nếu đây là phiên thứ 2 trở đi, "
            f"tạo profile bằng cách copy phiên đã login (cookie dùng chung được):\n"
            f"    cp -r deepseek_user_data {profile_dir}\n"
            f"rồi chạy lại lệnh này."
        )
        sys.exit(1)

    print(f"🔄 Đang khởi động Server DeepSeek-Claude Bridge (port={port}, profile={profile_dir})...")

    log_file = open(f"server_{port}.log", "w")

    env = os.environ.copy()
    env["DEEPSEEK_PROFILE_DIR"] = profile_dir

    server_process = subprocess.Popen(
        ["uvicorn", "app.server:app", "--port", str(port)],
        stdout=log_file,
        stderr=log_file,
        env=env,
    )

    print("⏳ Đang chờ server sẵn sàng...")
    start_time = time.time()
    timeout = 30

    while not is_port_open(port):
        if time.time() - start_time > timeout:
            print(f"❌ Lỗi: Server không thể khởi động sau 30s. Xem file server_{port}.log.")
            server_process.kill()
            sys.exit(1)
        time.sleep(0.5)

    print("⏳ Xác nhận đúng server (kiểm tra /health)...")
    while not is_our_server(port):
        if time.time() - start_time > timeout:
            print(
                f"❌ Lỗi: Port {port} đang bị tiến trình KHÁC chiếm, hoặc /health chưa "
                f"phản hồi đúng. Nếu bạn đang chạy song song nhiều cửa sổ, mỗi cửa sổ "
                f"phải dùng --port khác nhau. Xem server_{port}.log."
            )
            server_process.kill()
            sys.exit(1)
        time.sleep(0.5)

    print(f"✅ Server đã sẵn sàng ở port {port} (đã xác nhận đúng bridge, profile={profile_dir})!")
    print("🚀 Bàn giao quyền điều khiển cho Claude CLI...")
    print("-" * 50)

    os.environ["ANTHROPIC_BASE_URL"] = f"http://localhost:{port}"
    if "ANTHROPIC_API_KEY" not in os.environ:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-dummy-12345"

    try:
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