# Project Architecture Overview

## 1. Tổng quan dự án (Project Overview)
Dự án là một hệ thống tự động hóa toàn diện (automation pipeline) hỗ trợ xử lý video và âm thanh. Quy trình làm việc chính bao gồm 6 bước:
1. **Tải Video/Audio**: Hỗ trợ tải từ Bilibili hoặc dùng file cục bộ.
2. **Tạo phụ đề (Subtitle Generation)**: Trích xuất và chia nhỏ file phụ đề gốc (SRT) sử dụng Whisper.
3. **Dịch thuật AI (Translation)**: Tự động hóa qua trình duyệt ẩn Playwright để điều khiển Google Gemini dịch thuật phụ đề với logic tách/gộp (Split/Merge) để vượt qua giới hạn độ dài văn bản.
4. **Auto QA & Repair**: Phân tích lỗi timecode, độ dài câu trong phụ đề dịch, trích xuất đoạn lỗi (chunks) và gọi Gemini để vá lỗi tự động trên bản gốc mà không phá hỏng mốc thời gian.
5. **Text-to-Speech (TTS)**: Sinh âm thanh qua API Gradio. Tích hợp cơ chế Just-in-time pause để đợi người dùng nạp lại URL khi server Gradio bị chết dọc đường. Đồng thời hỗ trợ Tạm Dừng/Dừng an toàn ngay giữa tiến trình chạy đa luồng.
6. **CapCut Injection**: Bơm (inject) trực tiếp hàng loạt file phụ đề và audio vào thẳng file draft JSON của ứng dụng CapCut PC, giúp tự động khớp timeline mà không cần chỉnh sửa tay. Hệ thống đã được nâng cấp để hỗ trợ luồng Pause/Stop hoàn chỉnh trong cả quá trình sinh Audio và Inject.

## 2. Cấu trúc thư mục mới (Refactored Directory Structure - MVC & OOP)
Kiến trúc dự án đã được Tái cấu trúc triệt để (Refactored) phân tách rõ ràng theo mô hình MVC (Model-View-Controller) và Hướng đối tượng (OOP):

- `gui_v2.py`: **[View/Controller]** File giao diện người dùng chính. Chỉ đảm nhiệm vẽ UI (PyQt6), tiếp nhận sự kiện click và cấu hình luồng chạy. Hoàn toàn không chứa logic xử lý backend.
- `src/`: Thư mục chứa toàn bộ logic cốt lõi (Core Business Logic)
  - `gemini_bot.py`: **[Core/Model]** Class `GeminiBot`. Trí não AI trung tâm, đóng gói 100% các thao tác Playwright (mở trình duyệt, quản lý Profile, bypass Flash-Lite, upload file, giám sát phản hồi). Nếu Google cập nhật UI, chỉ cần sửa duy nhất file này.
  - `srt_manager.py`: **[Core/Model]** "Tổng quản lý" phụ đề. Đóng gói toàn bộ các thuật toán xử lý text (cắt nhỏ/gộp SRT, dãn tốc độ thời gian, quét và phân tích lỗi QA, đắp bản vá LLM).
  - `workflow_translate.py`: **[Workflow]** Dây chuyền tự động dịch thuật. Nhận đầu vào, khởi tạo `GeminiBot` và `srt_manager` để hoàn tất vòng lặp dịch.
  - `workflow_qa.py`: **[Workflow]** Dây chuyền tự động vá lỗi. Đọc báo cáo lỗi, yêu cầu `GeminiBot` sửa và dùng `srt_manager` để chèn vào SRT.
  - `backend.py`: **[Core/Model]** Chuyên gia thao tác mã nguồn file Draft JSON của phần mềm CapCut PC.
  - `workers.py`: **[Controller/Thread]** Xử lý Đa luồng (Multi-threading). Chứa các class `QThread` (ProcessWorker, LoginWorker) để chạy nền toàn bộ các bước mà không làm đơ giao diện UI.
  - `bilibili_downloader.py`: Quản lý logic tải video bằng yt-dlp và auth cookie.
  - `subtitle_generator.py`: Module giao tiếp với AI Whisper.
  - `config_manager.py`: Chuyên quản lý việc lưu và nạp cấu hình tùy chọn của người dùng vào `user_config.json`.
  - `utils.py`: Các hàm tiện ích độc lập (ví dụ tìm đường dẫn cài CapCut, xóa rác AI).

- `user_data/`: Thư mục lưu trữ trạng thái lâu dài (Persistent State).
  - `chrome_profiles/`: Các profile Playwright chứa cookie phiên làm việc (giúp Gemini không bắt đăng nhập lại).
  - `config/`: Nơi chứa file `user_config.json` và log `profile_cooldowns.json`.
  - `prompts/`: Template câu lệnh gốc dành cho AI.

