import os
import re
import wave
import contextlib
import unicodedata
import pysrt
from datetime import datetime

def get_wav_duration(filepath):
    """Lấy thời lượng audio bằng cách đọc Header (nhanh hơn load toàn bộ file)"""
    with contextlib.closing(wave.open(filepath, 'r')) as f:
        frames = f.getnframes()
        rate = f.getframerate()
        duration = frames / float(rate)
        return duration

def analyze_audio_wpm_and_log(srt_path, audio_dir, log_path, log_callback=print):
    log_lines = []

    def write_log(message):
        log_callback(message)
        log_lines.append(message)

    write_log(f"🕒 Bắt đầu quét lúc: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    write_log("=" * 165)

    try:
        subs = pysrt.open(srt_path, encoding="utf-8")
        write_log(f"✅ Đã tải {len(subs)} dòng phụ đề từ: {srt_path}")
    except Exception as e:
        write_log(f"❌ Lỗi đọc file SRT: {e}")
        return

    if not os.path.exists(audio_dir):
        write_log(f"❌ Không tìm thấy thư mục audio: {audio_dir}")
        return

    audio_files = [
        f for f in os.listdir(audio_dir)
        if f.startswith("clip_") and f.endswith(".wav")
    ]
    audio_files.sort()

    write_log("-" * 165)
    write_log(
        f"{'Tên File':<12} | "
        f"{'Âm tiết':<8} | "
        f"{'Ký tự':<8} | "
        f"{'Audio(s)':<9} | "
        f"{'SRT(s)':<8} | "
        f"{'SPM':<5} | "
        f"{'CPS':<5} | "
        f"{'Đánh giá SPM (Nhịp)':<22} | "
        f"{'Đánh giá CPS (Ký tự)':<22} | "
        f"{'Tình trạng Tràn'}"
    )
    write_log("-" * 165)

    total_overflow_s = 0
    overflow_count = 0
    total_syllables = 0
    total_chars = 0
    total_audio_duration = 0

    qualified_syllables = 0
    qualified_chars = 0
    qualified_duration = 0
    qualified_count = 0

    for filename in audio_files:
        match = re.search(r"clip_(\d+)\.wav", filename)
        if not match:
            continue

        index = int(match.group(1))

        if index < 1 or index > len(subs):
            write_log(f"{filename:<12} | ⚠️ Không tìm thấy dòng SRT tương ứng")
            continue

        sub = subs[index - 1]

        text = sub.text.replace("\n", " ")
        text = unicodedata.normalize('NFC', text) 
        clean_text = re.sub(r"[^\w\sÀ-ỹ]", " ", text)
        clean_text = re.sub(r"\s+", " ", clean_text).strip()

        syllable_count = len(clean_text.split())
        char_count = len(re.sub(r"\s+", "", clean_text))

        srt_duration_s = (sub.end.ordinal - sub.start.ordinal) / 1000.0
        
        filepath = os.path.join(audio_dir, filename)
        try:
            audio_duration_s = get_wav_duration(filepath)
        except Exception as e:
            write_log(f"{filename:<12} | ❌ Lỗi đọc audio: {e}")
            continue

        spm = 0
        cps = 0

        if audio_duration_s > 0:
            spm = (syllable_count / audio_duration_s) * 60
            cps = (char_count / audio_duration_s)

            total_syllables += syllable_count
            total_chars += char_count
            total_audio_duration += audio_duration_s

        speech_ratio = 0
        if srt_duration_s > 0:
            speech_ratio = audio_duration_s / srt_duration_s

        if spm > 180:
            spm_status = "🔴 QUÁ NHANH (>180)"
        elif spm > 160:
            spm_status = "🟡 HƠI GẤP (160-180)"
        elif spm < 110:
            spm_status = "🔵 HƠI CHẬM (<110)"
        else:
            spm_status = "✅ TỰ NHIÊN (110-160)"

        if cps > 18:
            cps_status = "🔴 QUÁ DÀY (>18)"
        elif cps > 15:
            cps_status = "🟡 HƠI DÀY (15-18)"
        elif cps < 7:
            cps_status = "🔵 HƠI LƯA THƯA (<7)"
        else:
            cps_status = "✅ ỔN ĐỊNH (7-15)"

        status = "✅ VỪA VẶN"
        if audio_duration_s > srt_duration_s:
            overflow = audio_duration_s - srt_duration_s
            status = f"❌ TRÀN {overflow:.2f}s"
            total_overflow_s += overflow
            overflow_count += 1

        is_qualified = (
            audio_duration_s <= srt_duration_s
            and spm <= 160
            and cps <= 15
            and speech_ratio >= 0.85
        )

        if is_qualified:
            qualified_syllables += syllable_count
            qualified_chars += char_count
            qualified_duration += audio_duration_s
            qualified_count += 1

        write_log(
            f"{filename:<12} | "
            f"{syllable_count:<8} | "
            f"{char_count:<8} | "
            f"{audio_duration_s:<9.2f} | "
            f"{srt_duration_s:<8.2f} | "
            f"{spm:<5.0f} | "
            f"{cps:<5.1f} | "
            f"{spm_status:<22} | "
            f"{cps_status:<22} | "
            f"{status}"
        )

    avg_spm = 0
    avg_cps = 0
    if total_audio_duration > 0:
        avg_spm = (total_syllables / total_audio_duration) * 60
        avg_cps = (total_chars / total_audio_duration)

    reference_spm = 0
    reference_cps = 0
    if qualified_duration > 0:
        reference_spm = (qualified_syllables / qualified_duration) * 60
        reference_cps = (qualified_chars / qualified_duration)

    write_log("=" * 165)
    write_log("📊 BÁO CÁO TỔNG QUAN")
    write_log(f"► Tổng số file quét: {len(audio_files)}")
    write_log("")
    write_log(f"► SPM trung bình (toàn bộ): {avg_spm:.0f} âm tiết/phút")
    write_log(f"► CPS trung bình (toàn bộ): {avg_cps:.1f} ký tự/giây")
    write_log("")
    write_log(f"► Clip đạt chuẩn (Không tràn, SPM ≤ 160, CPS ≤ 15): {qualified_count}/{len(audio_files)}")
    write_log(f"► SPM tham chiếu (đạt chuẩn): {reference_spm:.0f}")
    write_log(f"► CPS tham chiếu (đạt chuẩn): {reference_cps:.1f}")
    write_log("")
    write_log(f"► Số lượng clip bị tràn: {overflow_count}/{len(audio_files)}")
    
    if overflow_count > 0:
        write_log(f"► Trung bình mỗi clip lỗi tràn: {(total_overflow_s / overflow_count):.2f}s")
    
    write_log("=" * 165)

    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(log_lines))
        log_callback(f"\n💾 Đã lưu báo cáo thành công vào: {log_path}")
    except Exception as e:
        log_callback(f"\n❌ Lỗi khi lưu file log: {e}")

if __name__ == "__main__":
    SRT_FILE_PATH = "./source/srt/srt_0605VI_fixed.srt"
    AUDIO_DIRECTORY = "./source/wav/0605"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    LOG_FILE_PATH = f"tts_qa_report_{timestamp}.log"

    analyze_audio_wpm_and_log(
        SRT_FILE_PATH,
        AUDIO_DIRECTORY,
        LOG_FILE_PATH
    )