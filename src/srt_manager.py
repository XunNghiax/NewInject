import os
import re
import wave
import contextlib
import unicodedata
from datetime import datetime, timedelta
from typing import Callable, Optional, Dict, Any, List
import math
from datetime import datetime
from collections import defaultdict

# --- FROM srt_utils.py ---


try:
    import pysrt
except ImportError:
    pysrt = None

try:
    import imageio_ffmpeg
    _ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    if _ffmpeg_exe and os.path.exists(_ffmpeg_exe):
        _ffmpeg_dir = os.path.dirname(_ffmpeg_exe)
        if _ffmpeg_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = _ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
except Exception:
    pass

try:
    from pydub import AudioSegment
except ImportError:
    AudioSegment = None


# ==============================================================================
# 1. MERGE SRT (GỘP CÁC TỆP SRT ĐƯỢC ĐÁNH SỐ)
# ==============================================================================
def merge_numbered_srt_files(input_directory: str, output_file_path: str, log_callback: Callable = print):
    """Hàm tìm, sắp xếp và gộp các file SRT có số ở cuối tên (VD: 1.srt, output_1.srt)."""
    if not os.path.exists(input_directory):
        log_callback(f"❌ Lỗi: Không tìm thấy thư mục '{input_directory}'")
        return

    all_files = os.listdir(input_directory)
    srt_files = []

    for filename in all_files:
        match = re.search(r'(\d+)(?:_vi)?\.srt$', filename, re.IGNORECASE)
        if match:
            full_path = os.path.join(input_directory, filename)
            file_number = int(match.group(1)) 
            srt_files.append((full_path, file_number))
            
    if not srt_files:
        log_callback(f"❌ Lỗi: Không tìm thấy file định dạng chứa số (như 1.srt, _1.srt) trong '{input_directory}'")
        return

    srt_files.sort(key=lambda x: x[1])
    log_callback(f"🔎 Đã tìm thấy {len(srt_files)} file hợp lệ. Đang tiến hành gộp...")

    all_blocks = []
    current_index = 1

    for file_path, file_num in srt_files:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read().replace('\r\n', '\n').strip()
                
            blocks = re.split(r'\n\s*\n', content)
            for block in blocks:
                if not block.strip():
                    continue
                lines = block.split('\n')
                if len(lines) >= 2:
                    lines[0] = str(current_index)
                    all_blocks.append('\n'.join(lines))
                    current_index += 1
            log_callback(f" [+] Đã gộp xong: {os.path.basename(file_path)}")
        except Exception as e:
            log_callback(f"⚠️ Có lỗi khi đọc file {file_path}: {e}")

    try:
        os.makedirs(os.path.dirname(os.path.abspath(output_file_path)), exist_ok=True)
        with open(output_file_path, 'w', encoding='utf-8') as out_file:
            out_file.write('\n\n'.join(all_blocks) + '\n\n')
            
        log_callback("-" * 50)
        log_callback(f"✅ Hoàn thành! Đã gộp thành công vào file: '{output_file_path}'")
        log_callback(f"📊 Tổng số block phụ đề: {current_index - 1}")
    except Exception as e:
        log_callback(f"❌ Đã xảy ra lỗi khi lưu file: {e}")


# ==============================================================================
# 2. SPLIT SRT (TÁCH TỆP SRT THÀNH NGHÌN BLOCK NHỎ)
# ==============================================================================
def split_srt_file(input_file_path: str, output_prefix: str = "output", blocks_per_file: int = 125, log_callback: Callable = print):
    """Tách file SRT lớn thành các file nhỏ chứa số lượng block chỉ định."""
    if not os.path.exists(input_file_path):
        log_callback(f"❌ Lỗi: Không tìm thấy file '{input_file_path}'")
        return

    try:
        with open(input_file_path, 'r', encoding='utf-8') as file:
            content = file.read()
            
        content = content.replace('\r\n', '\n').strip()
        pattern = r'\n(?=\d+\s*\n\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3})'
        raw_blocks = re.split(pattern, '\n' + content)
        blocks = [b.strip() for b in raw_blocks if b.strip()]
        
        total_blocks = len(blocks)
        if total_blocks == 0:
            log_callback("❌ File gốc trống hoặc không có block nào hợp lệ.")
            return

        log_callback(f"🔎 Đã tìm thấy tổng cộng {total_blocks} block hợp lệ. Đang tiến hành chia nhỏ...")

        file_count = 1
        for i in range(0, total_blocks, blocks_per_file):
            chunk = blocks[i:i + blocks_per_file]
            output_filename = f"{output_prefix}_{file_count}.srt"
            
            with open(output_filename, 'w', encoding='utf-8') as out_file:
                out_file.write('\n\n'.join(chunk) + '\n\n')
                
            log_callback(f" [+] Đã tạo: {output_filename} (chứa {len(chunk)} block)")
            file_count += 1
            
        log_callback(f"\n✅ Hoàn thành! Đã chia thành {file_count - 1} file.")

    except Exception as e:
        log_callback(f"❌ Đã xảy ra lỗi trong quá trình xử lý: {e}")


