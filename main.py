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

# --- KHỞI TẠO NGOẠI VI PHẦN CỨNG THỰC TẾ ---
# Cấu hình LED trạng thái tại GPIO12 (HIGH = Sáng)
led = Pin(12, Pin.OUT)
led.value(0) # Ban đầu tắt LED

# Biến điều khiển Firebase (khai báo trước để load_config dùng global)
firebase_enabled = False  # Mặc định TẮT đẩy Firebase
firebase_url_custom = ""
firebase_api_key = ""

# --- ĐỌC/GHI CẤU HÌNH TỪ FLASH ---
def load_config():
    global firebase_enabled, firebase_url_custom, firebase_api_key
    default_cfg = {
        "im20_ip": "172.16.32.119",
        "firebase_enabled": False,
        "firebase_url": "",
        "firebase_api_key": ""
    }
    try:
        with open('config.json', 'r') as f:
            cfg = json.load(f)
            firebase_enabled = cfg.get("firebase_enabled", False)
            firebase_url_custom = cfg.get("firebase_url", "")
            firebase_api_key = cfg.get("firebase_api_key", "")
            return cfg
    except Exception as e:
        print("[Config] Chưa có hoặc lỗi đọc config.json, dùng mặc định:", e)
        return default_cfg

def save_config(new_data=None):
    global config, firebase_enabled, firebase_url_custom, firebase_api_key
    if new_data:
        config.update(new_data)
    config["firebase_enabled"] = firebase_enabled
    config["firebase_url"] = firebase_url_custom
    config["firebase_api_key"] = firebase_api_key
    try:
        with open('config.json', 'w') as f:
            json.dump(config, f)
        print("[Config] Đã lưu cấu hình mới vào config.json")
    except Exception as e:
        print("[Config] Lỗi ghi config.json:", e)

config = load_config()
IM20_IP = config.get("im20_ip", "172.16.32.119")
FIREBASE_API_KEY = config.get("firebase_api_key", "")

# Hàm lấy URL cơ sở cho Firebase
def get_firebase_base_url():
    if firebase_url_custom and firebase_url_custom.strip():
        return firebase_url_custom.strip().rstrip('/')
    return ""

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


