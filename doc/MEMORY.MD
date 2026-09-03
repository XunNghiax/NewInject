# 🧠 Developer Memory & Knowledge Base
*(Bản đồ Trí nhớ Kỹ thuật dành cho Developer và AI)*

## 1. Core Modules Mapping (Chi tiết các Module Cốt lõi)

### 1.1. `src/gemini_bot.py`
- **Class:** `GeminiBot`
- **Nhiệm vụ:** Đóng gói Playwright để mô phỏng thao tác trình duyệt.
- **Tính năng kỹ thuật nổi bật:**
  - `is_profile_in_cooldown()` / `get_next_available_pro_profile()`: Thuật toán **Auto Profile Rotation**. Nếu AI nhả về cảnh báo "Flash-Lite" (bị giới hạn Quota), hệ thống tự động ghi nhận vào log, khóa (cooldown) profile đó trong 5 giờ, đóng trình duyệt và tự động nhảy sang profile dự phòng (`chrome_data_2`, v.v.) để chạy tiếp.
  - Xử lý bypass DOM (Document Object Model) của Google. Dùng Selector constants trên cùng file để dễ bảo trì khi Google cập nhật UI.

### 1.2. `src/srt_manager.py`
- **Nhiệm vụ:** Trái tim của quá trình phân tích Text & Timecode.
- **Tính năng kỹ thuật nổi bật:**
  - `split_srt_file()` & `merge_numbered_srt_files()`: Cắt file SRT lớn thành các block nhỏ (mặc định ~100-125 blocks/file) đưa vào thư mục tạm `temp_split_cn_...` để né giới hạn token của LLM, sau đó gộp lại.
  - `analyze_srt_to_file()`: Quét và phân tích Overlap (chồng chéo thời gian) hoặc tính CPS (Characters Per Second) phát hiện câu quá dài.
  - `replace_blocks_in_folder()`: Đọc các đoạn text do AI sinh ra (bản vá QA) và dò tìm ID block tương ứng để chèn đè vào nội dung cũ mà KHÔNG làm vỡ cấu trúc thời gian của các line lân cận.

### 1.3. `src/backend.py`
- **Class:** `CapCutBackend`
- **Nhiệm vụ:** Thao tác Database JSON của phần mềm CapCut PC.
- **Tính năng kỹ thuật nổi bật:**
  - Tính toán Microseconds (1 giây = 1,000,000µs).
  - **Zero-Duration Guard:** Sử dụng Pydub quét `actual_duration_ms` trước khi inject. Chặn ngay các file âm thanh lỗi (dưới 100ms hoặc chỉ có header) để không làm crash CapCut.
  - **Strict Material Validation:** Sinh mã MD5 chính xác cho nội dung audio + Gán mã `uuid.uuid4()` cho `local_material_id` để bypass hệ thống kiểm tra file ngoại lai của CapCut.
  - **Smart Timeline:** Thuật toán N và N-1 đẩy các đoạn audio chồng chéo xuống các Track mới (Layer âm thanh riêng biệt) để ngăn tiếng bị đè/giật.

### 1.4. `src/workers.py`
- **Nhiệm vụ:** Kéo dãn các vòng lặp xử lý nặng vào Background Threads, giải phóng UI.
- **Tính năng kỹ thuật nổi bật:**
  - Các Class kế thừa từ `QThread` (ví dụ: `ProcessWorker`, `ChromeLoginWorker`).
  - Dùng Signal (`pyqtSignal`) bắn trạng thái cập nhật (Log/Progress) về UI.
  - **Just-in-time pause / Interrupts:** Sử dụng `QWaitCondition` và `QMutex`. Nếu API Gradio sập, Thread bị Pause chứ không chết. Nếu User bấm Stop, thread sẽ Return thoát ra an toàn.

## 2. Pipeline Folders & Data Flow (Luồng di chuyển dữ liệu)
Toàn bộ dự án đi theo cơ chế Pipeline tuyến tính:
1. `downloads/` ➔ Chứa video/audio thô tải về (Bilibili/yt-dlp).
2. `1_SRT_CN/` ➔ Chứa phụ đề tiếng gốc (Whisper bóc băng).
3. `temp_split_cn_.../` ➔ Thư mục tạm chứa các file đã chẻ nhỏ để đưa cho Gemini.
4. `2_SRT_VI/` ➔ Phụ đề đã dịch xong và gộp thành công.
5. `3_QA_REPORTS/` ➔ Nơi chứa báo cáo lỗi thời gian (TXT). Nếu AI sửa xong sẽ đổi tên thành `_da_sua.txt`.
6. `5_AUDIO_VI/` ➔ Các đoạn âm thanh (WAV) từng câu do Gradio TTS sinh ra, chờ để bơm vào CapCut.
7. `user_data/chrome_profiles/` ➔ Chứa Cookie trình duyệt, là xương sống giúp GeminiBot chạy không cần login.

## 3. Cảnh báo (Watch-outs & Pitfalls)
- **Tuyệt đối KHÔNG** viết logic xử lý vòng lặp for/while nặng vào trong các file UI (`ui/main_window.py`). Mọi thứ phải đẩy qua `workers.py`.
- Khi cập nhật phiên bản CapCut PC mới, cần kiểm tra lại logic parsing `draft_content.json` bên trong `backend.py` vì định dạng mảng `materials` có thể bị hãng thay đổi.
