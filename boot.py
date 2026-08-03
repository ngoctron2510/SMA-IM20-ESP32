import network
import time
import machine
import gc
from machine import Pin

# Tự động dọn dẹp bộ nhớ RAM khi heap dưới 8KB
gc.threshold(8192)

'''
# --- KHỞI TẠO ETHERNET CHO WT32-ETH01 v1.4 (LAN8720 + RJ45) ---
# Pinout chuẩn WT32-ETH01 v1.4:
#   MDC  = GPIO23, MDIO = GPIO18
#   PHY_ADDR = 1, POWER = GPIO16, CLOCK ngoài
lan = network.LAN(mdc=machine.Pin(23), mdio=machine.Pin(18),
                  phy_type=network.PHY_LAN8720, phy_addr=1,
                  power=machine.Pin(16))
'''
# --- KHỞI TẠO ETHERNET LAN8720 DÙNG CHUNG TOÀN HỆ THỐNG ---BO xanh
lan = network.LAN(id=0, 
                  mdc=machine.Pin(18),      
                  mdio=machine.Pin(2),     
                  phy_type=network.PHY_LAN8720, 
                  phy_addr=1, 
                  power=None,               
                  ref_clk=machine.Pin(17),  
                  ref_clk_mode=Pin.OUT 
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
    
    # CHỈ IMPORT VÀ CHẠY OTA KHI ĐÃ CÓ MẠNG
    try:
        import ota
        ota.check_and_update()
    except Exception as e:
        print("[OTA] Lỗi kiểm tra OTA:", e)

    # GIẢI PHÓNG TOÀN BỘ BỘ NHỚ CỦA OTA VÀ UREQUESTS KHỎI RAM
    try:
        import sys
        if 'ota' in sys.modules:
            del sys.modules['ota']
        if 'urequests' in sys.modules:
            del sys.modules['urequests']
        del ota
        print("[OTA] Đã xóa module OTA khỏi RAM trước khi chạy main.py")
    except:
        pass
    gc.collect()
else:
    print("\n[Ethernet] Lỗi: Không thể kết nối Ethernet!")

gc.collect()
