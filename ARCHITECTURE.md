# Project Architecture Overview

## 1. Tổng quan dự án (Project Overview)
Dự án là một hệ thống tự động hóa toàn diện (automation pipeline) hỗ trợ xử lý video và âm thanh. Quy trình làm việc chính bao gồm 6 bước:
1. **Tải Video/Audio**: Hỗ trợ tải từ Bilibili hoặc dùng file cục bộ.
2. **Tạo phụ đề (Subtitle Generation)**: Trích xuất và chia nhỏ file phụ đề gốc (SRT).
3. **Dịch thuật AI (Translation)**: Sử dụng trình duyệt ẩn Playwright để tự động hóa tương tác với Google Gemini, dịch phụ đề từ tiếng Trung (hoặc ngôn ngữ khác) sang tiếng Việt.
4. **Auto QA & Repair**: Phân tích lỗi timecode, độ dài câu trong phụ đề dịch, và tự động gọi Gemini để sửa lỗi trực tiếp trên các đoạn (parts) bị lỗi.
5. **Text-to-Speech (TTS)**: Sử dụng API thông qua một giao diện Gradio (Google Colab) để tạo giọng nói cho phụ đề. Có cơ chế "điểm dừng thông minh" (Just-in-time) chờ người dùng cập nhật link Gradio nếu kết nối thất bại.
6. **CapCut Injection**: Bơm (inject) trực tiếp file phụ đề và audio đã hoàn thiện vào file draft của ứng dụng CapCut trên PC để tự động hóa việc render video cuối cùng.

## 2. Cấu trúc thư mục (Directory Structure)
- `gui_v2.py`: File giao diện người dùng chính (phiên bản mới nhất), quản lý toàn bộ luồng pipeline và giao tiếp với các module xử lý.
- `gui.py`: File giao diện cũ (v1) - có thể đang được giữ lại để tham khảo hoặc chưa deprecate hoàn toàn.
- `src/`: Thư mục chứa các module core xử lý logic backend độc lập với UI.
  - `bilibili_downloader.py`: Logic tải video từ Bilibili (quản lý cookie, invoke yt-dlp).
  - `gemini_core.py` & `gemini_translate.py`: Xử lý tự động hóa Playwright để thao tác web UI của Gemini.
  - `qa_srt_before.py`, `auto_qa_repair.py`, `batch_replace_srt.py`: Các module phát hiện lỗi SRT (timecode, max length) và gửi prompt lên AI để repair hàng loạt.
  - `srt_utils.py`, `subtitle_generator.py`: Chia cắt, gộp, nội suy timecode cho các file SRT.
  - `backend.py`: Tương tác với CapCut PC, thao tác trên file draft JSON.
- `user_data/`: Thư mục lưu trữ trạng thái người dùng để duy trì qua nhiều phiên (Persistent State).
  - `chrome_profiles/`: Các profile Playwright chứa session đăng nhập (giúp Gemini không bắt đăng nhập lại).
  - `config/`: Cấu hình UI của người dùng (ví dụ: các ô checkbox, path thư mục đã chọn).
  - `prompts/`: Template câu lệnh (prompt) dành cho Gemini khi dịch và QA.
- `downloads/`: Thư mục output lưu trữ các file tạm, file video tải về, file chia cắt srt, và báo cáo QA.

## 3. Kiến trúc State Management & Quy ước Code (Conventions)
### 3.1. Kiến trúc & State Management
- **UI Framework**: Sử dụng **PyQt6**.
- **Đa luồng (Multi-threading)**: Để không làm treo giao diện khi tải file hay chạy Playwright, kiến trúc áp dụng triệt để `QThread` (như `ProcessWorker`, `ChromeLoginWorker`).
- **Giao tiếp giữa các luồng**: Luồng nền (Worker) giao tiếp với Main Thread (UI) thông qua cơ chế **Signals/Slots** đặc trưng của PyQt (ví dụ: `step_signal.emit()`, `log_signal.emit()`, `global_progress_signal.emit()`).
- **Quản lý cấu hình (Persistent Configs)**: Trạng thái của ứng dụng (đường dẫn file, checkbox được tick) được tách biệt khỏi code và lưu trong file ở `user_data/`.

### 3.2. Coding Conventions
- Tên biến, tên hàm, và tên file module sử dụng `snake_case` (ví dụ: `process_srt_speed`, `auto_qa_repair.py`).
- Tên lớp (Classes) sử dụng `PascalCase` (ví dụ: `ProcessWorker`, `MainWindowV2`).
- Pipeline được định nghĩa rõ ràng thành từng "BƯỚC" (Step) trực quan bằng `PipelineStepperWidget` trong `gui_v2.py`.

## 4. Những lưu ý quan trọng (Important Notes)
1. **Rủi ro tự động hóa Playwright**: Quy trình phụ thuộc hoàn toàn vào cấu trúc web UI của Google Gemini. Nếu Gemini thay đổi giao diện DOM (như đổi tên class, ID thẻ chat), file `gemini_translate.py` sẽ bị lỗi selector và cần được bảo trì ngay.
2. **Quản lý Session Cookie (Playwright Profiles)**: Thư mục `chrome_data_*` rất nhạy cảm, không được xóa tùy tiện. Nếu mất, người dùng sẽ phải đăng nhập lại và dễ dính captcha từ Google.
3. **Môi trường TTS Gradio**: Link Gradio URL thường sinh ra từ các nền tảng serverless (như Google Colab) nên có vòng đời ngắn (chỉ sống vài giờ). Hệ thống thiết kế cơ chế Just-in-time pause để người dùng chủ động nạp link mới mà không gãy tiến trình, không nên sửa logic này.
4. **CapCut Draft JSON**: Logic ở `backend.py` thao tác và ghi đè trực tiếp vào file cấu trúc `.json` của project CapCut. Bất kỳ sự thay đổi cấu trúc dữ liệu nội bộ nào từ phía bản cập nhật của phần mềm CapCut PC đều có thể gây lỗi corrupt dự án (làm CapCut không mở được project). Cần sao lưu trước khi inject.
