import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def is_port_open(port, timeout=0.3):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def get_server_health(port, timeout=2.0):
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data if data.get("bridge") == "deepseek-claude-agent" else None
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        ConnectionError,
        OSError,
    ):
        return None


def is_our_server(port, timeout=2.0):
    return get_server_health(port, timeout) is not None


def stop_process(process, timeout=8):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Khởi động DeepSeek-Claude Bridge. Mỗi launcher dùng một port/profile riêng; "
            "mỗi API request bên trong launcher được cô lập bằng một chat mới."
        )
    )
    parser.add_argument("--port", type=int, default=8000, help="Port server (mặc định 8000)")
    parser.add_argument(
        "--profile",
        default="./deepseek_user_data",
        help="Thư mục profile Chromium đã đăng nhập.",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=60,
        help="Số giây tối đa chờ browser và server sẵn sàng.",
    )
    args = parser.parse_args()

    if not 1 <= args.port <= 65535:
        parser.error("--port phải nằm trong khoảng 1..65535")
    if args.startup_timeout <= 0:
        parser.error("--startup-timeout phải lớn hơn 0")

    port = args.port
    profile_dir = Path(args.profile).expanduser().resolve()
    if not profile_dir.is_dir():
        print(
            f"❌ Không tìm thấy profile '{profile_dir}'. "
            f"Hãy tạo bằng: python login.py --profile {profile_dir}"
        )
        return 1

    claude_executable = shutil.which("claude")
    if not claude_executable:
        print("❌ Không tìm thấy Claude Code CLI trong PATH.")
        return 1

    if is_port_open(port):
        if is_our_server(port):
            print(
                f"❌ Port {port} đang chạy một bridge khác. "
                "Hãy tắt bridge đó hoặc chọn --port khác."
            )
        else:
            print(f"❌ Port {port} đang bị tiến trình khác chiếm.")
        return 1

    log_path = Path(f"server_{port}.log").resolve()
    print(f"🔄 Khởi động bridge (port={port}, profile={profile_dir})...")
    print(f"📝 Log: {log_path}")

    server_process = None
    with log_path.open("a", encoding="utf-8", buffering=1) as log_file:
        env = os.environ.copy()
        env["DEEPSEEK_PROFILE_DIR"] = str(profile_dir)
        server_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.server:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            stdout=log_file,
            stderr=log_file,
            env=env,
        )

        print("⏳ Đang chờ server và phiên đăng nhập sẵn sàng...")
        deadline = time.monotonic() + args.startup_timeout
        health = None
        while time.monotonic() < deadline:
            return_code = server_process.poll()
            if return_code is not None:
                print(
                    f"❌ Server dừng sớm với mã {return_code}. "
                    f"Xem log tại {log_path}."
                )
                return return_code or 1
            health = get_server_health(port)
            if health and health.get("browser_ready"):
                break
            time.sleep(0.5)
        else:
            detail = (health or {}).get("last_error")
            if detail:
                print(f"❌ Browser chưa sẵn sàng: {detail}")
            else:
                print(f"❌ Server không sẵn sàng sau {args.startup_timeout:.0f}s.")
            print(f"Xem log tại {log_path}.")
            stop_process(server_process)
            return 1

        print(f"✅ Bridge sẵn sàng tại http://127.0.0.1:{port}")
        print("🚀 Bàn giao quyền điều khiển cho Claude CLI...")

        client_env = os.environ.copy()
        client_env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
        if os.environ.get("BRIDGE_API_KEY"):
            client_env["ANTHROPIC_API_KEY"] = os.environ["BRIDGE_API_KEY"]
        else:
            client_env.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-local")

        try:
            completed = subprocess.run([claude_executable], env=client_env, check=False)
            return completed.returncode
        except KeyboardInterrupt:
            return 130
        finally:
            print("\n🛑 Đang dọn dẹp và tắt server...")
            stop_process(server_process)
            print("✅ Đã đóng ứng dụng an toàn.")


if __name__ == "__main__":
    raise SystemExit(main())
