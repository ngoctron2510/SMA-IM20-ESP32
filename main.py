import network
import time
import urequests
import json
import socket
import ustruct
import machine
import ntptime
import gc
from machine import Pin
from uModbusTCP import ModbusTCP

# Cấu hình GC: dọn rác thường xuyên hơn để tránh phân mảnh heap
gc.threshold(8192)  # Tự động GC khi heap còn < 8KB

# --- NHẬT KÝ HOẠT ĐỘNG (LOG RING BUFFER TỐI ƯU MEMORY RAM CHO ESP32) ---
MAX_LOG_LINES = 10
sys_logs = []

def log_info(*args):
    global sys_logs
    msg = " ".join([str(a) for a in args])
    print(msg)
    try:
        now = time.localtime()
        time_str = "{:02d}:{:02d}:{:02d}".format(now[3], now[4], now[5])
        entry = "[{}] {}".format(time_str, msg)
        sys_logs.append(entry)
        if len(sys_logs) > MAX_LOG_LINES:
            sys_logs.pop(0)
    except:
        pass

# --- KHỞI TẠO NGOẠI VI PHẦN CỨNG THỰC TẾ ---
# Cấu hình LED trạng thái tại GPIO12 (HIGH = Sáng)
led = Pin(12, Pin.OUT)
led.value(0) # Ban đầu tắt LED

# Biến điều khiển đẩy Firebase (khai báo trước để load_config dùng global)
firebase_enabled = False  # Mặc định TẮT đẩy Firebase
firebase_url_custom = ""

# --- ĐỌC/GHI CẤU HÌNH TỪ FLASH ---
def load_config():
    global firebase_enabled, firebase_url_custom
    try:
        with open('config.json', 'r') as f:
            cfg = json.load(f)
            if "firebase_enabled" in cfg:
                firebase_enabled = cfg["firebase_enabled"]
            if "firebase_url_custom" in cfg:
                firebase_url_custom = cfg["firebase_url_custom"]
            elif "firebase_url" in cfg:
                firebase_url_custom = cfg["firebase_url"]
            return cfg
    except:
        return {"im20_ip": "10.187.32.150"}

def save_config(config_data):
    global firebase_enabled, firebase_url_custom, IM20_IP
    cfg = {"im20_ip": IM20_IP, "firebase_enabled": firebase_enabled, "firebase_url_custom": firebase_url_custom}
    cfg.update(config_data)
    with open('config.json', 'w') as f:
        json.dump(cfg, f)

config = load_config()
IM20_IP = config.get("im20_ip", "10.187.32.150")
FIREBASE_API_KEY = "AIzaSyCGA2ktgEbP0vpFq1zbZ7zekGzPKrumikM"


# --- HÀM ĐỒNG BỘ THỜI GIAN (chỉ dùng 1 server time.google.com, nhẹ) ---
def sync_time():
    """Đồng bộ thời gian qua NTP. Trả về True nếu thành công."""
    global time_synced
    for retry in range(5):
        try:
            ntptime.host = "time.google.com"
            ntptime.settime()
            rtc = machine.RTC()
            utc_plus_7 = time.time() + 7 * 3600
            tm = time.localtime(utc_plus_7)
            rtc.datetime((tm[0], tm[1], tm[2], tm[6], tm[3], tm[4], tm[5], 0))
            log_info("Đồng bộ thời gian thành công (GMT+7): {:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
                tm[0], tm[1], tm[2], tm[3], tm[4], tm[5]))
            time_synced = True
            return True
        except Exception as e:
            log_info("Lỗi đồng bộ thời gian (lần {}): {}".format(retry + 1, e))
            time.sleep(2)
    log_info("Cảnh báo: Không thể đồng bộ thời gian!")
    time_synced = False
    return False

# --- YIELD CACHE (lưu giá trị yield cuối cùng để tính daily) ---
YIELD_CACHE_FILE = "yield_cache.json"

def load_yield_cache():
    """Đọc yield cache từ flash, trả về dict"""
    try:
        with open(YIELD_CACHE_FILE, 'r') as f:
            return json.load(f)
    except:
        return {"last_date": "", "total_yield_wh": 0, "inverters": {}}

def save_yield_cache(cache):
    """Ghi yield cache vào flash"""
    try:
        with open(YIELD_CACHE_FILE, 'w') as f:
            json.dump(cache, f)
    except:
        pass

# Biến toàn cục
local_data = {"system": {}, "inverters": {}}
DEVICE_IP = "0.0.0.0"

# Yield tracking
yield_cache = load_yield_cache()
daily_yield_data = None  # Sẽ được set khi tính daily yield