## 3. Kiến trúc State Management & Quy ước Code (Conventions)
### 3.1. Kiến trúc Đa luồng (Multi-threading) & UI
- **Framework**: **PyQt6**.
- **Tách biệt UI và Backend**: Mọi công việc nặng (tải file, dịch AI, xử lý JSON) BẮT BUỘC phải được ném vào các Worker Threads trong `src/workers.py`. UI (`gui_v2.py`) tuyệt đối không xử lý vòng lặp nặng.
- **Giao tiếp (Communication)**: Các luồng nền giao tiếp với UI thông qua cơ chế **Signals/Slots** (`pyqtSignal`).
- **Cơ chế Pause/Stop an toàn (Interrupts)**: Toàn bộ quá trình chạy được kiểm soát bởi `QWaitCondition` và `QMutex`. Cờ `check_pause_callback` được truyền xuyên suốt qua tất cả các module (từ Bilibili Downloader, Whisper, Playwright tới ThreadPool của Gradio TTS). Khi người dùng nhấn Pause/Stop, luồng nền sẽ phản hồi và dừng ngay lập tức mà không gây crash hoặc deadlock.
- **Auto Profile Rotation**: Hệ thống có khả năng theo dõi ngạch tài khoản Google (Quota). Nếu phát hiện account bị ép dùng bản Flash-Lite, hệ thống tự đánh dấu Cooldown (Khóa 5h) và tự động nhảy qua Account Pro khác để làm tiếp.

### 3.2. Coding Conventions
- **Biến/Hàm/File**: `snake_case` (`workflow_qa.py`, `upload_file_and_send`).
- **Lớp (Classes)**: `PascalCase` (`GeminiBot`, `ProcessWorker`, `ConfigManager`).
- **Bảo trì Selector**: Mọi HTML/CSS Selector cho Playwright đều phải được khai báo thành hằng số (Constants) ở đầu file (ví dụ ở đầu `gemini_bot.py`) để dễ thay thế.

## 4. Những lưu ý quan trọng về Rủi ro (Important Notes)
1. **Rủi ro giao diện Google Gemini (DOM Changes)**: Vì Playwright mô phỏng người thật, nếu Google thay đổi DOM (Tên Class, cấu trúc Input Chat), Bot sẽ trượt. Khi đó, chỉ cần cập nhật danh sách `_LOCATORS` trên đỉnh file `src/gemini_bot.py`. Tuyệt đối không cần sửa logic dưới workflow.
2. **Quản lý Session Chrome**: Các thư mục `chrome_data_*` ở `user_data` chứa Session ID. Nếu xóa, người dùng phải login lại từ đầu.
3. **Môi trường TTS Gradio (Colab)**: Link URL từ ngrok/colab sống rất ngắn. UI cần giữ luồng `WaitCondition` chờ người dùng nhập link mới. Không sửa logic blocking này.
4. **CapCut Draft JSON**: Logic chèn file ở `backend.py` thao tác trực tiếp mảng JSON. Hãy luôn backup file draft `draft_content.json` của CapCut trước khi inject. Cần lưu ý khi CapCut ra version major mới.


## 5. Chi tiết luồng xử lý kỹ thuật (Deep-Dive Technical Flow)
Hệ thống vận hành theo một dây chuyền (Pipeline) nghiêm ngặt, đầu ra của bước trước là đầu vào của bước sau. Dưới đây là chi tiết nguyên lý hoạt động của từng bước:

### Bước 1: Tải Video/Audio (Bilibili/Local)
- **Module xử lý:** `bilibili_downloader.py`
- **Cách hoạt động:** 
  - Nếu người dùng dán link Bilibili, hệ thống dùng `yt-dlp` (kết hợp với file Cookie trình duyệt nếu yêu cầu video premium) để tải video/audio về.
  - Các file được tải xuống sẽ được lưu tạm vào thư mục làm việc chung (thường là `./downloads`).
  - Đổi tên file về định dạng thống nhất (ví dụ: `video.mp4` hoặc `audio.wav`) để các bước sau dễ dàng tham chiếu.

### Bước 2: Sinh phụ đề gốc (Whisper/Speech-to-Text)
- **Module xử lý:** `subtitle_generator.py`
- **Cách hoạt động:**
  - Nhận file âm thanh `.wav` từ Bước 1. Trích xuất text thông qua mô hình AI Whisper (có thể qua API hoặc chạy local).
  - Kết quả trả về được lưu vào thư mục `1_SRT_CN` dưới dạng file `.srt`.
  - Nếu phụ đề gốc quá dài, hệ thống có thể cấu hình để tự động dãn tốc độ (0.8x) thông qua `srt_manager.py` để lấy thêm không gian thời gian (giúp bản tiếng Việt khi thu âm không bị đọc quá nhanh).