# --- LẤY THÔNG TIN KẾT NỐI ETHERNET TỪ BOOT.PY ---
lan = network.LAN()
if lan.isconnected():
    DEVICE_IP = lan.ifconfig()[0]
    
    # --- ĐỒNG BỘ THỜI GIAN QUA NTP (GOOGLE) & CHỈNH MÚI GIỜ GMT+7 ---
    time_synced = False
    for retry in range(5):
        try:
            ntptime.host = "time.google.com"
            ntptime.settime()
            # Chỉnh sang múi giờ GMT+7
            rtc = machine.RTC()
            utc_plus_7 = time.time() + 7 * 3600
            tm = time.localtime(utc_plus_7)
            rtc.datetime((tm[0], tm[1], tm[2], tm[6], tm[3], tm[4], tm[5], 0))
            print("Đồng bộ thời gian thành công (GMT+7):", "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(tm[0], tm[1], tm[2], tm[3], tm[4], tm[5]))
            time_synced = True
            break
        except Exception as e:
            print("Lỗi đồng bộ thời gian (lần {}): {}".format(retry + 1, e))
            time.sleep(2)
    
    if not time_synced:
        print("Cảnh báo: Không thể đồng bộ thời gian, dữ liệu lịch sử sẽ không hoạt động!")
else:
    print("Thất bại: Không nhận được IP từ DHCP!")
    time_synced = False

# --- KHỞI TẠO WIFI ACCESS POINT (PHÁT WIFI) ---
ap = network.WLAN(network.AP_IF)
ap.active(True)
ap.config(essid="IM20 Monitor", password="lienanh123", authmode=3)
print("Đã phát Wi-Fi AP. Tên: IM20 Monitor | Pass: lienanh123")
print("IP Web UI qua Wi-Fi:", ap.ifconfig()[0])

# --- GIAO DIỆN HTML WEB SERVER (cache tĩnh, giảm phân mảnh heap) ---
HTML_HEAD = """<!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>WT32-ETH01 Giám sát SMA</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background-color: #f4f4f9; }
            .container { max-width: 900px; margin: auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }
            h1, h2 { color: #333; }
            .card { background: #eef2f7; padding: 15px; margin-bottom: 15px; border-radius: 5px; }
            table { width: 100%; border-collapse: collapse; margin-top: 10px; }
            th, td { border: 1px solid #ddd; padding: 8px; text-align: center; }
            th { background-color: #007bff; color: white; }
            input[type=text] { padding: 8px; width: 200px; border: 1px solid #ccc; border-radius: 4px; }
            input[type=submit] { padding: 8px 15px; background: #28a745; color: white; border: none; border-radius: 4px; cursor: pointer; }
        </style>
        <script>
            function updateStatus(id, status) {
                let el = document.getElementById(id);
                if(status === "connected") {
                    el.innerHTML = '<span style="color:green;font-weight:bold">&#9679; Kết nối</span>';
                } else {
                    el.innerHTML = '<span style="color:red;font-weight:bold">&#9679; Mất kết nối</span>';
                }
            }
            function updateFirebaseUI(enabled, customUrl) {
                let btn = document.getElementById('fb_toggle_btn');
                let statusText = document.getElementById('fb_toggle_status');
                if(enabled) {
                    btn.innerHTML = 'TẮT đẩy Firebase';
                    btn.style.background = '#dc3545';
                    statusText.innerHTML = '<span style="color:green;font-weight:bold">&#9679; Đang bật</span>';
                } else {
                    btn.innerHTML = 'BẬT đẩy Firebase';
                    btn.style.background = '#28a745';
                    statusText.innerHTML = '<span style="color:red;font-weight:bold">&#9679; Đang tắt</span>';
                }
                if(customUrl) {
                    document.getElementById('fb_url_input').value = customUrl;
                }
            }
            function toggleFirebase() {
                fetch('/toggle_firebase').then(r => r.json()).then(data => {
                    updateFirebaseUI(data.firebase_enabled);
                });
            }
                    setInterval(function() {
                        fetch('/data').then(response => response.json()).then(data => {
                            if(data.system) {
                                if(data.system.power_total !== undefined && data.system.power_total !== null && data.system.power_total >= 0) {
                                    document.getElementById('sys_p').innerText = (data.system.power_total/1000).toFixed(2) + " kW";
                                    document.getElementById('sys_f').innerText = (data.system.frequency || 50.0).toFixed(2) + " Hz";
                                } else {
                                    document.getElementById('sys_p').innerText = "-";
                                    document.getElementById('sys_f').innerText = "-";
                                }
                                if(data.system.device_ip) {
                                    document.getElementById('device_ip').innerText = data.system.device_ip;
                                }
                                // Cập nhật sản lượng điện (lấy 3 số thập phân)
                                if(data.system.today_yield_kwh !== undefined) {
                                    document.getElementById('today_yield').innerText = data.system.today_yield_kwh.toFixed(3);
                                }
                                if(data.system.month_yield_kwh !== undefined) {
                                    document.getElementById('month_yield').innerText = data.system.month_yield_kwh.toFixed(1);
                                }
                                updateStatus('im20_status', data.system.im20_status || 'disconnected');
                                updateStatus('fb_status', data.system.firebase_status || 'disconnected');
                                // Cập nhật trạng thái nút bật/tắt Firebase
                                if(data.system.firebase_enabled !== undefined) {
                                    updateFirebaseUI(data.system.firebase_enabled, data.system.firebase_url_custom || '');
                                }
                            }
                    
                    let invTable = document.getElementById('inv_table_body');
                    invTable.innerHTML = "";
                    let invData = data.inverters || {};
                    let sortedKeys = Object.keys(invData).sort((a, b) => {
                        let numA = parseInt(a.replace('inv_', ''));
                        let numB = parseInt(b.replace('inv_', ''));
                        return numA - numB;
                    });
                    document.getElementById('inv_online_count').innerText = sortedKeys.length;
                    if(sortedKeys.length > 0) {
                        for (let key of sortedKeys) {
                            let inv = invData[key];
                            let row = `<tr>
                                <td><b>${key.toUpperCase()}</b></td>
                                <td>${inv.ia} / ${inv.ib} / ${inv.ic}</td>
                                <td>${inv.va} / ${inv.vb} / ${inv.vc}</td>
                                <td>${(inv.power/1000).toFixed(2)} kW</td>
                            </tr>`;
                            invTable.innerHTML += row;
                        }
                    } else {
                        invTable.innerHTML = '<tr><td colspan="4">Đang quét dữ liệu Modbus từ mạng LAN...</td></tr>';
                    }
                });
            }, 3000);
        </script>"""

HTML_BODY = """    </head>
    <body>
        <div class="container">
            <h1>Hệ thống Giám sát SMA Modbus TCP (WT32-ETH01)</h1>
            
            <div class="card">
                <h2>Cấu hình Hệ thống</h2>
                <form action="/set_ip" method="GET">
                    <label>IP Inverter Manager (IM20): </label>
                    <input type="text" name="ip" value=\"{IM20_IP}\">
                    <input type="submit" value="Cập nhật IP">
                </form>
                <hr style="margin:12px 0;border-color:#ddd">
                <form action="/set_firebase_url" method="GET">
                    <label>Firebase Database URL: </label>
                    <input type="text" id="fb_url_input" name="url" value=\"{FB_URL}\" placeholder="https://...firebasedatabase.app" style="width:300px">
                    <input type="submit" value="Lưu URL">
                </form>
            </div>

            <div class="card">
                <h2>Trạng thái kết nối</h2>
                <p>IP thiết bị (WT32-ETH01): <span id="device_ip" style="font-weight:bold;color:#007bff">-</span></p>
                <p>IM20 (Inverter Manager): <span id="im20_status"><span style="color:gray">&#9679; Đang chờ...</span></span></p>
                <p>Firebase: <span id="fb_status"><span style="color:gray">&#9679; Đang chờ...</span></span></p>
                <p>Đẩy dữ liệu Firebase: <span id="fb_toggle_status"><span style="color:gray">&#9679; Đang chờ...</span></span>
                   <button id="fb_toggle_btn" onclick="toggleFirebase()" style="margin-left:10px;padding:6px 14px;background:#28a745;color:white;border:none;border-radius:4px;cursor:pointer">Đang tải...</button>
                </p>
            </div>
            <div class="card">
                <h2>Thông số tổng Hệ thống (IM20)</h2>
                <p>Công suất tổng hệ thống: <span id="sys_p">-</span></p>
                <p>Tần số mạng lưới: <span id="sys_f">-</span></p>
                <p>⚡ Sản lượng hôm nay: <span id="today_yield" style="color:#e17055;font-weight:bold">--</span> kWh</p>
                <p>📊 Sản lượng tháng này: <span id="month_yield" style="color:#00b894;font-weight:bold">--</span> kWh</p>
            </div>

            <div class="card">
                <h2>Chi tiết Biến tần thành phần: <span id="inv_online_count" style="color:#007bff">0</span>/16 online</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Inverter ID</th>
                            <th>Dòng 3 Pha (A) [Ia/Ib/Ic]</th>
                            <th>Áp Pha (V) [Ua/Ub/Uc]</th>
                            <th>Công suất (kW)</th>
                        </tr>
                    </thead>
                    <tbody id="inv_table_body">
                        <tr><td colspan="4">Đang quét dữ liệu Modbus từ mạng LAN...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </body>
    </html>"""

def get_html_page():
    return HTML_HEAD + HTML_BODY.format(IM20_IP=IM20_IP, FB_URL=firebase_url_custom)

# --- KHỞI TẠO WEB SERVER ---
server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server_socket.bind(('', 80))
server_socket.listen(2)
server_socket.settimeout(0.1) 

def handle_web_server():
    global IM20_IP, firebase_enabled, firebase_url_custom
    try:
        conn, addr = server_socket.accept()
        led.value(1) # Bật LED khi có người dùng truy cập web
        request = conn.recv(384).decode('utf-8')
        
        if 'GET /data ' in request:
            data_out = dict(local_data)
            if "system" not in data_out:
                data_out["system"] = {}
            data_out["system"]["firebase_enabled"] = firebase_enabled
            data_out["system"]["firebase_url_custom"] = firebase_url_custom
            conn.send('HTTP/1.1 200 OK\nContent-Type: application/json\n\n')
            conn.send(json.dumps(data_out))
        elif 'GET /set_ip' in request:
            try:
                query = request.split(' ')[1]
                new_ip = query.split('ip=')[1].split('&')[0]
                IM20_IP = new_ip
                save_config({"im20_ip": new_ip})
                print("Đã lưu IP mới cho IM20:", new_ip)
            except:
                pass
            conn.send('HTTP/1.1 303 See Other\nLocation: /\n\n')
        elif 'GET /toggle_firebase' in request:
            firebase_enabled = not firebase_enabled
            save_config()
            print("Firebase push:", "BẬT" if firebase_enabled else "TẮT")
            conn.send('HTTP/1.1 200 OK\nContent-Type: application/json\n\n')
            conn.send(json.dumps({"firebase_enabled": firebase_enabled}))
        elif 'GET /set_firebase_url' in request:
            try:
                query = request.split(' ')[1]
                new_url = query.split('url=')[1].split('&')[0]
                new_url = new_url.replace('%3A', ':').replace('%2F', '/').replace('%3F', '?').replace('%3D', '=').replace('%26', '&')
                firebase_url_custom = new_url
                save_config()
                print("Đã lưu Firebase URL mới:", new_url)
            except:
                pass
            conn.send('HTTP/1.1 303 See Other\nLocation: /\n\n')
        else:
            conn.send('HTTP/1.1 200 OK\nContent-Type: text/html\n\n')
            conn.send(get_html_page())
        conn.close()
        gc.collect()
        led.value(0)
    except OSError:
        pass

# --- HÀM QUÉT MODBUS ---
def task_modbus_scan():
    global local_data
    client = ModbusTCP(IM20_IP)
    im20_connected = client.connect()

    payload = {"system": {"device_ip": DEVICE_IP, "im20_status": "connected" if im20_connected else "disconnected"}}
    
    if im20_connected:
        # 1. Đọc dữ liệu tổng hệ thống từ IM20 (Unit ID = 125)
        data_total = client.read_holding_registers(125, 40195, 7)
        if data_total and len(data_total) >= 7:
            payload["system"]["voltage"] = round(data_total[0] * 0.1, 1)
            p_total = int(data_total[4] * 100)
            if p_total > 1200000 or p_total < 0 or data_total[4] in (0xFFFF, 0xFFFE):
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

        # 3. Đọc Total Yield (Sản lượng tích lũy) theo SunSpec (Unit ID 125 đã có hệ 1000 - đơn vị kWh)
        data_yield_total = client.read_holding_registers(125, 40209, 2)
        if data_yield_total and len(data_yield_total) >= 2:
            raw = (data_yield_total[0] << 16) | data_yield_total[1]
            if raw != 0x80000000:  # NaN check
                payload["system"]["total_yield_wh"] = raw

        for inv_id in range(126, 142):
            if f"inv_{inv_id}" not in payload["inverters"]:
                continue
            try:
                data_yield_inv = client.read_holding_registers(inv_id, 40209, 2)
                if data_yield_inv and len(data_yield_inv) >= 2:
                    raw = (data_yield_inv[0] << 16) | data_yield_inv[1]
                    if raw != 0x80000000 and raw != 0:
                        payload["inverters"][f"inv_{inv_id}"]["yield_wh"] = raw
            except:
                pass
            time.sleep(0.02)

        # Nếu power_total của IM20 bị 0 hoặc không đọc được, tính tổng từ các inverter thành phần
        if payload["system"].get("power_total", 0) == 0 and payload.get("inverters"):
            inv_p_sum = sum(inv.get("power", 0) for inv in payload["inverters"].values() if inv.get("power", 0) > 0)
            if inv_p_sum <= 1200000:
                payload["system"]["power_total"] = inv_p_sum

        # Bổ sung tính trung bình điện áp & tần số nếu chưa có
        if (payload["system"].get("voltage", 0) == 0 or payload["system"].get("frequency", 0) == 0) and payload.get("inverters"):
            valid_v = [inv["va"] for inv in payload["inverters"].values() if inv.get("va", 0) > 0]
            if valid_v:
                payload["system"]["voltage"] = round(sum(valid_v) / len(valid_v), 1)
            if payload["system"].get("frequency", 0) == 0:
                payload["system"]["frequency"] = 50.0
        
        client.close()
    
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
    base_url = get_firebase_base_url()
    if firebase_enabled and base_url and FIREBASE_API_KEY:
        try:
            gc.collect()
            led.value(1)
            push_url = base_url + "/solarsystem/live.json?key=" + FIREBASE_API_KEY
            local_data["system"]["firebase_status"] = "connected"
            headers = {'Content-Type': 'application/json'}
            res = urequests.put(push_url, data=json.dumps(payload), headers=headers)
            res.close()
            del res, headers
            gc.collect()
            led.value(0)
        except Exception as e:
            print("Lỗi đẩy dữ liệu Firebase:", e)
            local_data["system"]["firebase_status"] = "disconnected"
            led.value(0)
    else:
        local_data["system"]["firebase_status"] = "disconnected"

# --- HÀM LƯU DỮ LIỆU LỊCH SỬ (15 PHÚT / LẦN) ---
def push_history_to_firebase():
    global local_data
    base_url = get_firebase_base_url()
    if not firebase_enabled or not base_url or not FIREBASE_API_KEY:
        return
    if "system" not in local_data:
        return
    try:
        now = time.localtime()
        date_str = "{:04d}-{:02d}-{:02d}".format(now[0], now[1], now[2])
        time_str = "{:02d}-{:02d}-{:02d}".format(now[3], now[4], now[5])
        history_url = "{}/solarsystem/history/{}/{}.json?key={}".format(base_url, date_str, time_str, FIREBASE_API_KEY)
        
        gc.collect()
        led.value(1)
        headers = {'Content-Type': 'application/json'}
        res = urequests.put(history_url, data=json.dumps(local_data), headers=headers)
        res.close()
        del res, headers
        gc.collect()
        print("Đã lưu dữ liệu lịch sử vào Firebase: {}/{}.".format(date_str, time_str))
        led.value(0)
    except Exception as e:
        print("Lỗi lưu dữ liệu lịch sử Firebase:", e)
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
    curr_total = sys_data.get("total_yield_wh", 0)  # Lưu ý: ID 125 sản lượng tổng đọc về đã là kWh (hệ 1000)

    # 1. Tính Sản lượng Tháng (month_yield_kwh) - Không chia 1000 nữa!
    if "start_of_month" not in yield_cache:
        yield_cache["start_of_month"] = {"month": "", "total_yield_wh": 0}

    prev_month = yield_cache.get("start_of_month", {}).get("month", "")
    if month_str != prev_month or yield_cache["start_of_month"].get("total_yield_wh", 0) == 0:
        yield_cache["start_of_month"] = {"month": month_str, "total_yield_wh": curr_total}
        print("Cập nhật start_of_month: {} -> {} kWh".format(month_str, curr_total))

    start_month_total = yield_cache.get("start_of_month", {}).get("total_yield_wh", 0)
    if curr_total > 0 and start_month_total > 0 and curr_total >= start_month_total:
        month_kwh = curr_total - start_month_total  # Đã là kWh (không chia 1000)
        local_data["system"]["month_yield_kwh"] = round(month_kwh, 2)
    else:
        local_data["system"]["month_yield_kwh"] = 0.0

    if curr_total == 0:
        local_data["system"]["today_yield_kwh"] = 0.0
        return

    # 2. Tính Sản lượng Hôm nay (today_yield_kwh live - lấy 3 số thập phân, không chia 1000)
    if "start_of_day" not in yield_cache:
        yield_cache["start_of_day"] = {"date": "", "total_yield_wh": 0}

    start_day_date = yield_cache.get("start_of_day", {}).get("date", "")
    start_day_total = yield_cache.get("start_of_day", {}).get("total_yield_wh", 0)

    if today_str != start_day_date or start_day_total == 0:
        yield_cache["start_of_day"] = {"date": today_str, "total_yield_wh": curr_total}
        start_day_total = curr_total

    if curr_total >= start_day_total and start_day_total > 0:
        today_kwh = curr_total - start_day_total  # Đã là kWh (ID 125 không chia 1000)
        local_data["system"]["today_yield_kwh"] = round(today_kwh, 3)  # Lấy 3 số thập phân
    else:
        local_data["system"]["today_yield_kwh"] = 0.0

    # 3. Khi sang ngày mới: tính sản lượng ngày hôm qua để đẩy lên Firebase
    if today_str != yield_cache.get("last_date", "") and yield_cache.get("last_date", ""):
        prev_total = yield_cache.get("total_yield_wh", 0)
        if prev_total > 0:
            daily_kwh = curr_total - prev_total  # Đã là kWh, KHÔNG chia 1000!
            if daily_kwh >= 0:
                daily_yield_kwh = round(daily_kwh, 3)  # Lấy 3 số thập phân đơn vị kWh
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
                            # Inverter (ID 126-141) yield_wh đọc về là Wh -> CẦN chia 1000 để ra kWh
                            daily_yield_data["inverters"][inv_key] = {
                                "yield_kwh": round(daily_inv_wh / 1000, 2)
                            }
                print("Daily yield tính cho {}: {} kWh".format(yield_cache["last_date"], daily_yield_kwh))

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
    base_url = get_firebase_base_url()
    if not firebase_enabled or not base_url or not FIREBASE_API_KEY:
        return
    if daily_yield_data is None:
        return
    try:
        date = daily_yield_data["date"]
        url = "{}/solarsystem/daily_yield/{}.json?key={}".format(base_url, date, FIREBASE_API_KEY)
        gc.collect()
        led.value(1)
        headers = {'Content-Type': 'application/json'}
        res = urequests.put(url, data=json.dumps(daily_yield_data), headers=headers)
        res.close()
        del res, headers
        gc.collect()
        print("Đã đẩy daily yield lên Firebase: {} -> {} kWh".format(date, daily_yield_data["total_yield_kwh"]))
        led.value(0)
        daily_yield_data = None
    except Exception as e:
        print("Lỗi đẩy daily yield lên Firebase:", e)
        led.value(0)

# --- VÒNG LẶP CHÍNH ---
def main():
    global time_synced
    last_modbus_scan = 0
    scan_interval = 10
    last_firebase_push = 0
    firebase_interval = 900
    
    print("Mạch đã sẵn sàng chạy tác vụ nền!")
    while True:
        handle_web_server()
        
        current_time = time.time()
        
        if current_time - last_modbus_scan >= scan_interval:
            task_modbus_scan()
            last_modbus_scan = time.time()
            calculate_daily_yield(local_data)
        
        if daily_yield_data is not None:
            push_daily_yield_to_firebase()
        
        if time_synced and current_time - last_firebase_push >= firebase_interval:
            push_history_to_firebase()
            last_firebase_push = time.time()
        
        if current_time % 30 < 0.1:
            gc.collect()
            
        time.sleep(0.02)

if __name__ == "__main__":
    main()