# --- KIỂM TRA & SỬ DỤNG ETHERNET ĐÃ KHỞI TẠO TỪ BOOT.PY ---
lan = network.LAN()
if lan.isconnected():
    log_info("Kết nối Ethernet thành công! IP Mạch:", lan.ifconfig()[0])
    DEVICE_IP = lan.ifconfig()[0]
    time_synced = sync_time()
    if not time_synced:
        log_info("Cảnh báo: Không thể đồng bộ thời gian, dữ liệu lịch sử sẽ không hoạt động!")
else:
    log_info("Cảnh báo: Chưa nhận được IP Ethernet!")
    time_synced = False
gc.collect()

# --- KHỞI TẠO WIFI ACCESS POINT (PHÁT WIFI) ---
ap = network.WLAN(network.AP_IF)
ap.active(True)
ap.config(essid="IM20 Monitor", password="lienanh123", authmode=3)
log_info("Đã phát Wi-Fi AP. Tên: IM20 Monitor | Pass: lienanh123")
log_info("IP Web UI qua Wi-Fi:", ap.ifconfig()[0])

# --- HÀM TRUYỀN FILE STATIC INDEX.HTML (Streaming theo chunk 512 bytes, tiết kiệm RAM) ---
def send_index_html(conn):
    try:
        conn.send('HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n')
        with open('index.html', 'rb') as f:
            while True:
                buf = f.read(512)
                if not buf:
                    break
                conn.send(buf)
    except Exception as e:
        print("Lỗi gửi index.html:", e)

# --- KHỞI TẠO WEB SERVER ---
server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server_socket.bind(('', 80))
server_socket.listen(1)
server_socket.settimeout(0.1) 

def handle_web_server():
    global IM20_IP, firebase_enabled, firebase_url_custom, FIREBASE_API_KEY, FIREBASE_DEFAULT_URL
    try:
        conn, addr = server_socket.accept()
        led.value(1) # Bật LED khi có người dùng truy cập web hoặc AJAX gọi dữ liệu
        request = conn.recv(384).decode('utf-8') # Giảm buffer từ 1024 xuống 384 để tiết kiệm heap
        
        if 'GET /data ' in request:
            # Bổ sung trạng thái firebase, logs và IP vào dữ liệu trả về
            data_out = dict(local_data)
            if "system" not in data_out:
                data_out["system"] = {}
            data_out["system"]["im20_ip"] = IM20_IP
            data_out["system"]["firebase_enabled"] = firebase_enabled
            data_out["system"]["firebase_url_custom"] = firebase_url_custom
            data_out["logs"] = sys_logs
            conn.send('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n')
            conn.send(json.dumps(data_out))
            del data_out
        elif 'GET /set_ip' in request:
            res_msg = {"status": "ok", "message": "Đã lưu IP thành công!"}
            try:
                query = request.split(' ')[1]
                new_ip = query.split('ip=')[1].split('&')[0]
                IM20_IP = new_ip
                save_config({"im20_ip": new_ip})
                log_info("Đã lưu IP mới cho IM20:", new_ip)
            except Exception as e:
                res_msg = {"status": "error", "message": str(e)}
            conn.send('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n')
            conn.send(json.dumps(res_msg))
            del res_msg
        elif 'GET /toggle_firebase' in request:
            firebase_enabled = not firebase_enabled
            save_config({"firebase_enabled": firebase_enabled})
            log_info("Firebase push:", "BẬT" if firebase_enabled else "TẮT")
            conn.send('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n')
            conn.send(json.dumps({"firebase_enabled": firebase_enabled}))
        elif 'GET /set_firebase_url' in request:
            res_msg = {"status": "ok", "message": "Đã lưu Firebase URL thành công!"}
            try:
                query = request.split(' ')[1]
                new_url = query.split('url=')[1].split('&')[0]
                new_url = new_url.replace('%3A', ':').replace('%2F', '/').replace('%3F', '?').replace('%3D', '=').replace('%26', '&')
                firebase_url_custom = new_url
                FIREBASE_DEFAULT_URL = new_url
                save_config({"firebase_url_custom": new_url, "firebase_url": new_url})
                log_info("Đã lưu Firebase URL mới:", new_url)
            except Exception as e:
                res_msg = {"status": "error", "message": str(e)}
            conn.send('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n')
            conn.send(json.dumps(res_msg))
            del res_msg
        else:
            send_index_html(conn)
        conn.close()
        gc.collect() # Giải phóng heap sau mỗi request web
        led.value(0) # Tắt LED sau khi xử lý xong request
    except OSError:
        pass

