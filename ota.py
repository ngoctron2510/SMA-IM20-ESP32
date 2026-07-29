import urequests
import os
import machine
import time
import gc

# Đường dẫn URL chính thức tới file main.py của bạn trên GitHub
OTA_URL = "https://raw.githubusercontent.com/ngoctron2510/SMA-IM20-ESP32/main/main.py"
LOCAL_FILE = "main.py"
TEMP_FILE = "main.py.tmp"

def check_and_update():
    """Kiểm tra và cập nhật file main.py từ GitHub theo dạng Stream chunk 512B (Tiết kiệm RAM tuyệt đối)"""
    print("\n[OTA] Đang kiểm tra bản cập nhật code từ GitHub...")
    gc.collect()
    response = None
    try:
        # Sử dụng stream=True để không nạp toàn bộ file vào RAM
        response = urequests.get(OTA_URL, stream=True)
        if response.status_code == 200:
            # Ghi file tạm main.py.tmp theo từng chunk 512 bytes
            with open(TEMP_FILE, "wb") as f_tmp:
                while True:
                    chunk = response.raw.read(512)
                    if not chunk:
                        break
                    f_tmp.write(chunk)
            
            response.close()
            response = None
            gc.collect()

            # So sánh file mới tải (TEMP_FILE) và file hiện tại (LOCAL_FILE)
            is_different = False
            try:
                stat_tmp = os.stat(TEMP_FILE)
                stat_local = os.stat(LOCAL_FILE)
                
                # So sánh dung lượng file trước
                if stat_tmp[6] != stat_local[6]:
                    is_different = True
                else:
                    # So sánh từng chunk 512B để không ngốn RAM
                    with open(TEMP_FILE, "rb") as f1, open(LOCAL_FILE, "rb") as f2:
                        while True:
                            b1 = f1.read(512)
                            b2 = f2.read(512)
                            if b1 != b2:
                                is_different = True
                                break
                            if not b1:
                                break
            except OSError:
                is_different = True  # Nếu chưa có LOCAL_FILE thì coi như khác nhau

            if is_different:
                print(f"[OTA] Phát hiện phiên bản mới! Đang ghi đè {LOCAL_FILE}...")
                
                # Backup bản hiện tại phòng sự cố
                try:
                    os.remove(LOCAL_FILE + ".bak")
                except:
                    pass
                try:
                    os.rename(LOCAL_FILE, LOCAL_FILE + ".bak")
                except:
                    pass

                # Đổi tên file tạm thành main.py
                os.rename(TEMP_FILE, LOCAL_FILE)

                print("[OTA] Cập nhật thành công! Đang khởi động lại ESP32...")
                gc.collect()
                time.sleep(1)
                machine.reset()
            else:
                print("[OTA] Code hiện tại đã là mới nhất.")
                # Xóa file tạm
                try:
                    os.remove(TEMP_FILE)
                except:
                    pass
        else:
            print(f"[OTA] Không thể lấy file OTA (HTTP Code: {response.status_code})")
            if response:
                response.close()
    except Exception as e:
        print("[OTA] Lỗi trong quá trình kiểm tra OTA:", e)
        try:
            os.remove(TEMP_FILE)
        except:
            pass
    finally:
        if response:
            try:
                response.close()
            except:
                pass
        gc.collect()
        print(f"Đã dọn dẹp module OTA")