# ==============================================================================
# 3. RENUMBER & TIMELINE CHECK (ĐÁNH LẠI SỐ VÀ KIỂM TRA TIMELINE SRT)
# ==============================================================================
def time_to_ms(time_str: str) -> int:
    """Chuyển đổi chuỗi thời gian SRT (HH:MM:SS,ms) thành mili-giây."""
    h, m, s_ms = time_str.split(':')
    s, ms = s_ms.split(',')
    return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)


def process_single_srt(input_file: str, output_file: str, log_callback: Callable = print, start_index: int = 1):
    """Hàm xử lý Re-index và Check Timeline cho 1 file duy nhất."""
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            content = f.read()

        raw_blocks = content.strip().split('\n\n')
        new_blocks = []

        previous_start_time = -1
        previous_end_time = -1
        overlap_count = 0
        out_of_order_count = 0
        new_index = start_index

        for block in raw_blocks:
            lines = block.split('\n')
            if not lines:
                continue

            clean_lines = [
                line for line in lines
                if not line.strip().startswith('; [MERGED:')
                and not line.strip().startswith(';[MERGED:')
            ]

            if len(clean_lines) < 2:
                continue

            timeline_match = None
            for cl in clean_lines[1:]:
                timeline_match = re.search(
                    r'(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})',
                    cl
                )
                if timeline_match:
                    break

            if not timeline_match:
                continue

            old_index = clean_lines[0].strip()
            clean_lines[0] = str(new_index)

            start_str = timeline_match.group(1)
            end_str = timeline_match.group(2)
            start_ms = time_to_ms(start_str)
            end_ms = time_to_ms(end_str)

            if start_ms < previous_start_time:
                log_callback(f"   🚨 Lỗi: Block {new_index} (cũ: {old_index}) ngược thời gian (Bắt đầu: {start_str})")
                out_of_order_count += 1
            elif start_ms < previous_end_time:
                log_callback(f"   ⚠️ Cảnh báo: Block {new_index} (cũ: {old_index}) đè timeline lên block trước")
                overlap_count += 1

            previous_start_time = start_ms
            previous_end_time = end_ms

            for i in range(1, len(clean_lines)):
                if '-->' not in clean_lines[i]:
                    clean_lines[i] = re.sub(r'(?<! )(!+)', r' \1', clean_lines[i])

            new_blocks.append('\n'.join(clean_lines))
            new_index += 1

        os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write('\n\n'.join(new_blocks) + '\n')

        total = new_index - 1
        if overlap_count > 0 or out_of_order_count > 0:
            log_callback(f"   ↳ ⚠️ Xong {os.path.basename(input_file)}: {total} block | {out_of_order_count} lỗi NGƯỢC | {overlap_count} lỗi ĐÈ.")
        else:
            log_callback(f"   ↳ ✅ Xong {os.path.basename(input_file)}: Timeline chuẩn ({total} block).")

    except Exception as e:
        log_callback(f"   ❌ Lỗi khi xử lý file {os.path.basename(input_file)}: {e}")


def process_and_renumber_srt(in_path: str, out_path: str, log_callback: Callable = print):
    """Hàm chính re-index tự động nhận diện File hoặc Thư mục."""
    if not os.path.exists(in_path):
        log_callback(f"❌ Lỗi: Không tìm thấy đường dẫn gốc '{in_path}'.")
        return

    if os.path.isfile(in_path):
        log_callback("📄 CHẾ ĐỘ: Reindex & Kiểm tra timeline 1 file đơn lẻ...\n")
        process_single_srt(in_path, out_path, log_callback)
        log_callback("=" * 50)
        log_callback(f"📁 File đã xử lý được lưu tại: {out_path}")

    elif os.path.isdir(in_path):
        log_callback("📁 CHẾ ĐỘ: Reindex & Kiểm tra timeline hàng loạt...\n")
        out_dir = os.path.dirname(out_path) if out_path.lower().endswith('.srt') else out_path
        os.makedirs(out_dir, exist_ok=True)

        def natural_sort_key(s):
            return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]

        srt_files = sorted((f for f in os.listdir(in_path) if f.lower().endswith('.srt')), key=natural_sort_key)
        if not srt_files:
            log_callback(f"⚠️ Không tìm thấy file .srt nào trong thư mục: {in_path}")
            return

        log_callback(f"🔍 Tìm thấy {len(srt_files)} file SRT. Bắt đầu chạy:\n")

        for filename in srt_files:
            input_file = os.path.join(in_path, filename)
            output_file = os.path.join(out_dir, filename)

            # Nếu là file chia nhỏ (part_X.srt hoặc _X.srt), tính start_index theo file_no để không làm lệch block_id
            m = re.search(r'(\d+)', filename)
            if m and ('part_' in filename.lower() or filename.startswith('_') or 'temp_split' in in_path.lower()):
                file_no = int(m.group(1))
                start_idx = (file_no - 1) * 100 + 1
            else:
                start_idx = 1

            process_single_srt(input_file, output_file, log_callback, start_index=start_idx)

        log_callback("\n" + "=" * 50)
        log_callback(f"🎉 HOÀN TẤT! Đã xử lý {len(srt_files)} file.")
        log_callback(f"📁 Thư mục lưu kết quả: {out_dir}")