# --- HÀM QUÉT MODBUS (cập nhật local_data cho Web) ---
def task_modbus_scan():
    global local_data
    if not IM20_IP:
        return
    client = ModbusTCP(IM20_IP)
    im20_connected = client.connect()

    payload = {"system": {"device_ip": DEVICE_IP, "im20_status": "connected" if im20_connected else "disconnected"}}
    
    if im20_connected:
        # 1. Đọc dữ liệu tổng hệ thống từ IM20 (Unit ID = 125)
        data_total = client.read_holding_registers(125, 40195, 7)
        if data_total and len(data_total) >= 7:
            payload["system"]["voltage"] = round(data_total[0] * 0.1, 1)
            p_total = int(data_total[4] * 100)
            if p_total > 1200000 or p_total < 0 or data_total[4] in (0xFFFF, 0xFFFE, 0xFFFD):
                p_total = 0
            payload["system"]["power_total"] = p_total
            payload["system"]["frequency"] = round(data_total[6] * 0.001, 2)
        
        # 2. Đọc 16 Inverter thành phần (Unit ID: 126 đến 141)
        payload["inverters"] = {}
        for inv_id in range(126, 142):
            data_inv = client.read_holding_registers(inv_id, 40187, 13)
            if data_inv and len(data_inv) >= 13:
                if data_inv[0] == 0xFFFF or data_inv[12] == 0x8000:
                    continue
                payload["inverters"][f"inv_{inv_id}"] = {
                    "ia": round(data_inv[1] * 0.01, 2),
                    "ib": round(data_inv[2] * 0.01, 2),
                    "ic": round(data_inv[3] * 0.01, 2),
                    "va": round(data_inv[8] * 0.1, 1),
                    "vb": round(data_inv[9] * 0.1, 1),
                    "vc": round(data_inv[10] * 0.1, 1),
                    "power": int(data_inv[12] * 10)
                }
            time.sleep(0.02)

        # 3. Đọc Total Yield (Sản lượng tích lũy) theo SunSpec
        data_yield_total = client.read_holding_registers(125, 40210, 2)
        if data_yield_total and len(data_yield_total) >= 2:
            raw = (data_yield_total[0] << 16) | data_yield_total[1]
            if raw != 0xFFFFFFFF:
                payload["system"]["total_yield_wh"] = raw * 1000  # WH_SF=3

        for inv_id in range(126, 142):
            if f"inv_{inv_id}" not in payload["inverters"]:
                continue
            data_yield_inv = client.read_holding_registers(inv_id, 40210, 2)
            if data_yield_inv and len(data_yield_inv) >= 2:
                raw = (data_yield_inv[0] << 16) | data_yield_inv[1]
                if raw != 0xFFFFFFFF and raw != 0:
                    payload["inverters"][f"inv_{inv_id}"]["yield_wh"] = raw
            time.sleep(0.02)
        
        client.close()
    
    # Cập nhật local_data
    local_data = payload
    try:
        del client
    except:
        pass
    try:
        del data_total
    except:
        pass
    gc.collect()

    # Đẩy dữ liệu hiện tại lên Firebase (10 giây / lần) - chỉ đẩy nếu được bật
    base_url = firebase_url_custom if firebase_url_custom else FIREBASE_DEFAULT_URL
    api_key = FIREBASE_API_KEY
    if firebase_enabled and base_url and api_key:
        try:
            gc.collect()
            led.value(1)
            push_url = base_url.rstrip('/') + "/solarsystem/live.json?key=" + api_key + "&print=silent"
            local_data["system"]["firebase_status"] = "connected"
            headers = {'Content-Type': 'application/json'}
            raw_payload = json.dumps(payload)
            gc.collect()
            res = urequests.put(push_url, data=raw_payload, headers=headers)
            res.close()
            del res, headers, raw_payload
            gc.collect()
            led.value(0)
        except Exception as e:
            log_info("Lỗi đẩy dữ liệu Firebase:", e)
            local_data["system"]["firebase_status"] = "disconnected"
            led.value(0)
    else:
        local_data["system"]["firebase_status"] = "disconnected"

