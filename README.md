# DeepSeek ↔ Claude Code Bridge

Proxy cục bộ cung cấp một phần giao diện Anthropic Messages API cho Claude Code,
nhưng xử lý yêu cầu bằng DeepSeek Web thông qua Playwright.

> Đây là dự án không chính thức, không liên kết với Anthropic hoặc DeepSeek.
> Tự động hóa giao diện web có thể không phù hợp với điều khoản của dịch vụ và có
> thể ngừng hoạt động khi giao diện DeepSeek thay đổi.

## Đặc điểm

- POST /v1/messages, hỗ trợ non-stream và SSE.
- POST /v1/messages/count_tokens với số token ước lượng.
- Chuyển tool schema và tool result giữa định dạng Anthropic và prompt văn bản.
- Xác thực tool input bằng JSON Schema trước khi trả tool_use.
- Mỗi request dựng lại đầy đủ system prompt và lịch sử, rồi mở một chat DeepSeek mới.
- Các request được xếp hàng để không trộn nội dung trong cùng browser profile.
- Kiểm tra phiên đăng nhập thật qua /health.
- SSE mở ngay và gửi heartbeat trong lúc chờ DeepSeek.

SSE hiện là **buffered streaming**: kết nối được mở ngay, nhưng content delta chỉ
được phát sau khi câu trả lời hoàn tất và tool call đã được xác thực. Đây không
phải token streaming trực tiếp từ model.

## Yêu cầu

- Python 3.9 trở lên.
- Claude Code CLI trong PATH.
- Tài khoản DeepSeek.

Google Chrome không bắt buộc. Mặc định cả login và server dùng Chromium do
Playwright quản lý.

## Cài đặt

~~~bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
~~~

Để phát triển và chạy test:

~~~bash
pip install -r requirements-dev.txt
pytest
ruff check .
~~~

## Đăng nhập

~~~bash
python login.py
~~~

Sau khi đăng nhập và thấy khung chat, quay lại terminal rồi nhấn Enter. Profile
mặc định được lưu ở ./deepseek_user_data với quyền thư mục hạn chế.

Có thể chọn profile hoặc dùng Chrome đã cài:

~~~bash
python login.py --profile ./deepseek_user_data_2
python login.py --channel chrome
~~~

Không sao chép hoặc chia sẻ thư mục profile: nó chứa cookie đăng nhập.

## Chạy

~~~bash
python launcher.py
~~~

Launcher:

1. Kiểm tra port chưa bị process khác chiếm.
2. Khởi động Uvicorn chỉ trên 127.0.0.1.
3. Chờ browser thực sự sẵn sàng.
4. Chạy Claude Code với ANTHROPIC_BASE_URL trỏ về bridge.
5. Dừng và chờ server thoát khi Claude kết thúc.

Chạy nhiều launcher bằng port và profile riêng:

~~~bash
python launcher.py --port 8001 --profile ./deepseek_user_data_2
~~~

Log được nối thêm vào server_<port>.log, không ghi đè log cũ.

## Cấu hình

Các biến môi trường hỗ trợ:

| Biến | Mặc định | Ý nghĩa |
|---|---:|---|
| BRIDGE_API_KEY | rỗng | Nếu đặt, bắt buộc x-api-key hoặc Bearer token |
| BROWSER_HEADLESS | 1 | Đặt 0 để hiện browser |
| BROWSER_NO_SANDBOX | rỗng | Chỉ đặt 1 trong container tin cậy |
| DEEPSEEK_CHAT_URL | https://chat.deepseek.com | URL giao diện chat |
| RESPONSE_START_TIMEOUT | 30 | Giây chờ bubble phản hồi mới |
| RESPONSE_COMPLETE_TIMEOUT | 120 | Giây chờ phản hồi hoàn tất |
| RESPONSE_STABLE_SECONDS | 5 | Thời gian text phải ổn định |
| MAX_REQUEST_BYTES | 2097152 | Kích thước JSON request tối đa |

Ví dụ bật khóa cục bộ:

~~~bash
export BRIDGE_API_KEY='replace-with-a-random-local-secret'
python launcher.py
~~~

Launcher tự chuyển khóa này thành ANTHROPIC_API_KEY cho Claude Code.

## Phạm vi tương thích

Bridge phục vụ luồng Claude Code thông dụng, không phải bản triển khai đầy đủ của
Anthropic API. model được phản chiếu trong response nhưng không chọn model trên
DeepSeek Web. temperature và các tham số sampling khác không được giao diện web
đảm bảo. max_tokens, stop_sequences và tool_choice chỉ được mô phỏng ở tầng
adapter.

Token được ước lượng theo số byte UTF-8; không nên dùng cho thanh toán hoặc giới
hạn chính xác.

## Cấu trúc

~~~text
.
├── app/
│   ├── browser.py       # Vòng đời Playwright và đọc phản hồi DeepSeek
│   └── server.py        # Adapter HTTP, prompt, parser và SSE
├── tests/               # Test hồi quy
├── launcher.py          # Khởi động server và Claude Code
├── login.py             # Tạo profile đăng nhập
├── requirements.txt
└── requirements-dev.txt
~~~

## Giới hạn và an toàn

- Selector DOM của DeepSeek vẫn có thể thay đổi.
- Một browser xử lý tuần tự; request đồng thời sẽ phải chờ.
- Không expose port ra mạng công cộng. Launcher cố định bind ở loopback.
- Tool call là output không đáng tin cậy và chỉ được chuyển tiếp sau khi đúng tên
  và đúng JSON Schema; vẫn nên giữ cơ chế xác nhận tool nguy hiểm của Claude Code.
- Nếu cần độ ổn định production, hãy thay lớp Playwright bằng API model chính thức.

## Giấy phép

MIT, xem [LICENSE](LICENSE).