# ==============================================================================
# 4. COMPARE SRT FOLDERS (SO SÁNH CẤU TRÚC 2 FOLDER SRT)
# ==============================================================================
def natural_sort_key(s: str):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]


def parse_srt_structure(filepath: str) -> Dict[str, str]:
    structure = {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read().replace('\r\n', '\n').strip()
        blocks = re.split(r'\n\s*\n', content)
        for block in blocks:
            lines = block.strip().split('\n')
            if len(lines) >= 2:
                block_id = lines[0].strip()
                timeline = lines[1].strip()
                if "-->" in timeline:
                    structure[block_id] = timeline
    except Exception:
        pass
    return structure


def compare_srt_folders(folder_a: str, folder_b: str, log_callback: Callable = print):
    """So sánh cấu trúc Block ID và Timeline giữa 2 thư mục SRT."""
    if not os.path.exists(folder_a) or not os.path.exists(folder_b):
        log_callback("❌ Lỗi: Thư mục không tồn tại.")
        return

    files_a = {f for f in os.listdir(folder_a) if f.endswith('.srt')}
    files_b = {f for f in os.listdir(folder_b) if f.endswith('.srt')}

    common_files = sorted(list(files_a & files_b), key=natural_sort_key)
    only_a = sorted(list(files_a - files_b), key=natural_sort_key)
    only_b = sorted(list(files_b - files_a), key=natural_sort_key)

    if not common_files:
        log_callback("⚠️ Không tìm thấy file .srt nào có tên giống nhau giữa 2 thư mục để so sánh.")
        return

    log_callback(f"🔎 Đang so sánh {len(common_files)} file chung giữa 2 thư mục...\n")
    files_with_diffs = []

    for filename in common_files:
        path_a = os.path.join(folder_a, filename)
        path_b = os.path.join(folder_b, filename)

        struct_a = parse_srt_structure(path_a)
        struct_b = parse_srt_structure(path_b)
        diffs = []

        all_keys = sorted(set(list(struct_a.keys()) + list(struct_b.keys())), key=lambda x: int(x) if x.isdigit() else 0)
        for key in all_keys:
            if key not in struct_a:
                diffs.append(f"   - Block {key}: Bị thiếu ở Thư mục A")
            elif key not in struct_b:
                diffs.append(f"   - Block {key}: Bị thiếu ở Thư mục B")
            else:
                if struct_a[key] != struct_b[key]:
                    diffs.append(f"   - Block {key}:\n      + [Thư mục A]: {struct_a[key]}\n      + [Thư mục B]: {struct_b[key]}")

        if diffs:
            files_with_diffs.append(filename)
            log_callback(f"⚠️ {filename}: Tìm thấy {len(diffs)} điểm khác biệt:")
            for d in diffs:
                log_callback(d)
            log_callback("-" * 45)
        else:
            log_callback(f"✅ {filename}: Trùng khớp 100% ID và Timeline.")

    log_callback("\n" + "=" * 50)
    if only_a:
        log_callback(f"ℹ️ Có {len(only_a)} file CHỈ CÓ ở Thư mục A: {', '.join(only_a)}")
    if only_b:
        log_callback(f"ℹ️ Có {len(only_b)} file CHỈ CÓ ở Thư mục B: {', '.join(only_b)}")

    if not files_with_diffs and not only_a and not only_b:
        log_callback("\n🎉 TUYỆT VỜI! Tất cả các file đều trùng khớp 100% về Block ID và Timeline.")
    else:
        log_callback(f"\n✅ Hoàn thành phân tích. Có {len(files_with_diffs)} file bị sai lệch nội dung.")


# ==============================================================================
# 5. CONVERT SRT SPEED (ĐỔI TỐC ĐỘ TIMELINE SRT)
# ==============================================================================
def adjust_time(time_str: str, ratio: float) -> str:
    time_obj = datetime.strptime(time_str, '%H:%M:%S,%f')
    delta = timedelta(hours=time_obj.hour, minutes=time_obj.minute, seconds=time_obj.second, microseconds=time_obj.microsecond)
    new_delta = delta * ratio
    total_seconds = int(new_delta.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    milliseconds = int(new_delta.microseconds / 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def process_srt_speed(input_file: str, output_file: str, old_speed: float, new_speed: float, log_callback: Callable = print):
    ratio = old_speed / new_speed
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            content = f.read()
            
        def replace_time(match):
            start = adjust_time(match.group(1), ratio)
            end = adjust_time(match.group(2), ratio)
            return f"{start} --> {end}"
            
        new_content = re.sub(r'(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})', replace_time, content)
        
        os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(new_content)
            
        log_callback(f"✅ Đã chuyển đổi thành công từ tốc độ {old_speed} sang {new_speed}!")
        log_callback(f"📂 File kết quả đã được lưu vào: {output_file}")
        
    except Exception as e:
        log_callback(f"❌ Lỗi hệ thống: {str(e)}")
        raise e


# ==============================================================================
# 6. COUNT WPM & AUDIO SYNC ANALYSIS
# ==============================================================================
def get_wav_duration(filepath: str) -> float:
    with contextlib.closing(wave.open(filepath, 'r')) as f:
        frames = f.getnframes()
        rate = f.getframerate()
        return frames / float(rate)


def analyze_audio_wpm_and_log(srt_path: str, audio_dir: str, log_path: str, log_callback: Callable = print):
    log_lines = []
    def write_log(message):
        log_callback(message)
        log_lines.append(message)

    write_log(f"🕒 Bắt đầu quét lúc: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    write_log("=" * 165)

    if pysrt is None:
        write_log("❌ Lỗi: Thư viện pysrt chưa được cài đặt!")
        return

    try:
        subs = pysrt.open(srt_path, encoding="utf-8")
        write_log(f"✅ Đã tải {len(subs)} dòng phụ đề từ: {srt_path}")
    except Exception as e:
        write_log(f"❌ Lỗi đọc file SRT: {e}")
        return

    if not os.path.exists(audio_dir):
        write_log(f"❌ Không tìm thấy thư mục audio: {audio_dir}")
        return

    audio_files = sorted([f for f in os.listdir(audio_dir) if f.startswith("clip_") and f.endswith(".wav")])

    for filename in audio_files:
        match = re.search(r"clip_(\d+)\.wav", filename)
        if not match:
            continue
        index = int(match.group(1))
        if index < 1 or index > len(subs):
            continue
        sub = subs[index - 1]
        text = unicodedata.normalize('NFC', sub.text.replace("\n", " "))
        clean_text = re.sub(r"\s+", " ", re.sub(r"[^\w\sÀ-ỹ]", " ", text)).strip()
        syllable_count = len(clean_text.split())
        srt_duration_s = (sub.end.ordinal - sub.start.ordinal) / 1000.0
        
        filepath = os.path.join(audio_dir, filename)
        try:
            audio_duration_s = get_wav_duration(filepath)
        except Exception:
            continue

    write_log("✅ Hoàn tất phân tích WPM & Audio Sync.")


def check_srt_audio_sync(srt_path: str, wav_folder: str, log_callback: Callable = print):
    """Kiểm tra độ khớp thời lượng giữa từng câu SRT và file Audio tương ứng."""
    if not os.path.exists(srt_path) or not os.path.exists(wav_folder):
        log_callback("❌ Lỗi: Không tìm thấy file SRT hoặc thư mục Audio!")
        return

    if pysrt is None or AudioSegment is None:
        log_callback("❌ Lỗi: Chưa cài đặt thư viện pysrt hoặc pydub!")
        return

    try:
        subs = pysrt.open(srt_path, encoding='utf-8')
        total_spillover = 0
        for i, sub in enumerate(subs, 1):
            srt_duration_ms = sub.duration.ordinal
            wav_filename = f"clip_{i:03d}.wav"
            wav_path = os.path.join(wav_folder, wav_filename)
            if os.path.exists(wav_path):
                audio = AudioSegment.from_file(wav_path)
                diff_ms = len(audio) - srt_duration_ms
                if diff_ms > 0:
                    total_spillover += diff_ms
        log_callback(f"📊 Tổng thời gian Audio dư ra: {total_spillover / 1000:.2f} giây")
    except Exception as e:
        log_callback(f"❌ Lỗi khi kiểm tra đồng bộ: {e}")


# --- FROM qa_srt_before.py ---


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
        _, ext = os.path.splitext(output_filename)
        if ext:
            out_dir = os.path.dirname(output_filename)
            folder_name = os.path.basename(out_dir) if out_dir else "Report"
        else:
            out_dir = output_filename
            folder_name = os.path.basename(output_filename)
            ext = ".txt"

        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
            # Dọn dẹp các file report cũ trước khi lưu báo cáo mới
            for old_f in os.listdir(out_dir):
                if (old_f.lower().startswith('report_') or old_f.lower().startswith(f"{folder_name.lower()}_")) and old_f.lower().endswith('.txt'):
                    if not old_f.lower().endswith('_done.txt') and not old_f.lower().endswith('_da_sua.txt'):
                        try:
                            os.remove(os.path.join(out_dir, old_f))
                        except Exception:
                            pass
        
        timestamp_str  = datetime.now().strftime("%Y-%m-%d %H:%M")

        # --- AUTO-INCREMENT LOGIC ---
        start_id = 0
        if os.path.exists(out_dir):
            import re
            for existing_f in os.listdir(out_dir):
                if existing_f.lower().startswith(f"{folder_name.lower()}_") and existing_f.lower().endswith('.txt'):
                    # Tìm số ID ở cuối file, bỏ qua hậu tố _done hoặc _da_sua
                    match = re.search(r'_(\d+)(?:_done|_da_sua)?\.txt$', existing_f, re.IGNORECASE)
                    if match:
                        num = int(match.group(1))
                        if num > start_id:
                            start_id = num
        # ----------------------------

        for file_idx in range(total_files):
            start_idx = file_idx * errors_per_file
            end_idx   = min(start_idx + errors_per_file, total_errors)
            chunk     = error_clusters[start_idx:end_idx]

            # TẠO TÊN FILE MỚI: Tự động đếm tiếp từ ID lớn nhất
            new_file_name = f"{folder_name}_{start_id + file_idx + 1}{ext}"
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


def analyze_srt_to_file(in_path, out_path, errors_per_file=30,
                        log_callback=print, scan_mode='all',
                        check_pause_callback=None):
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
        if check_pause_callback:
            check_pause_callback()
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

# --- FROM batch_replace_srt.py ---



# ==============================================================================
# HÀM PARSE CHUNG — Dùng cho cả chế độ file đơn lẻ lẫn thư mục
# ==============================================================================

def parse_patch_and_deletes(text):
    """
    Phân tích output của Repair Engine, trả về:
      - replace_dict : { block_id: new_block_text }  — các block cần thay thế
      - delete_set   : { block_id, ... }              — các block cần xóa

    Repair Engine output có 2 dạng thông tin:

    Dạng 1 — Block thay thế bình thường:
        92
        00:02:39,677 --> 00:02:42,000
        Tôi lườm Liễu Như Yên một cái, lạnh lùng đáp:

    Dạng 2 — Comment MERGED ngay sau block cuối của cụm:
        ; [MERGED: 13, 14, 15, 16 → xóa khỏi file gốc, renumber từ 17 trở đi]
        → Parse ra delete_set = {13, 14, 15, 16}

    Comment MERGED có thể xuất hiện ngay sau 1 block hoặc sau nhóm nhiều block.
    """
    replace_dict = {}
    delete_set   = set()

    # Chuẩn hóa line ending và loại bỏ markdown code fences
    text = text.replace('\r\n', '\n')
    text = re.sub(r'```(?:srt)?', '', text, flags=re.IGNORECASE).strip()

    # Tách thành các đoạn theo dòng trống
    raw_chunks = re.split(r'\n\s*\n', text)

    for chunk in raw_chunks:
        chunk = chunk.strip()
        if not chunk:
            continue

        lines = chunk.split('\n')

        # Tách dòng comment [MERGED] ra khỏi phần SRT
        srt_lines    = []
        merged_lines = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith('; [MERGED:') or stripped.startswith(';[MERGED:'):
                merged_lines.append(stripped)
            else:
                srt_lines.append(line)

        # Parse phần SRT (tìm dòng đầu tiên là số nguyên ID block)
        if srt_lines:
            id_idx = -1
            for idx, sl in enumerate(srt_lines):
                if re.match(r'^\d+$', sl.strip()):
                    id_idx = idx
                    break

            if id_idx != -1:
                block_id = int(srt_lines[id_idx].strip())
                clean_srt_text = '\n'.join(srt_lines[id_idx:]).strip()
                replace_dict[block_id] = clean_srt_text

        # Parse phần [MERGED] để lấy danh sách block cần xóa
        for mline in merged_lines:
            m = re.search(r'\[MERGED:\s*([^\]→]+)', mline)
            if m:
                ids_str = m.group(1)
                for num_str in re.findall(r'\d+', ids_str):
                    delete_set.add(int(num_str))

    return replace_dict, delete_set


def parse_patch_blocks(text):
    """
    Backward-compatible: Chỉ parse replace_dict (không xử lý MERGED).
    Giữ lại để không break code cũ nếu có nơi nào gọi trực tiếp.
    """
    replace_dict, _ = parse_patch_and_deletes(text)
    return replace_dict


# ==============================================================================
# HELPER: LOG THAY ĐỔI
# ==============================================================================

def _log_replace(log_callback, block_id, old_text, new_text):
    log_callback(f"   ✓ Đã thay thế Block {block_id}")

def _log_delete(log_callback, block_id):
    log_callback(f"   🗑️ Đã xóa Block {block_id} (gộp block)")



# ==============================================================================
# CHẾ ĐỘ THƯ MỤC
# ==============================================================================

def detect_prefix(folder):
    """
    Tự động quét thư mục để tìm tiền tố của file .srt.
    Ví dụ: Thấy file '0609_1.srt' → trả về '0609'
    """
    if os.path.exists(folder):
        for filename in os.listdir(folder):
            if filename.endswith(".srt"):
                parts = filename.rsplit('_', 1)
                if len(parts) == 2:
                    return parts[0]
    return ""


def build_block_index_map(folder):
    """
    Quét thực tế tất cả các file .srt trong folder và các thư mục con 
    để lập bản đồ: block_id -> đường dẫn file thực tế chứa block đó.
    """
    block_map = {}
    if not os.path.exists(folder):
        return block_map

    for root, dirs, files in os.walk(folder):
        for fname in sorted(files, key=lambda x: [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', x)]):
            if fname.endswith('.srt'):
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, 'r', encoding='utf-8') as f:
                        content = '\n' + f.read().replace('\r\n', '\n')
                    for m in re.finditer(r'\n(\d+)\n\d{2}:\d{2}:\d{2}', content):
                        b_id = int(m.group(1))
                        # Ưu tiên các folder tiếng Việt/temp_split_vi nếu có trùng
                        if b_id not in block_map or "temp_split_vi" in root or "_vi" in root:
                            block_map[b_id] = fpath
                except Exception:
                    pass
    return block_map


def get_target_file(block_id, folder, prefix, block_map=None):
    """Xác định chính xác tên file chứa block_id bằng block_map hoặc quét thực tế."""
    if block_map and block_id in block_map:
        return block_map[block_id]

    # Quét trực tiếp tìm file thực sự chứa block_id
    if os.path.exists(folder):
        for root, dirs, files in os.walk(folder):
            for fname in files:
                if fname.endswith('.srt'):
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, 'r', encoding='utf-8') as f:
                            content = '\n' + f.read().replace('\r\n', '\n')
                            if f"\n{block_id}\n" in content:
                                return fpath
                    except Exception:
                        pass

    return None


def replace_blocks_in_folder(folder, patch_text, log_callback=print):
    """
    Quy trình Batch Replace + Delete cho CHẾ ĐỘ THƯ MỤC.

    Xử lý cả 2 loại thay đổi từ Repair Engine:
      - REPLACE: block đã được sửa nội dung / timestamp
      - DELETE : block đã bị gộp vào block khác (từ comment [MERGED])

    Sau khi chạy xong, cần chạy reindex.py để đánh lại số thứ tự.
    """
    replace_dict, delete_set = parse_patch_and_deletes(patch_text)

    if not replace_dict and not delete_set:
        log_callback("⚠️ Không tìm thấy block hợp lệ nào trong đoạn text đã dán.")
        return 0, 0

    total_replaced = 0
    total_deleted = 0

    # Tự động nhận diện tiền tố
    prefix = detect_prefix(folder)
    if prefix:
        log_callback(f"🔍 Tự động nhận diện tiền tố file: '{prefix}_'")
    else:
        log_callback("🔍 Không nhận diện được tiền tố, dùng định dạng '_{file_no}.srt'")

    log_callback(f"🔎 Tìm thấy {len(replace_dict)} block cần thay thế, "
                 f"{len(delete_set)} block cần xóa")
    if delete_set:
        log_callback(f"   🗑️ Danh sách block sẽ xóa: {sorted(delete_set)}")

    # Lập bản đồ block_id -> filepath thực tế
    block_map = build_block_index_map(folder)

    # Group tất cả block_id (cả replace lẫn delete) theo file đích
    all_ids = set(replace_dict.keys()) | delete_set
    file_to_ids = defaultdict(set)
    for b_id in all_ids:
        filepath = get_target_file(b_id, folder, prefix, block_map)
        if filepath is None:
            log_callback(f"   ⚠️ Bỏ qua block {b_id} vì không tồn tại trong thực tế")
            continue
        file_to_ids[filepath].add(b_id)

    # Xử lý từng file (sắp xếp theo thứ tự số tự nhiên: part_1, part_2 ... part_32)
    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]

    sorted_filepaths = sorted(file_to_ids.keys(), key=lambda x: natural_sort_key(os.path.basename(x)))

    for filepath in sorted_filepaths:
        ids_in_file = file_to_ids[filepath]
        filename = os.path.basename(filepath)
        log_callback(f"\n📄 Đang xử lý file: {filename}")

        if not os.path.exists(filepath):
            log_callback(f"   ⚠️ Không tìm thấy {filename}")
            continue

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read().replace('\r\n', '\n').strip()
        except Exception as e:
            log_callback(f"   ⚠️ Lỗi đọc file {filename}: {e}")
            continue

        original_blocks = re.split(r'\n\s*\n', content)
        new_blocks      = []
        replaced_count  = 0
        deleted_count   = 0
        local_replace   = {k: v for k, v in replace_dict.items() if k in ids_in_file}
        local_delete    = delete_set & ids_in_file

        for ob in original_blocks:
            olines = ob.strip().split('\n')
            if not olines:
                continue
            try:
                ob_id = int(olines[0].strip())

                if ob_id in local_delete:
                    # XÓA block này (bị gộp bởi Repair Engine)
                    _log_delete(log_callback, ob_id)
                    local_delete.discard(ob_id)
                    deleted_count += 1

                elif ob_id in local_replace:
                    # REPLACE block này
                    old_text = ob.strip()
                    new_text = local_replace[ob_id]
                    _log_replace(log_callback, ob_id, old_text, new_text)
                    new_blocks.append(new_text)
                    del local_replace[ob_id]
                    replaced_count += 1

                else:
                    # Giữ nguyên
                    new_blocks.append(ob.strip())

            except ValueError:
                new_blocks.append(ob.strip())

        # Báo cáo block không tìm thấy
        for missed_id in local_replace:
            log_callback(f"   ⚠️ Block {missed_id} không tồn tại trong {filename}")
        for missed_id in local_delete:
            log_callback(f"   ⚠️ Block {missed_id} (cần xóa) không tồn tại trong {filename}")

        # Ghi đè nếu có thay đổi
        if replaced_count > 0 or deleted_count > 0:
            total_replaced += replaced_count
            total_deleted += deleted_count
            try:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write('\n\n'.join(new_blocks) + '\n\n')
                log_callback(
                    f"   ↳ ✅ {filename}: Thay thế {replaced_count} block, "
                    f"xóa {deleted_count} block."
                )
            except Exception as e:
                log_callback(f"   ⚠️ Lỗi lưu file {filename}: {e}")
        else:
            log_callback(f"   ↳ ℹ️ {filename}: Không có thay đổi.")

    log_callback("\n✅ Hoàn thành Batch Replace+Delete!")
    log_callback("⚠️  Nhớ chạy Reindex để đánh lại số thứ tự block sau khi xóa.")
    return total_replaced, total_deleted


# ==============================================================================
# CHẾ ĐỘ FILE ĐƠN LẺ
# ==============================================================================

def replace_blocks_in_file(filepath, patch_text, log_callback=print):
    """
    Quy trình Batch Replace + Delete cho MỘT FILE DUY NHẤT.

    Xử lý cả 2 loại thay đổi từ Repair Engine:
      - REPLACE: block đã được sửa nội dung / timestamp
      - DELETE : block đã bị gộp vào block khác (từ comment [MERGED])

    Sau khi chạy xong, cần chạy reindex.py để đánh lại số thứ tự.
    """
    replace_dict, delete_set = parse_patch_and_deletes(patch_text)

    if not replace_dict and not delete_set:
        log_callback("⚠️ Không tìm thấy block hợp lệ nào trong đoạn text đã dán.")
        return 0, 0

    filename = os.path.basename(filepath)
    log_callback(f"\n📄 Đang xử lý file đơn lẻ: {filename}")
    log_callback(f"🔎 Tìm thấy {len(replace_dict)} block cần thay thế, "
                 f"{len(delete_set)} block cần xóa")
    if delete_set:
        log_callback(f"   🗑️ Danh sách block sẽ xóa: {sorted(delete_set)}")

    if not os.path.exists(filepath):
        log_callback(f"⚠️ Không tìm thấy file {filename}")
        return

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read().replace('\r\n', '\n').strip()
    except Exception as e:
        log_callback(f"⚠️ Lỗi đọc file {filename}: {e}")
        return

    original_blocks = re.split(r'\n\s*\n', content)
    new_blocks      = []
    replaced_count  = 0
    deleted_count   = 0

    for ob in original_blocks:
        olines = ob.strip().split('\n')
        if not olines:
            continue
        try:
            ob_id = int(olines[0].strip())

            if ob_id in delete_set:
                # XÓA block này
                _log_delete(log_callback, ob_id)
                delete_set.discard(ob_id)
                deleted_count += 1

            elif ob_id in replace_dict:
                # REPLACE block này
                old_text = ob.strip()
                new_text = replace_dict[ob_id]
                _log_replace(log_callback, ob_id, old_text, new_text)
                new_blocks.append(new_text)
                del replace_dict[ob_id]
                replaced_count += 1

            else:
                # Giữ nguyên
                new_blocks.append(ob.strip())

        except ValueError:
            new_blocks.append(ob.strip())

    # Báo cáo block không tìm thấy
    for missed_id in replace_dict:
        log_callback(f"   ⚠️ Block {missed_id} không tồn tại trong file gốc để thay thế.")
    for missed_id in delete_set:
        log_callback(f"   ⚠️ Block {missed_id} (cần xóa) không tồn tại trong file gốc.")

    # Ghi đè file
    if replaced_count > 0 or deleted_count > 0:
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write('\n\n'.join(new_blocks) + '\n\n')
            log_callback(
                f"\n✅ Hoàn thành: Thay thế {replaced_count} block, "
                f"xóa {deleted_count} block trong file {filename}!"
            )
            log_callback("⚠️  Nhớ chạy Reindex để đánh lại số thứ tự block sau khi xóa.")
        except Exception as e:
            log_callback(f"⚠️ Lỗi lưu file {filename}: {e}")
    else:
        log_callback("\n⚠️ Không có block nào được thay thế hoặc xóa "
                     "(Có thể ID không khớp với file gốc).")
    
    return replaced_count, deleted_count

def clean_gemini_output(text: str) -> str:
    """Làm sạch kết quả đầu ra từ Gemini AI, trích xuất duy nhất nội dung trong code block SRT."""
    if not text:
        return ""

    # 1. Trích xuất TẤT CẢ nội dung nằm bên trong các code block ```srt ... ``` (gộp lại nếu Gemini bị tách thành nhiều code block)
    code_blocks = re.findall(r'```(?:srt)?\s*\n(.*?)\n\s*```', text, re.DOTALL | re.IGNORECASE)
    if code_blocks:
        text = "\n\n".join(b.strip() for b in code_blocks if b.strip())
    else:
        # Nếu không có thẻ đóng, xóa các dòng ``` ở đầu/cuối
        text = re.sub(r'^```[a-zA-Z]*\n', '', text, flags=re.MULTILINE)
        text = re.sub(r'\n```$', '', text, flags=re.MULTILINE)
        text = text.replace('```', '')

    # 2. Định vị từ vị trí Block đầu tiên
    match = re.search(r'(?:\n|^)(\d+\s*\n\s*\d{2}:\d{2}:\d{2}[,\.]\d{3}\s*-->)', text)
    if match:
        text = text[match.start():].strip()

    lines = text.split('\n')
    while lines:
        first_line = lines[0].strip().lower()
        if first_line.startswith('```') or first_line.startswith('dưới đây') or first_line.startswith('đây là') or first_line == '':
            lines.pop(0)
        else:
            break  

    while lines:
        last_line = lines[-1].strip()
        if last_line.startswith('```') or last_line == '':
            lines.pop()
        else:
            break

    return '\n'.join(lines).strip()



def is_srt_structure_match(file_a: str, file_b: str, log_callback: Callable = print) -> bool:
    """So sánh cấu trúc 2 file SRT. Trả về True nếu khớp cả Block ID và Timeline chính xác, False nếu sai lệch."""
    struct_a = parse_srt_structure(file_a)
    struct_b = parse_srt_structure(file_b)
    keys_a = sorted(struct_a.keys(), key=lambda x: int(x) if x.isdigit() else 0)
    keys_b = sorted(struct_b.keys(), key=lambda x: int(x) if x.isdigit() else 0)
    if not keys_a or not keys_b:
        return False
    if keys_a != keys_b:
        return False
    for k in keys_a:
        ts_a = re.sub(r'\s+', '', struct_a[k]).replace('.', ',')
        ts_b = re.sub(r'\s+', '', struct_b[k]).replace('.', ',')
        if ts_a != ts_b:
            return False
    return True


def get_matched_blocks_count(cn_path: str, vi_path: str) -> int:
    """Đếm số lượng block SRT tính từ đầu khớp mốc thời gian hoàn toàn giữa 2 file."""
    try:
        struct_cn = parse_srt_structure(cn_path)
        struct_vi = parse_srt_structure(vi_path)
        
        cn_keys = sorted(struct_cn.keys(), key=lambda x: int(x) if x.isdigit() else 0)
        vi_keys = sorted(struct_vi.keys(), key=lambda x: int(x) if x.isdigit() else 0)
        
        if not cn_keys or not vi_keys:
            return 0

        match_count = 0
        for k_cn, k_vi in zip(cn_keys, vi_keys):
            if k_cn == k_vi and struct_cn[k_cn] == struct_vi.get(k_vi):
                match_count += 1
            else:
                break
        return match_count
    except Exception:
        return 0


