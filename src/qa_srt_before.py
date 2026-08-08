import re
import math
import os
from datetime import datetime

# ==============================================================================
# CẤU HÌNH NGƯỠNG QA — Chỉnh tại đây để tune toàn bộ hệ thống
# ==============================================================================
# [FIX] CPS_MID < CPS_HIGH < CPS_CRITICAL — 3 mức tách biệt, không trùng nhau
# Ngưỡng tính trên char KHÔNG có space (len(text.replace(' ', '')))
# và sau khi đã trừ thời gian pause ước tính của dấu câu
CPS_CRITICAL         = 40    # Ngưỡng chết: Bắt buộc phải sửa vì TTS sẽ hỏng hoàn toàn
CPS_CONSECUTIVE_WARN = 35    # Ngưỡng dồn toa: CPS > 35 bắt đầu gây hẹp timeline
MAX_CONSECUTIVE      = 2     # Số block liên tiếp vượt CPS_CONSECUTIVE_WARN bị coi là kẹt timeline

GAP_WARN_THRESHOLD   = 10.0  # Khoảng trống giữa 2 block (giây) bị coi là bất thường

# Pause ước tính (giây) mỗi loại dấu câu — dùng để tính effective_duration
PAUSE_COMMA         = 0.20  # dấu phẩy
PAUSE_SENTENCE      = 0.40  # dấu chấm, !, ?
PAUSE_ELLIPSIS      = 0.50  # dấu …
PAUSE_DASH          = 0.25  # dấu – hoặc - (ngắt thoại)

# Liên từ / giới từ / trợ từ cuối block = câu chắc chắn chưa kết thúc
# Đây là signal mạnh nhất, ít false positive nhất cho tiếng Việt
INCOMPLETE_ENDING_PATTERN = re.compile(
    r'\b(của|và|với|để|mà|thì|là|bởi|vì|nên|nhưng|hoặc|hay|khi|nếu|dù|tuy|'
    r'vẫn|đã|sẽ|đang|rằng|rồi|còn|cũng|lại|cứ|mới|chỉ|chưa|đến|tới|từ|'
    r'trong|ngoài|trên|dưới|trước|sau|bên|giữa|qua|về|theo|bằng|như|hơn|'
    r'hết|thêm|được|bị|cho|ra|vào|lên|xuống|sang|qua|đi|lại|lên|xuống)$',
    re.IGNORECASE
)

# Regex dấu câu kết thúc hợp lệ — bao gồm ellipsis, dash thoại,
# và dấu đóng ngoặc/ngoặc kép bọc sau dấu câu
VALID_ENDING_PATTERN = re.compile(
    r'[.,?!;:\u2026\u2013\u2014][\"\'\u201d\u2019\u00bb]?$'
)


# ==============================================================================
# HÀM TIỆN ÍCH
# ==============================================================================

def time_to_seconds(time_str):
    """Chuyển đổi chuỗi thời gian SRT (HH:MM:SS,mmm) sang giây."""
    h, m, s_ms = time_str.split(':')
    s, ms = s_ms.split(',')
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def calc_effective_cps(text, duration):
    """
    Tính CPS hiệu chỉnh cho tiếng Việt TTS.

    Cải tiến so với CPS thô:
    1. Chỉ đếm ký tự thật — bỏ space (space không mất thời gian phát âm)
    2. Trừ thời gian pause ước tính của dấu câu khỏi duration
       vì TTS tự chèn pause — nếu không trừ sẽ overestimate CPS của block
       có nhiều dấu câu và flag warning oan

    Ví dụ: "Nàng ta, vốn là con gái của tể tướng."
      - Thô: len("Nàng ta, vốn là con gái của tể tướng.") / duration
      - Hiệu chỉnh: len(text không space) / (duration - 0.2 - 0.4)
    """
    pause_time = (
        text.count(',')  * PAUSE_COMMA    +
        text.count('.')  * PAUSE_SENTENCE +
        text.count('!')  * PAUSE_SENTENCE +
        text.count('?')  * PAUSE_SENTENCE +
        text.count('\u2026') * PAUSE_ELLIPSIS +  # …
        text.count('\u2013') * PAUSE_DASH    +   # –
        text.count('\u2014') * PAUSE_DASH        # —
    )
    effective_duration = max(duration - pause_time, 0.1)
    char_count_no_space = len(text.replace(' ', ''))
    return round(char_count_no_space / effective_duration, 2) if effective_duration > 0 else 0


