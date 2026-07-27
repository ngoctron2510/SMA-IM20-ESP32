import urequests
import os
import machine
import time

# Đường dẫn URL chính thức tới file main.py của bạn trên GitHub
OTA_URL = "https://raw.githubusercontent.com/ngoctron2510/SMA-IM20-ESP32/main/main.py"
LOCAL_FILE = "main.py"

def check_and_update():
    """Kiểm tra và cập nhật file main.py từ GitHub"""
    print("\n[OTA] Đang kiểm tra bản cập nhật code từ GitHub...")
    try:
        response = urequests.get(OTA_URL)
        if response.status_code == 200:
            new_code = response.text
            response.close()

            current_code = ""
            try:
                with open(LOCAL_FILE, "r") as f:
                    current_code = f.read()
            except OSError:
                print(f"[OTA] Chưa tìm thấy file {LOCAL_FILE}, sẽ tạo mới.")

            # So sánh nội dung code server và local
            if new_code != current_code:
                print(f"[OTA] Phát hiện phiên bản mới! Đang ghi đè {LOCAL_FILE}...")
                
                # Backup bản hiện tại phòng sự cố
                try:
                    with open(LOCAL_FILE + ".bak", "w") as f:
                        f.write(current_code)
                except:
                    pass

                # Ghi code mới
                with open(LOCAL_FILE, "w") as f:
                    f.write(new_code)

                print("[OTA] Cập nhật thành công! Đang khởi động lại ESP32...")
                time.sleep(1)
                machine.reset()
            else:
                print("[OTA] Code hiện tại đã là mới nhất.")
        else:
            print(f"[OTA] Không thể lấy file OTA (HTTP Code: {response.status_code})")
            response.close()
    except Exception as e:
        print("[OTA] Lỗi trong quá trình kiểm tra OTA:", e)