import os
import re

def split_srt_file(input_file_path, output_prefix="output", blocks_per_file=125, log_callback=print):

    if not os.path.exists(input_file_path):
        log_callback(f"❌ Lỗi: Không tìm thấy file '{input_file_path}'")
        return

    try:
        with open(input_file_path, 'r', encoding='utf-8') as file:
            content = file.read()
            
        # Chuẩn hóa ký tự xuống dòng
        content = content.replace('\r\n', '\n').strip()
        
        # Biểu thức Regex bắt chính xác điểm bắt đầu của một block SRT mới
        pattern = r'\n(?=\d+\s*\n\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3})'
        
        # Thêm '\n' vào đầu chuỗi để Regex bắt được cả block đầu tiên
        raw_blocks = re.split(pattern, '\n' + content)
        
        # Lọc bỏ các phần tử rỗng và xóa khoảng trắng thừa
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
                # Nối các block bằng '\n\n' để ép định dạng chuẩn, tự động sửa lỗi dính chữ
                out_file.write('\n\n'.join(chunk) + '\n\n')
                
            log_callback(f" [+] Đã tạo: {output_filename} (chứa {len(chunk)} block)")
            file_count += 1
            
        log_callback(f"\n✅ Hoàn thành! Đã chia thành {file_count - 1} file.")

    except Exception as e:
        log_callback(f"❌ Đã xảy ra lỗi trong quá trình xử lý: {e}")

if __name__ == "__main__":
    file_can_cat = './source/srt/0609.srt' 
    ten_file_dau_ra = './source/srtphude_past'
    
    split_srt_file(file_can_cat, ten_file_dau_ra, blocks_per_file=150)