def format_block_info(block, label):
    """Định dạng block để in ra báo cáo rõ ràng cho LLM đọc hiểu."""
    return (
        f"[{label}]\n"
        f"{block['index']}\n"
        f"{block['timestamps']}\n"
        f"{block['text_original']}\n"
    )


# ==============================================================================
# HÀM PHÂN TÍCH CHÍNH
# ==============================================================================

def analyze_single_srt(file_path, log_callback=print, scan_mode='all'):
    """Hàm xử lý logic cốt lõi: Đọc file SRT và trả về danh sách lỗi."""
    if scan_mode == 'semantic': mode_text = "[CHỈ QUÉT GÃY CÂU]"
    elif scan_mode == 'cps':    mode_text = "[CHỈ QUÉT CPS]"
    else:                       mode_text = "[QUÉT TOÀN DIỆN]"

    log_callback(f"🔎 {mode_text} Đang phân tích: {os.path.basename(file_path)}...")
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
    except FileNotFoundError:
        log_callback(f"❌ Không tìm thấy file: {file_path}")
        return [], 0, 0, 0

    # ──────────────────────────────────────────────────────
    # BƯỚC 1: Parse toàn bộ SRT
    # ──────────────────────────────────────────────────────
    blocks_raw = re.split(r'\n\n+', content.replace('\r\n', '\n'))
    parsed_blocks = []

    for block in blocks_raw:
        lines = block.split('\n')
        if len(lines) < 3:
            continue

        index      = lines[0].strip()
        timestamps = lines[1].strip()

        # [FIX] Strip từng dòng text trước khi join để loại bỏ space thừa
        # giữa các dòng khi block có 2+ dòng — tránh sai lệch char_count
        text_original = '\n'.join(lines[2:])
        text_flat     = ' '.join(line.strip() for line in lines[2:])

        try:
            start_str, end_str = timestamps.split(' --> ')
            start_time = time_to_seconds(start_str.strip())
            end_time   = time_to_seconds(end_str.strip())
        except ValueError:
            continue

        duration = round(end_time - start_time, 3)

        # [FIX] char_count = ký tự KHÔNG space — nhất quán với calc_effective_cps
        char_count_no_space = len(text_flat.replace(' ', ''))
        cps_raw             = round(char_count_no_space / duration, 2) if duration > 0 else 0
        cps_effective       = calc_effective_cps(text_flat, duration)

        # Syllable count = số từ (tiếng Việt đơn âm tiết)
        syllable_count = len(text_flat.split())

        parsed_blocks.append({
            'index':            index,
            'timestamps':       timestamps,
            'start_time':       start_time,
            'end_time':         end_time,
            'text_original':    text_original,
            'text_flat':        text_flat,        # đã strip từng dòng, dùng cho mọi check
            'duration':         duration,
            'syllable_count':   syllable_count,
            'char_count':       char_count_no_space,
            'cps_raw':          cps_raw,
            'cps':              cps_effective,    # CPS hiệu chỉnh — dùng cho threshold check
            'errors':           []
        })

    # ──────────────────────────────────────────────────────
    # BƯỚC 2: Quét lỗi
    # ──────────────────────────────────────────────────────
    error_clusters  = []
    previous_end    = 0.0
    count_critical  = 0
    count_warning   = 0
    filename_base   = os.path.basename(file_path)

    for i, block in enumerate(parsed_blocks):
        errors       = []
        dur          = block['duration']
        text_flat    = block['text_flat']
        text_stripped = text_flat.strip()

        # ── GAP & OVERLAP CHECK ──────────────────────────
        if scan_mode in ['all', 'cps']:
            gap = block['start_time'] - previous_end

            # [NEW] Phát hiện overlap timestamp — lỗi nghiêm trọng, 2 clip phát đè nhau
            if i > 0 and gap < 0:
                errors.append(
                    f"CRITICAL: Timestamp OVERLAP với block trước ({abs(gap):.3f}s) "
                    f"— 2 clip TTS sẽ phát đè nhau"
                )
            # Khoảng trống bất thường
            elif i > 0 and gap > GAP_WARN_THRESHOLD:
                errors.append(
                    f"WARNING: Khoảng trống lớn trước block này "
                    f"({gap:.1f}s — ngưỡng {GAP_WARN_THRESHOLD}s)"
                )

        # ── SEMANTIC: PHÁT HIỆN NGẮT CÂU SAI ────────────
        if scan_mode in ['all', 'semantic']:

            has_valid_ending = bool(VALID_ENDING_PATTERN.search(text_stripped))

            # Signal 1 (MẠNH NHẤT): Kết thúc bằng liên từ/giới từ/trợ từ khi KHÔNG có dấu câu kết thúc
            if not has_valid_ending and INCOMPLETE_ENDING_PATTERN.search(text_stripped):
                errors.append(
                    "CRITICAL: Block kết thúc bằng liên từ/giới từ/trợ từ "
                    "— câu chưa hoàn chỉnh về ngữ pháp, cần gộp với block tiếp theo"
                )

            # Signal 2: Không có dấu câu hợp lệ → xem block tiếp theo
            elif not has_valid_ending and i + 1 < len(parsed_blocks):
                next_text    = parsed_blocks[i + 1]['text_flat'].strip()
                next_1st_char = next_text[0] if next_text else ''

                # Signal 2a: Block tiếp theo bắt đầu bằng chữ thường
                # → chắc chắn là ngắt câu lưng chừng
                if next_1st_char.islower():
                    errors.append(
                        "CRITICAL: Ngắt câu lưng chừng — block tiếp theo bắt đầu "
                        "bằng chữ thường, TTS sẽ bị vỡ ngữ điệu"
                    )

                # Signal 2b: Block tiếp theo bắt đầu bằng từ hoa ngắn thực sự nghi ngờ là tên riêng
                elif next_1st_char.isupper():
                    next_first_word = next_text.split()[0] if next_text.split() else ''
                    COMMON_START_WORDS = {
                        'Anh', 'Tôi', 'Nó', 'Cô', 'Khi', 'Một', 'Em', 'Cậu', 'Ta', 'Hắn',
                        'Bà', 'Ông', 'Mẹ', 'Bố', 'Chị', 'Chú', 'Bác', 'Nếu', 'Tuy', 'Dù',
                        'Sao', 'Vậy', 'Này', 'Nào', 'Làm', 'Đi', 'Đã', 'Sẽ', 'Đang'
                    }
                    if len(next_first_word) <= 4 and next_first_word not in COMMON_START_WORDS and dur < 0.8:
                        errors.append(
                            "WARNING: Có thể ngắt câu trước tên riêng — block tiếp theo "
                            "bắt đầu bằng từ viết hoa ngắn, block hiện tại không có dấu câu"
                        )
                    elif dur < 0.5:
                        errors.append(
                            "WARNING: Block quá ngắn (< 0.5s) và không có dấu câu kết thúc "
                            "— có thể là ngắt câu sai"
                        )

            # Signal 3: Block kết thúc bằng số → có thể tách khỏi đơn vị
            if re.search(r'\d+$', text_stripped):
                errors.append(
                    "WARNING: Block kết thúc bằng số — có thể bị tách khỏi "
                    "đơn vị đo lường, TTS sẽ đọc sai ngữ cảnh"
                )

        # ── CPS & DURATION CHECK ─────────────────────────
        if scan_mode in ['all', 'cps']:

            # Duration không hợp lệ
            if dur <= 0:
                errors.append("CRITICAL: Duration <= 0 — sẽ gây crash hệ thống")
                dur = 0.001
            elif dur < 0.5 and not has_valid_ending:
                errors.append(f"WARNING: Duration quá ngắn ({dur}s) — TTS có thể không kịp render")

            cps = block['cps']
            if cps > CPS_CRITICAL:
                errors.append(
                    f"CRITICAL: CPS = {cps} (ngưỡng {CPS_CRITICAL}) "
                    f"— TTS sẽ nuốt âm hoặc bị drop từ"
                )
            elif cps > CPS_CONSECUTIVE_WARN:
                errors.append(
                    f"WARNING: CPS = {cps} (ngưỡng {CPS_CONSECUTIVE_WARN}) "
                    f"— cần rút ngắn nội dung"
                )

        # ── ĐÓNG GÓI LỖI ─────────────────────────────────
        if errors:
            block['errors'] = errors

            for e in errors:
                if e.startswith("CRITICAL"): count_critical += 1
                else:                        count_warning  += 1

            cluster_text = []
            cluster_text.append(
                f"========== [{filename_base}] - LỖI TẠI BLOCK {block['index']} =========="
            )
            cluster_text.append("🚨 VẤN ĐỀ:\n- " + "\n- ".join(errors) + "\n")

            if i > 0:
                cluster_text.append(format_block_info(parsed_blocks[i - 1], "N-1 (Câu trước)"))

            cluster_text.append(format_block_info(block, "N (CÂU BỊ LỖI)"))

            if i < len(parsed_blocks) - 1:
                cluster_text.append(format_block_info(parsed_blocks[i + 1], "N+1 (Câu sau)"))

            cluster_text.append("------------------------\n")
            error_clusters.append("\n".join(cluster_text))

        previous_end = block['end_time']

    return error_clusters, len(parsed_blocks), count_critical, count_warning


