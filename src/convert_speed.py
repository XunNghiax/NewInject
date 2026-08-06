import re
import os
from datetime import datetime, timedelta

def adjust_time(time_str, ratio):
    # Chuyển đổi chuỗi thời gian SRT thành object timedelta
    time_obj = datetime.strptime(time_str, '%H:%M:%S,%f')
    delta = timedelta(hours=time_obj.hour, minutes=time_obj.minute, seconds=time_obj.second, microseconds=time_obj.microsecond)
    
    # Tính toán thời gian mới dựa trên tỷ lệ
    new_delta = delta * ratio
    
    # Định dạng lại thành chuỗi SRT chuẩn
    total_seconds = int(new_delta.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    milliseconds = int(new_delta.microseconds / 1000)
    
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"

# Đã thêm log_callback để xuất text ra GUI thay vì print
def process_srt_speed(input_file, output_file, old_speed, new_speed, log_callback=print):
    ratio = old_speed / new_speed
    
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            content = f.read()
            
        def replace_time(match):
            start = adjust_time(match.group(1), ratio)
            end = adjust_time(match.group(2), ratio)
            return f"{start} --> {end}"
            
        # Dùng Regex để tìm và thay thế toàn bộ mốc thời gian
        new_content = re.sub(r'(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})', replace_time, content)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(new_content)
            
        log_callback(f"✅ Đã chuyển đổi thành công từ tốc độ {old_speed} sang {new_speed}!")
        log_callback(f"📂 File kết quả đã được inject vào: {output_file}")
        
    except Exception as e:
        log_callback(f"❌ Lỗi hệ thống: {str(e)}")
        raise e