### Bước 3: Dịch thuật tự động (Translation Engine)
- **Module xử lý:** `workflow_translate.py` phối hợp cùng `gemini_bot.py`
- **Cách hoạt động (Rất phức tạp):**
  - **Khởi tạo thư mục:** Quét toàn bộ file trong `1_SRT_CN`. Tạo sẵn thư mục đích `2_SRT_VI`.
  - **Quản lý chia nhỏ (Split):** File SRT thường rất dài (chứa hàng trăm blocks). Hệ thống gọi `srt_manager.split_srt_file` để băm nhỏ file gốc ra (mặc định 100 blocks/file) đưa vào thư mục tạm `temp_split_cn_...`.
  - **Tương tác AI (Playwright):** 
    - Khởi chạy trình duyệt bằng Profile Chrome (`user_data/chrome_profiles/chrome_data_1`).
    - **Kiểm tra Quota:** `gemini_bot` sẽ check UI của Google Gemini xem có chữ "Flash-Lite" không. Nếu có, lập tức đánh dấu khóa Profile này 5 tiếng (lưu vào `user_data/config/profile_cooldowns.json`), đóng trình duyệt và tự động xoay vòng sang `chrome_data_2`.
    - **Tải file lên (Upload):** `gemini_bot` tìm thẻ input[type="file"]. Nếu không tìm thấy, bot tự động kích hoạt File Chooser hoặc sử dụng JS Injection để đẩy thẳng nội dung Text vào khung chat. Đi kèm là Prompt yêu cầu dịch và giữ nguyên ID/Timecode.
  - **Xác thực kết quả (Validation):** 
    - Lấy phản hồi từ khối code block markdown.
    - `srt_manager.is_srt_structure_match` đối chiếu file gốc và file dịch. Số lượng Block và mốc Thời gian (Timecode) PHẢI khớp 100%. Nếu AI làm sai/mất dòng, hệ thống tự động tải lại trang và bắt AI làm lại (Tối đa 3 lần).
  - **Gộp (Merge):** Cuối cùng, gộp các file nhỏ thành một file tiếng Việt hoàn chỉnh lưu vào `2_SRT_VI`. Xóa thư mục tạm. Hệ thống tự động tạo các mốc Checkpoint để nếu mất điện, khi chạy lại sẽ bỏ qua các file đã dịch xong.

### Bước 4: Tự động Sửa lỗi (Auto QA & Repair)
- **Module xử lý:** `workflow_qa.py` phối hợp cùng `srt_manager.py`
- **Cách hoạt động:**
  - **Quét lỗi (Scan):** `srt_manager` quét thư mục `2_SRT_VI` tìm các điểm bất thường như mốc thời gian đè lên nhau (Overlap) hoặc đoạn text dịch quá dài so với thời gian nói.
  - **Sinh Báo Cáo:** Xuất ra thư mục `3_QA_REPORTS` các file `.txt` chứa danh sách các cụm block (clusters) bị lỗi.
  - **Vá lỗi (Patch):** `workflow_qa` đọc file `.txt`, truyền cho `GeminiBot`. AI được yêu cầu chỉ trả về các đoạn SRT đã được fix. 
  - **Bơm bản vá:** `srt_manager.replace_blocks_in_folder` tiến hành gỡ block lỗi trong file SRT và thay thế bằng block vừa được AI sửa. File báo cáo được đổi đuôi thành `_da_sua.txt` để đánh dấu trạng thái.

### Bước 5: Sinh âm thanh (Gradio TTS)
- **Module xử lý:** Worker luồng riêng trong `workers.py`
- **Cách hoạt động:**
  - Duyệt qua từng Block trong file SRT tiếng Việt cuối cùng.
  - Gửi Text tới server Gradio TTS qua REST API. Lấy kết quả lưu thành các file `001.wav`, `002.wav` trong thư mục `5_AUDIO_VI`.
  - **Xử lý ngắt kết nối Just-in-time:** Link Gradio Colab thường hay chết đột ngột. Khi Request HTTP bị timeout hoặc trả mã lỗi, hệ thống không làm hỏng tiến trình mà đưa luồng Thread vào trạng thái Pause (Bằng `QWaitCondition`). Giao diện sẽ thông báo người dùng nạp lại URL mới. Sau khi nạp, luồng Thread tiếp tục Resume dịch từ chính Block đang bị dở dang.
  - **Kiểm soát Tạm Dừng/Dừng Đa Luồng:** Trong quá trình sinh TTS bằng `ThreadPoolExecutor` (gọi Gradio đồng thời), `check_pause_callback` được nhúng trực tiếp vào đầu hàm sinh âm thanh `generate_voice_clip`, đảm bảo ngay cả khi đang gọi hàng loạt request, ứng dụng vẫn có thể bị ngắt (Pause/Stop) ngay lập tức theo lệnh người dùng.

### Bước 6: Chèn nội dung vào CapCut (CapCut Injection)
- **Module xử lý:** `backend.py`
- **Cách hoạt động:**
  - Định vị thư mục dự án CapCut được chỉ định. Đọc file JSON cốt lõi của CapCut là `draft_content.json`.
  - Backup file JSON trước khi đụng chạm. Quét sạch các track AI cũ nếu người dùng tick chọn "Xóa âm thanh AI cũ".
  - Tính toán số Microsecond (đơn vị thời gian của CapCut) từ timecode của SRT.
  - Cập nhật mảng `tracks` (Track phụ đề và Audio track) và mảng `materials` (Khai báo UUID tham chiếu tới các file .wav trong ổ cứng).
  - Ghi đè file `draft_content.json`. Khi người dùng mở lại phần mềm CapCut PC, video, phụ đề và toàn bộ các khối âm thanh ghép giọng AI đã được căn sẵn khớp 100% với nhau trên Timeline.