# ==============================================================================
# HÀM LƯU FILE TỔNG HỢP VÀ CHUYỂN MẠCH
# ==============================================================================

def save_reports(error_clusters, output_filename, total_blocks, count_critical,
                 count_warning, errors_per_file, log_callback, scan_mode='all'):
    """Hàm chia nhỏ số lỗi thành các file part chứa tối đa errors_per_file."""
    if not error_clusters:
        log_callback("   ↳ 🎉 Mọi dữ liệu đều ổn định, không tìm thấy lỗi.")
        return

    total_errors = len(error_clusters)
    error_rate   = round(total_errors / total_blocks * 100, 1) if total_blocks > 0 else 0
    total_files  = math.ceil(total_errors / errors_per_file)

    log_callback(
        f"📊 TỔNG KẾT: {total_blocks} block | {total_errors} lỗi ({error_rate}%) "
        f"| LỖI NGHIÊM TRỌNG: {count_critical} | WARNING: {count_warning}"
    )

    try:
        out_dir = os.path.dirname(output_filename)
        
        # Trích xuất tên folder cuối cùng từ đường dẫn
        # VD: "C:/Videos/Tap_01" -> Lấy ra "Tap_01"
        folder_name = os.path.basename(out_dir) if out_dir else "Report"
        
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            # Dọn dẹp các file report cũ trước khi lưu báo cáo mới
            for old_f in os.listdir(out_dir):
                if (old_f.lower().startswith('report_') or old_f.lower().startswith(f"{folder_name.lower()}_")) and old_f.lower().endswith('.txt'):
                    try:
                        os.remove(os.path.join(out_dir, old_f))
                    except Exception:
                        pass

        _, ext = os.path.splitext(output_filename)
        if not ext: ext = ".txt" # Đảm bảo luôn có đuôi file
        
        timestamp_str  = datetime.now().strftime("%Y-%m-%d %H:%M")

        for file_idx in range(total_files):
            start_idx = file_idx * errors_per_file
            end_idx   = min(start_idx + errors_per_file, total_errors)
            chunk     = error_clusters[start_idx:end_idx]

            # TẠO TÊN FILE MỚI: Chỉ gồm [Tên folder]_[Số thứ tự]
            # VD: Tap_01_1.txt, Tap_01_2.txt
            new_file_name = f"{folder_name}_{file_idx + 1}{ext}"
            chunk_filename = os.path.join(out_dir, new_file_name)

            with open(chunk_filename, 'w', encoding='utf-8') as f:
                if scan_mode == 'semantic':
                    f.write("BÁO CÁO LỖI PHỤ ĐỀ (CHỈ LỖI NGẮT CÂU LƯNG CHỪNG)\n")
                elif scan_mode == 'cps':
                    f.write("BÁO CÁO LỖI PHỤ ĐỀ (CHỈ LỖI TRÀN TỐC ĐỘ CPS & KHOẢNG TRỐNG)\n")
                else:
                    f.write("BÁO CÁO LỖI PHỤ ĐỀ (NGẮT CÂU SAI, TRÀN TỐC ĐỘ, KHOẢNG TRỐNG)\n")

                f.write(f"Thời điểm  : {timestamp_str}\n")
                f.write(
                    "Đọc các block lỗi bên dưới và xử lý theo SRT Repair Engine. "
                    "Được phép chỉnh Timestamp trong phạm vi cụm 3 block (N-1, N, N+1).\n"
                )
                f.write("=" * 60 + "\n\n")
                f.write("\n".join(chunk))

        if total_files > 1:
            log_callback(
                f"   ↳ ⚠️ Đã tự động chia thành {total_files} báo cáo nhỏ "
                f"({errors_per_file} lỗi/file)."
            )
        else:
            log_callback(f"   ↳ ✅ Đã xuất báo cáo ra file: {new_file_name}")

    except Exception as e:
        log_callback(f"   ↳ ❌ Lỗi khi lưu file báo cáo: {e}")