# --- HÀM LƯU DỮ LIỆU LỊCH SỬ (15 PHÚT / LẦN) ---
def push_history_to_firebase():
    global local_data
    base_url = firebase_url_custom if firebase_url_custom else FIREBASE_DEFAULT_URL
    api_key = FIREBASE_API_KEY
    if not firebase_enabled or not base_url or not api_key:
        return
    if "system" not in local_data:
        return
    try:
        now = time.localtime()
        date_str = "{:04d}-{:02d}-{:02d}".format(now[0], now[1], now[2])
        time_str = "{:02d}-{:02d}-{:02d}".format(now[3], now[4], now[5])
        history_url = "{}/solarsystem/history/{}/{}.json?key={}&print=silent".format(base_url.rstrip('/'), date_str, time_str, api_key)
        
        gc.collect()
        led.value(1)
        headers = {'Content-Type': 'application/json'}
        raw_data = json.dumps(local_data)
        gc.collect()
        res = urequests.put(history_url, data=raw_data, headers=headers)
        res.close()
        del res, headers, raw_data
        gc.collect()
        log_info("Đã lưu dữ liệu lịch sử vào Firebase: {}/{}.".format(date_str, time_str))
        led.value(0)
    except Exception as e:
        log_info("Lỗi lưu dữ liệu lịch sử Firebase:", e)
        led.value(0)
        gc.collect()


# --- HÀM TÍNH SẢN LƯỢNG NGÀY & THÁNG ---
def calculate_daily_yield(current_data):
    global yield_cache, daily_yield_data, local_data
    now = time.localtime()
    today_str = "{:04d}-{:02d}-{:02d}".format(now[0], now[1], now[2])
    month_str = "{:04d}-{:02d}".format(now[0], now[1])

    sys_data = current_data.get("system", {})
    inv_data = current_data.get("inverters", {})
    curr_total = sys_data.get("total_yield_wh", 0)

    if "start_of_month" not in yield_cache:
        yield_cache["start_of_month"] = {"month": "", "total_yield_wh": 0}

    prev_month = yield_cache.get("start_of_month", {}).get("month", "")
    if month_str != prev_month:
        yield_cache["start_of_month"] = {"month": month_str, "total_yield_wh": curr_total}
        log_info("Cập nhật start_of_month: {} -> {} Wh".format(month_str, curr_total))

    start_month_total = yield_cache.get("start_of_month", {}).get("total_yield_wh", 0)
    if curr_total > 0 and start_month_total > 0 and curr_total >= start_month_total:
        month_wh = curr_total - start_month_total
        local_data["system"]["month_yield_kwh"] = round(month_wh / 1000, 2)
    else:
        local_data["system"]["month_yield_kwh"] = 0

    if curr_total == 0:
        local_data["system"]["today_yield_kwh"] = 0
        return  # Chưa có dữ liệu yield

    if today_str != yield_cache.get("last_date", "") and yield_cache.get("last_date", ""):
        prev_total = yield_cache.get("total_yield_wh", 0)
        if prev_total > 0:
            daily_wh = curr_total - prev_total
            if daily_wh >= 0:
                daily_yield_kwh = round(daily_wh / 1000, 3)
                daily_yield_data = {
                    "date": yield_cache["last_date"],
                    "total_yield_kwh": daily_yield_kwh,
                    "inverters": {}
                }
                for inv_key, inv in inv_data.items():
                    curr_ywh = inv.get("yield_wh", 0)
                    prev_ywh = yield_cache.get("inverters", {}).get(inv_key, {}).get("yield_wh", 0)
                    if curr_ywh > 0 and prev_ywh > 0:
                        daily_inv_wh = curr_ywh - prev_ywh
                        if daily_inv_wh >= 0:
                            daily_yield_data["inverters"][inv_key] = {
                                "yield_kwh": round(daily_inv_wh / 1000, 3)
                            }
                local_data["system"]["today_yield_kwh"] = daily_yield_kwh
                log_info("Daily yield tính cho {}: {} kWh".format(
                    yield_cache["last_date"], daily_yield_kwh))

    yield_cache["last_date"] = today_str
    yield_cache["total_yield_wh"] = curr_total
    if "inverters" not in yield_cache:
        yield_cache["inverters"] = {}
    for inv_key, inv in inv_data.items():
        ywh = inv.get("yield_wh", 0)
        if ywh > 0:
            if inv_key not in yield_cache["inverters"]:
                yield_cache["inverters"][inv_key] = {}
            yield_cache["inverters"][inv_key]["yield_wh"] = ywh
    save_yield_cache(yield_cache)


