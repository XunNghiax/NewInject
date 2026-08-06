import os
import re

def merge_numbered_srt_files(input_directory, output_file_path, log_callback=print):
    """
    Hàm tìm, sắp xếp và gộp các file SRT có số ở cuối tên (VD: 1.srt, _1.srt, output_1.srt).
    """
    if not os.path.exists(input_directory):
        log_callback(f"❌ Lỗi: Không tìm thấy thư mục '{input_directory}'")
        return

    all_files = os.listdir(input_directory)
    srt_files = []

    for filename in all_files:
        # Sử dụng re.search để tìm con số nằm ngay trước đuôi .srt (bỏ qua các ký tự ở đầu)
        match = re.search(r'(\d+)\.srt$', filename)
        if match:
            full_path = os.path.join(input_directory, filename)
            file_number = int(match.group(1)) 
            srt_files.append((full_path, file_number))
            
    if not srt_files:
        log_callback(f"❌ Lỗi: Không tìm thấy file định dạng chứa số (như 1.srt, _1.srt) trong '{input_directory}'")
        return

    # Sắp xếp danh sách file theo thứ tự số (từ 1 đến hết)
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
        with open(output_file_path, 'w', encoding='utf-8') as out_file:
            out_file.write('\n\n'.join(all_blocks) + '\n\n')
            
        log_callback("-" * 50)
        log_callback(f"✅ Hoàn thành! Đã gộp thành công vào file: '{output_file_path}'")
        log_callback(f"📊 Tổng số block phụ đề: {current_index - 1}")
    except Exception as e:
        log_callback(f"❌ Đã xảy ra lỗi khi lưu file: {e}")

if __name__ == "__main__":
    thu_muc_chua_file = "./source/srt/" 
    file_gop_hoan_chinh = "./source/srt/0609/merged_output.srt"
    merge_numbered_srt_files(thu_muc_chua_file, file_gop_hoan_chinh)