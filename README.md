 # 🌉 DeepSeek ↔ Claude Code Bridge

Chạy Claude Code CLI miễn phí bằng cách "mượn" não của DeepSeek Web Chat.

Dự án này dựng một proxy tương thích Anthropic Messages API (/v1/messages), nhưng thay vì gọi Anthropic, nó điều khiển một trình duyệt Playwright đăng nhập sẵn vào chat.deepseek.com, gửi prompt vào khung chat, chờ DeepSeek trả lời, rồi dịch ngược câu trả lời thành định dạng Anthropic (text + tool_use). Nhờ vậy Claude Code CLI tưởng đang nói chuyện với Anthropic, nhưng thực chất là DeepSeek đang làm việc.
✨ Tính năng

🔌 Tương thích Anthropic API — Hỗ trợ /v1/messages (cả stream và non-stream) và /v1/messages/count_tokens.

🧠 Tool Calling — Dịch schema tool của Claude Code sang định dạng văn bản mà DeepSeek hiểu được, rồi parse ngược lại thành tool_use chuẩn Anthropic.

🛡️ Chống lỗi escape JSON — Tham số tool được truyền dưới dạng block thô thay vì nhồi vào JSON, tránh JSONDecodeError khi nội dung file chứa dấu ngoặc kép hoặc ký tự xuống dòng.

🔁 Tự động sửa lỗi định dạng — Nếu DeepSeek trả về tool call sai cú pháp, proxy tự động yêu cầu gửi lại một lần.

🕵️ Stealth Mode — Dùng playwright_stealth để giảm khả năng bị phát hiện là bot.

💾 Lưu phiên đăng nhập — Dùng persistent context, chỉ cần đăng nhập một lần.

📡 Streaming SSE — Trả lời theo chuẩn Server-Sent Events mà Claude Code CLI yêu cầu.

📁 Cấu trúc dự án
text
Copy
Download
.
├── launcher.py              # Entry point: khởi động server + chạy Claude CLI
├── login.py                 # Script đăng nhập DeepSeek thủ công (chạy 1 lần)
├── requirements.txt         # Danh sách thư viện Python
├── app/
│   ├── __init__.py
│   ├── browser.py           # BrowserBridge: điều khiển Playwright gửi/nhận tin nhắn
│   └── server.py            # FastAPI: adapter Anthropic API ↔ DeepSeek
└── deepseek_user_data/      # Dữ liệu phiên đăng nhập (tự sinh, đã gitignore)
⚙️ Yêu cầu hệ thống

Python ≥ 3.9

Google Chrome đã cài đặt (Playwright dùng channel="chrome")

Claude Code CLI đã cài (npm install -g @anthropic-ai/claude-code)

Tài khoản DeepSeek (miễn phí)

🚀 Cài đặt
1. Cài thư viện Python
bash
Copy
Download
pip install -r requirements.txt
playwright install chromium
2. Đăng nhập DeepSeek (chỉ làm một lần)
bash
Copy
Download
python login.py

Script sẽ mở một cửa sổ Chrome. Bạn hãy:

Đăng nhập vào tài khoản DeepSeek.

Giải Captcha nếu có, chờ đến khi thấy khung chat.

KHÔNG đóng trình duyệt bằng dấu X (sẽ mất dữ liệu phiên).

Quay lại terminal và nhấn ENTER.

Phiên đăng nhập sẽ được lưu vào ./deepseek_user_data/.

3. Chạy
bash
Copy
Download
python launcher.py

launcher.py sẽ:

Khởi động FastAPI server ở http://localhost:8000 (chạy nền, log ghi ra server.log).

Chờ server sẵn sàng (tối đa 30 giây).

Thiết lập biến môi trường ANTHROPIC_BASE_URL=http://localhost:8000.

Chạy claude CLI và chuyển toàn bộ traffic qua proxy.

🧩 Cách hoạt động
text
Copy
Download
Claude Code CLI
      │  (Anthropic Messages API)
      ▼
┌─────────────────────┐
│  FastAPI (server.py)│
│  /v1/messages       │
└─────────┬───────────┘
          │  build_prompt() → văn bản thuần
          ▼
┌─────────────────────┐
│ BrowserBridge       │
│ (browser.py)        │
│  Playwright + Stealth│
└─────────┬───────────┘
          │  gõ vào textarea, nhấn Enter, chờ ổn định
          ▼
   chat.deepseek.com
          │
          ▼  extract_tool_calls()
┌─────────────────────┐
│  Anthropic response │
│  text + tool_use    │
└─────────────────────┘
Vì sao không nhồi tool call vào JSON?

Schema tool đầy đủ của Claude Code rất dài. Nếu bắt DeepSeek trả về JSON chứa nội dung file (có dấu ", \n, \), model rất dễ escape sai và gây JSONDecodeError.

Giải pháp: dùng định dạng block tham số thô — nội dung được đặt nguyên văn, không cần escape. Parser trong server.py sẽ:

Tìm các block tool_call (đếm độ sâu lồng nhau để không bị nhầm với thẻ mẫu bên trong nội dung file).

Đọc tên tool và các tham số.

Ép kiểu tham số theo input_schema (vì block thô trả về toàn string).

Bỏ qua các tool call chỉ chứa giá trị placeholder (..., giá trị, v.v.).

Fallback sang JSON hoặc XML kiểu cũ nếu cần.

📡 API Endpoints
Method	Endpoint	Mô tả
POST	/v1/messages	Endpoint chính, hỗ trợ stream + non-stream
POST	/v1/messages/count_tokens	Ước lượng số token (xấp xỉ len/4)
🐛 Xử lý sự cố
Triệu chứng	Nguyên nhân & cách khắc phục
❌ LỖI: Không tìm thấy khung chat	Phiên đăng nhập hết hạn. Chạy lại python login.py.
❌ Lỗi: Server không thể khởi động sau 30s	Xem server.log. Thường do port 8000 đang bị chiếm.
DeepSeek trả lời mãi không xong	Đã quá 20s chưa thấy bubble mới — có thể Enter chưa kích hoạt generate. Thử gửi lại.
JSONDecodeError khi parse tool call	Đây chính là lý do dự án dùng định dạng block thô. Nếu vẫn gặp, mở issue kèm log.
Bị DeepSeek chặn / captcha liên tục	Dùng tài khoản khác, giảm tần suất, hoặc chạy login.py lại để làm mới phiên.
Chrome không mở được	Kiểm tra đã cài Google Chrome; Playwright dùng channel="chrome".
⚠️ Lưu ý & Giới hạn

Không chính thức: Đây là dự án tự chế, không liên kết với Anthropic hay DeepSeek.

Có thể vi phạm ToS của DeepSeek nếu dùng để tự động hóa quá mức. Tự chịu trách nhiệm.

Không ổn định bằng API thật: Phụ thuộc vào giao diện web DeepSeek — khi DeepSeek đổi DOM (textarea[placeholder='Message DeepSeek'], .ds-assistant-message-main-content), proxy sẽ vỡ.

Hiệu năng chậm: Mỗi request phải chờ DeepSeek gõ xong và ổn định 6 lần × 0.5s.

Không có bảo mật: Server chạy localhost, không có auth. Đừng expose ra mạng ngoài.

Token đếm xấp xỉ (len(text) // 4) — chỉ để Claude Code không báo lỗi, không chính xác.

📄 Giấy phép

MIT — dùng tùy ý, tự chịu rủi ro.
