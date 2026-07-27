import network
import time
import webrepl
import ota
import machine
from machine import Pin

# -------------------------------------------------------------
# 1. CẤU HÌNH ETHERNET (Điều chỉnh theo phần cứng của bạn)
# Ví dụ bên dưới phổ biến cho chip LAN8720
# -------------------------------------------------------------
lan = network.LAN(
    mdc=machine.Pin(23),
    mdio=machine.Pin(18),
    power=machine.Pin(16),
    phy_type=network.PHY_LAN8720,
    phy_addr=1
)

lan.active(True)

print("Đang kết nối Ethernet...", end="")
timeout = 0
while not lan.isconnected() and timeout < 20:  # Chờ tối đa 10s
    time.sleep(0.5)
    print(".", end="")
    timeout += 1

if lan.isconnected():
    ip_info = lan.ifconfig()
    print(f"\n[Ethernet] Kết nối thành công! IP: {ip_info[0]}")
    
    # ---------------------------------------------------------
    # 2. KHỞI ĐỘNG WEBREPL
    # ---------------------------------------------------------
    try:
        webrepl.start()
        print(f"[WebREPL] Đã bật tại ws://{ip_info[0]}:8266/")
    except Exception as e:
        print("[WebREPL] Lỗi khi khởi động:", e)

    # ---------------------------------------------------------
    # 3. CHẠY OTA UPDATE
    # ---------------------------------------------------------
    ota.check_and_update()

else:
    print("\n[Ethernet] Lỗi: Không thể kết nối Ethernet!")

# Tự động cấu hình mật khẩu cho WebREPL nếu chưa có
try:
    import webrepl_cfg
except ImportError:
    with open("webrepl_cfg.py", "w") as f:
        # Thay '123456' bằng mật khẩu WebREPL bạn muốn
        f.write("PASS = '123456'\n")
    print("[WebREPL] Đã tạo file cấu hình mật khẩu mặc định: 123456")