def analyze_srt_to_file(in_path, out_path, errors_per_file=80,
                        log_callback=print, scan_mode='all'):
    """
    Hàm giao tiếp với GUI.
    Tự động nhận diện xử lý 1 File đơn lẻ hoặc Hàng loạt Thư Mục.
    Hỗ trợ tham số scan_mode: 'all' | 'semantic' | 'cps'
    """
    if not os.path.exists(in_path):
        log_callback(f"❌ Lỗi: Không tìm thấy đường dẫn gốc '{in_path}'.")
        return

    all_error_clusters = []
    total_blocks_all   = 0
    total_crit_all     = 0
    total_warn_all     = 0

    if scan_mode == 'semantic': mode_display = "CHỈ LỖI GÃY CÂU"
    elif scan_mode == 'cps':    mode_display = "CHỈ LỖI TỐC ĐỘ CPS"
    else:                       mode_display = "TOÀN DIỆN"

    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]

    if os.path.isfile(in_path):
        log_callback(f"📄 CHẾ ĐỘ: Quét {mode_display} — 1 file đơn lẻ...\n")
        files_to_process = [in_path]
    elif os.path.isdir(in_path):
        log_callback(f"📁 CHẾ ĐỘ: Quét {mode_display} — hàng loạt thư mục...\n")
        files_to_process = sorted(
            [
                os.path.join(in_path, f)
                for f in os.listdir(in_path)
                if f.lower().endswith('.srt')
            ],
            key=lambda x: natural_sort_key(os.path.basename(x))
        )
        if not files_to_process:
            log_callback(f"⚠️ Không tìm thấy file .srt nào trong thư mục: {in_path}")
            return
    else:
        log_callback(f"❌ Đường dẫn không hợp lệ: {in_path}")
        return

    for file in files_to_process:
        clusters, b_cnt, c_cnt, w_cnt = analyze_single_srt(file, log_callback, scan_mode)
        all_error_clusters.extend(clusters)
        total_blocks_all += b_cnt
        total_crit_all   += c_cnt
        total_warn_all   += w_cnt

    log_callback("\n" + "=" * 50)
    save_reports(
        all_error_clusters, out_path,
        total_blocks_all, total_crit_all, total_warn_all,
        errors_per_file, log_callback, scan_mode
    )
    log_callback(f"📁 Thư mục lưu báo cáo: {os.path.dirname(os.path.abspath(out_path))}")
    return len(all_error_clusters), total_crit_all, total_warn_all