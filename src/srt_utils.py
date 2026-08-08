import os
import re
import wave
import contextlib
import unicodedata
from datetime import datetime, timedelta
from typing import Callable, Optional, Dict, Any, List

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
