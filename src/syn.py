import os
import pysrt
from pydub import AudioSegment

def check_srt_audio_sync(srt_path, wav_folder):
    print("==================================================")
    print("🔍 BẮT ĐẦU KIỂM TRA ĐỒNG BỘ SRT & AUDIO")
    print(f"📄 File SRT: {srt_path}")
    print(f"📁 Thư mục Audio: {wav_folder}")
    print("==================================================\n")

    if not os.path.exists(srt_path):
        print("❌ LỖI: Không tìm thấy file SRT!")
        return
    if not os.path.exists(wav_folder):
        print("❌ LỖI: Không tìm thấy thư mục Audio!")
        return

    # Đọc file SRT
    try:
        subs = pysrt.open(srt_path, encoding='utf-8')
    except Exception as e:
        print(f"❌ Lỗi đọc SRT: {e}")
        return

    total_blocks = len(subs)
    total_spillover = 0 # Tổng thời gian audio bị lố (cần nhường chỗ/đóng băng)
    
    print(f"{'Câu':<5} | {'Thời lượng SRT':<15} | {'Thời lượng Audio':<16} | {'Độ lệch (ms)':<15} | {'Trạng thái'}")
    print("-" * 75)

    for i, sub in enumerate(subs, 1):
        # Tính thời lượng của block SRT (bằng mili-giây)
        srt_duration_ms = sub.duration.ordinal
        
        # Đường dẫn file WAV tương ứng (VD: clip_001.wav)
        wav_filename = f"clip_{i:03d}.wav"
        wav_path = os.path.join(wav_folder, wav_filename)
        
        if not os.path.exists(wav_path):
            print(f"#{i:03d} | Không tìm thấy file audio: {wav_filename}")
            continue

        try:
            # Đo độ dài thực tế của file WAV
            audio = AudioSegment.from_file(wav_path)
            audio_duration_ms = len(audio)
            
            # So sánh
            diff_ms = audio_duration_ms - srt_duration_ms
            
            # Đánh giá trạng thái
            if diff_ms > 0:
                status = "🔴 Audio DÀI HƠN"
                total_spillover += diff_ms
            elif diff_ms < 0:
                status = "🟢 Audio ngắn hơn (An toàn)"
            else:
                status = "⚪ Khớp hoàn hảo"
                
            # In kết quả (chỉ in những câu bị dài hơn hoặc lệch trên 100ms để đỡ rối mắt, 
            # bạn có thể bỏ IF để in toàn bộ)
            if abs(diff_ms) > 100:
                print(f"#{i:03d} | {srt_duration_ms:<13} ms | {audio_duration_ms:<14} ms | {diff_ms:<13} | {status}")
                
        except Exception as e:
            print(f"#{i:03d} | ❌ Lỗi đọc audio: {e}")

    print("-" * 75)
    print(f"📊 TỔNG KẾT:")
    print(f"Tổng số câu đã kiểm tra: {total_blocks}")
    print(f"Tổng thời gian Audio dư ra (Nguy cơ gây lùi timeline): {total_spillover / 1000:.2f} giây")
    print("==================================================")

# ================= CÁCH SỬ DỤNG =================
if __name__ == "__main__":
    # Thay đổi đường dẫn này thành đường dẫn thực tế của bạn
    SRT_FILE = r"D:\Coder\Python\CapcutInjectorV2\source\0615\Srt\new_synced.srt"
    AUDIO_DIR = r"D:\Coder\Python\CapcutInjectorV2\source\0615\wav2"
    
    check_srt_audio_sync(SRT_FILE, AUDIO_DIR)