# --- HÀM ĐẨY DAILY YIELD LÊN FIREBASE ---
def push_daily_yield_to_firebase():
    global daily_yield_data
    base_url = firebase_url_custom if firebase_url_custom else FIREBASE_DEFAULT_URL
    api_key = FIREBASE_API_KEY
    if not firebase_enabled or not base_url or not api_key:
        return
    if daily_yield_data is None:
        return
    try:
        date = daily_yield_data["date"]
        url = "{}/solarsystem/daily_yield/{}.json?key={}&print=silent".format(base_url.rstrip('/'), date, api_key)
        gc.collect()
        led.value(1)
        headers = {'Content-Type': 'application/json'}
        raw_data = json.dumps(daily_yield_data)
        gc.collect()
        res = urequests.put(url, data=raw_data, headers=headers)
        res.close()
        del res, headers, raw_data
        gc.collect()
        log_info("Đã đẩy daily yield lên Firebase: {} -> {} kWh".format(
            date, daily_yield_data["total_yield_kwh"]))
        led.value(0)
        daily_yield_data = None  # Reset sau khi đẩy thành công
    except Exception as e:
        log_info("Lỗi đẩy daily yield lên Firebase:", e)
        led.value(0)

# --- HÀM KIỂM TRA LỆNH RESET TỪ XA TỪ FIREBASE ---
def check_remote_commands():
    base_url = firebase_url_custom if firebase_url_custom else FIREBASE_DEFAULT_URL
    api_key = FIREBASE_API_KEY
    if not firebase_enabled or not base_url or not api_key:
        return
    try:
        cmd_url = "{}/solarsystem/commands/reset.json?key={}".format(base_url.rstrip('/'), api_key)
        headers = {'Content-Type': 'application/json'}
        gc.collect()
        res = urequests.get(cmd_url, headers=headers)
        val = res.json()
        res.close()
        del res, headers
        gc.collect()

        if val is True or val == "reboot" or (isinstance(val, dict) and val.get("action") == "reboot"):
            log_info("⚠️ NHẬN LỆNH RESET TỪ XA TỪ FIREBASE! ĐANG KHỞI ĐỘNG LẠI ESP32...")
            
            clear_url = "{}/solarsystem/commands/reset.json?key={}&print=silent".format(base_url.rstrip('/'), api_key)
            headers = {'Content-Type': 'application/json'}
            res_clear = urequests.put(clear_url, data="false", headers=headers)
            res_clear.close()
            del res_clear, headers
            gc.collect()

            for _ in range(5):
                led.value(1)
                time.sleep(0.1)
                led.value(0)
                time.sleep(0.1)

            machine.reset()
    except Exception as e:
        log_info("Lỗi kiểm tra lệnh reset Firebase:", e)

# --- VÒNG LẶP CHÍNH ---
def main():
    global time_synced
    last_modbus_scan = 0
    scan_interval = 10 # Chu kỳ 10 giây quét Modbus một lần
    last_firebase_push = 0
    firebase_interval = 900 # Chu kỳ 15 phút (900 giây) đẩy Firebase một lần
    last_cmd_check = 0
    cmd_check_interval = 10 # Chu kỳ 10 giây kiểm tra lệnh reset từ xa
    last_time_check = time.time() # Tránh chạy NTP ngay khi khởi động
    
    log_info("Mạch đã sẵn sàng chạy tác vụ nền!")
    while True:
        handle_web_server()
        
        current_time = time.time()
        
        # --- KIỂM TRA ĐỒNG BỘ THỜI GIAN ĐỊNH KỲ ---
        if time_synced:
            if current_time - last_time_check >= 3600:  # 1 giờ / lần
                log_info("Kiểm tra đồng bộ thời gian định kỳ...")
                sync_time()
                last_time_check = time.time()
        else:
            if current_time - last_time_check >= 60:  # 60 giây / lần nếu mất đồng bộ
                log_info("Thử đồng bộ thời gian lại...")
                sync_time()
                last_time_check = time.time()
        
        # Kiểm tra lệnh reset từ Firebase (10s/lần)
        if firebase_enabled and current_time - last_cmd_check >= cmd_check_interval:
            check_remote_commands()
            last_cmd_check = time.time()

        # Kiểm tra chu kỳ quét Modbus (10 giây)
        if current_time - last_modbus_scan >= scan_interval:
            task_modbus_scan()
            last_modbus_scan = time.time()
            calculate_daily_yield(local_data)
        
        # Đẩy daily yield lên Firebase nếu có
        if daily_yield_data is not None:
            push_daily_yield_to_firebase()
        
        # Chỉ gửi Firebase khi đã đồng bộ thời gian
        if time_synced and current_time - last_firebase_push >= firebase_interval:
            push_history_to_firebase()
            last_firebase_push = time.time()
        
        # Dọn rác định kỳ mỗi 30 giây để tránh phân mảnh heap
        if current_time % 30 < 0.1:
            gc.collect()
            
        time.sleep(0.02)

if __name__ == "__main__":
    main()


