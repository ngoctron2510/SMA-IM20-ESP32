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
try:
    from uModbusTCP import ModbusTCP, set_log_callback
except ImportError:
    from uModbusTCP import ModbusTCP
    set_log_callback = None

# Cấu hình GC: dọn rác thường xuyên hơn để tránh phân mảnh heap
gc.threshold(8192)  # Tự động GC khi heap còn < 8KB

# --- NHẬT KÝ HOẠT ĐỘNG (LOG RING BUFFER TỐI ƯU MEMORY RAM CHO ESP32) ---
MAX_LOG_LINES = 20
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

if set_log_callback:
    try:
        set_log_callback(log_info)
    except:
        pass

# Biến điều khiển Firebase & Phần cứng
firebase_enabled = False  # Mặc định TẮT đẩy Firebase
firebase_url_custom = ""
firebase_api_key = ""
led_pin_num = 12  # Mặc định GPIO 12

# --- ĐỌC/GHI CẤU HÌNH TỪ FLASH ---
def load_config():
    global firebase_enabled, firebase_url_custom, firebase_api_key, led_pin_num
    default_cfg = {
        "im20_ip": "172.16.32.119",
        "firebase_enabled": False,
        "firebase_url": "",
        "firebase_api_key": "",
        "led_pin": 12
    }
    try:
        with open('config.json', 'r') as f:
            cfg = json.load(f)
            firebase_enabled = cfg.get("firebase_enabled", False)
            firebase_url_custom = cfg.get("firebase_url", "")
            firebase_api_key = cfg.get("firebase_api_key", "")
            led_pin_num = cfg.get("led_pin", 12)
            return cfg
    except Exception as e:
        log_info("[Config] Chưa có hoặc lỗi đọc config.json, dùng mặc định:", e)
        return default_cfg

def save_config(new_data=None):
    global config, firebase_enabled, firebase_url_custom, firebase_api_key, led_pin_num
    if new_data:
        config.update(new_data)
    config["firebase_enabled"] = firebase_enabled
    config["firebase_url"] = firebase_url_custom
    config["firebase_api_key"] = firebase_api_key
    config["led_pin"] = led_pin_num
    try:
        with open('config.json', 'w') as f:
            json.dump(config, f)
        log_info("[Config] Đã lưu cấu hình mới vào config.json")
    except Exception as e:
        log_info("[Config] Lỗi ghi config.json:", e)

config = load_config()
IM20_IP = config.get("im20_ip", "172.16.32.119")
FIREBASE_API_KEY = config.get("firebase_api_key", "")
led_pin_num = config.get("led_pin", 12)

# --- KHỞI TẠO NGOẠI VI PHẦN CỨNG THỰC TẾ ---
led = Pin(led_pin_num, Pin.OUT)
led.value(0) # Ban đầu tắt LED

def set_led_pin(new_pin):
    global led, led_pin_num
    try:
        pin_val = int(new_pin)
        if pin_val in (12, 22):
            if led:
                try:
                    led.value(0)
                except:
                    pass
            led_pin_num = pin_val
            led = Pin(led_pin_num, Pin.OUT)
            led.value(0)
            save_config({"led_pin": led_pin_num})
            log_info("Đã chuyển chân LED trạng thái sang GPIO", led_pin_num)
    except Exception as e:
        log_info("Lỗi chuyển chân LED:", e)

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


# --- ĐỒNG BỘ THỜI GIAN QUA NTP (ĐỊNH KỲ & NHIỀU SERVER DỰ PHÒNG) ---
time_synced = False
last_ntp_sync = 0

def sync_ntp_time():
    global time_synced, last_ntp_sync
    ntp_servers = ["time.google.com", "pool.ntp.org", "asia.pool.ntp.org", "time.windows.com"]
    for server in ntp_servers:
        try:
            ntptime.host = server
            ntptime.settime()
            # Chỉnh sang múi giờ GMT+7 Việt Nam
            rtc = machine.RTC()
            utc_plus_7 = time.time() + 7 * 3600
            tm = time.localtime(utc_plus_7)
            rtc.datetime((tm[0], tm[1], tm[2], tm[6], tm[3], tm[4], tm[5], 0))
            log_info("Đồng bộ NTP thành công ({}) GMT+7: {:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(server, tm[0], tm[1], tm[2], tm[3], tm[4], tm[5]))
            time_synced = True
            last_ntp_sync = time.time()
            return True
        except Exception as e:
            log_info("Lỗi đồng bộ NTP từ {}: {}".format(server, e))
    time_synced = False
    return False

# --- LẤY THÔNG TIN KẾT NỐI ETHERNET TỪ BOOT.PY ---
lan = network.LAN()
if lan.isconnected():
    DEVICE_IP = lan.ifconfig()[0]
    log_info("Đã nhận IP Ethernet:", DEVICE_IP)
    sync_ntp_time()
else:
    log_info("Thất bại: Chưa nhận được IP từ DHCP!")
    time_synced = False

# --- KHỞI TẠO WIFI ACCESS POINT (PHÁT WIFI) ---
ap = network.WLAN(network.AP_IF)
ap.active(True)
ap.config(essid="IM20 Monitor", password="lienanh123", authmode=3)
log_info("Đã phát Wi-Fi AP. Tên: IM20 Monitor | Pass: lienanh123")
log_info("IP Web UI qua Wi-Fi:", ap.ifconfig()[0])
log_info("Phien ban v1.1 18/07/2026 18h00")

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
                            
                            // Cập nhật Nhật ký Terminal Log
                            if(data.logs && Array.isArray(data.logs)) {
                                let logBox = document.getElementById('sys_log_box');
                                if(logBox) {
                                    let shouldScroll = (logBox.scrollHeight - logBox.clientHeight <= logBox.scrollTop + 30);
                                    logBox.innerText = data.logs.join(String.fromCharCode(10));
                                    if(shouldScroll) {
                                        logBox.scrollTop = logBox.scrollHeight;
                                    }
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
                }).catch(err => console.log('Fetch error:', err));
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
                    <input type="text" name="ip" value="{IM20_IP}">
                    <input type="submit" value="Cập nhật IP">
                </form>
                <hr style="margin:12px 0;border-color:#ddd">
                <form action="/set_firebase_url" method="GET">
                    <label>Firebase Database URL: </label>
                    <input type="text" id="fb_url_input" name="url" value="{FB_URL}" placeholder="https://...firebasedatabase.app" style="width:300px">
                    <input type="submit" value="Lưu URL">
                </form>
                <hr style="margin:12px 0;border-color:#ddd">
                <form action="/set_led_pin" method="GET">
                    <label>Chân LED Trạng thái (GPIO): </label>
                    <select name="pin" style="padding:6px;border-radius:4px;border:1px solid #ccc;margin-right:8px">
                        <option value="12" {LED_12_SEL}>GPIO 12 (Phiên bản v1)</option>
                        <option value="22" {LED_22_SEL}>GPIO 22 (Phiên bản v2)</option>
                    </select>
                    <input type="submit" value="Lưu Chân LED">
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

            <div class="card" style="background:#1e272e;color:#55efc4">
                <h2 style="color:#00d2d3;margin-top:0">📜 Nhật ký Hệ thống (Terminal Log)</h2>
                <pre id="sys_log_box" style="height:160px;overflow-y:auto;background:#0d1117;color:#7ee787;padding:10px;border-radius:4px;font-family:'Consolas','Courier New',monospace;font-size:12px;margin:0;white-space:pre-wrap;word-break:break-all">Đang tải nhật ký...</pre>
            </div>
        </div>
    </body>
    </html>"""

def send_html_page(conn):
    led_12_sel = "selected" if led_pin_num == 12 else ""
    led_22_sel = "selected" if led_pin_num == 22 else ""
    body_str = HTML_BODY.format(
        IM20_IP=IM20_IP,
        FB_URL=firebase_url_custom,
        LED_12_SEL=led_12_sel,
        LED_22_SEL=led_22_sel
    )
    head_bytes = HTML_HEAD.encode('utf-8')
    body_bytes = body_str.encode('utf-8')
    total_len = len(head_bytes) + len(body_bytes)
    
    header = 'HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n'.format(total_len)
    conn.sendall(header.encode('utf-8'))
    
    chunk_size = 512
    for i in range(0, len(head_bytes), chunk_size):
        conn.sendall(head_bytes[i:i+chunk_size])
        time.sleep(0.002)
    for i in range(0, len(body_bytes), chunk_size):
        conn.sendall(body_bytes[i:i+chunk_size])
        time.sleep(0.002)
    del head_bytes, body_bytes, body_str
    gc.collect()

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
    except OSError:
        return

    try:
        conn.settimeout(1.0) # Cho phép chờ tối đa 1s để nhận trọn vẹn HTTP Request từ browser
        led.value(1)
        raw_req = conn.recv(1024)
        if not raw_req:
            return
        
        request = raw_req.decode('utf-8', 'ignore')
        
        if 'GET /data' in request:
            data_out = dict(local_data)
            if "system" not in data_out:
                data_out["system"] = {}
            data_out["system"]["firebase_enabled"] = firebase_enabled
            data_out["system"]["firebase_url_custom"] = firebase_url_custom
            data_out["system"]["led_pin"] = led_pin_num
            data_out["logs"] = sys_logs
            json_payload = json.dumps(data_out)
            json_bytes = json_payload.encode('utf-8')
            header = 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n'.format(len(json_bytes))
            conn.sendall(header.encode('utf-8'))
            chunk_size = 512
            for i in range(0, len(json_bytes), chunk_size):
                conn.sendall(json_bytes[i:i+chunk_size])
                time.sleep(0.002)
            del data_out, json_payload, json_bytes
            gc.collect()
        elif 'GET /set_ip' in request:
            try:
                query = request.split(' ')[1]
                new_ip = query.split('ip=')[1].split('&')[0]
                IM20_IP = new_ip
                save_config({"im20_ip": new_ip})
                log_info("Đã lưu IP mới cho IM20:", new_ip)
            except:
                pass
            conn.sendall(b'HTTP/1.1 303 See Other\r\nLocation: /\r\nConnection: close\r\n\r\n')
        elif 'GET /set_led_pin' in request:
            try:
                query = request.split(' ')[1]
                new_pin = query.split('pin=')[1].split('&')[0]
                set_led_pin(new_pin)
            except:
                pass
            conn.sendall(b'HTTP/1.1 303 See Other\r\nLocation: /\r\nConnection: close\r\n\r\n')
        elif 'GET /toggle_firebase' in request:
            firebase_enabled = not firebase_enabled
            save_config()
            log_info("Firebase push:", "BẬT" if firebase_enabled else "TẮT")
            res_body = json.dumps({"firebase_enabled": firebase_enabled})
            res_bytes = res_body.encode('utf-8')
            header = 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n'.format(len(res_bytes))
            conn.sendall(header.encode('utf-8'))
            conn.sendall(res_bytes)
            del res_body, res_bytes
            gc.collect()
        elif 'GET /set_firebase_url' in request:
            try:
                query = request.split(' ')[1]
                new_url = query.split('url=')[1].split('&')[0]
                new_url = new_url.replace('%3A', ':').replace('%2F', '/').replace('%3F', '?').replace('%3D', '=').replace('%26', '&')
                firebase_url_custom = new_url
                save_config()
                log_info("Đã lưu Firebase URL mới:", new_url)
            except:
                pass
            conn.sendall(b'HTTP/1.1 303 See Other\r\nLocation: /\r\nConnection: close\r\n\r\n')
        else:
            send_html_page(conn)
    except Exception as e:
        pass
    finally:
        try:
            time.sleep(0.02) # Chờ LWIP đẩy xong hết dữ liệu TCP trước khi đóng socket
            conn.close()
        except:
            pass
        led.value(0)
        gc.collect()

# --- HÀM AN TOÀN ĐẨY DỮ LIỆU FIREBASE (TỐI ƯU BỘ NHỚ RAM HEAP CHỐNG ENOMEM) ---
def safe_firebase_put(url, data_dict):
    res = None
    try:
        gc.collect()
        led.value(1)
        payload = json.dumps(data_dict)
        headers = {'Content-Type': 'application/json'}
        res = urequests.put(url, data=payload, headers=headers)
        status = res.status_code
        res.close()
        del res, payload, headers
        gc.collect()
        led.value(0)
        return (status == 200 or status == 204)
    except Exception as e:
        if res:
            try:
                res.close()
            except:
                pass
            del res
        led.value(0)
        gc.collect()
        log_info("Lỗi đẩy dữ liệu Firebase:", e)
        return False

# --- HÀM QUÉT MODBUS (ĐỌC GỘP THANH GHI TỐI ƯU BĂNG THÔNG) ---
def task_modbus_scan():
    global local_data
    gc.collect()
    client = ModbusTCP(IM20_IP)
    im20_connected = client.connect()

    if "system" not in local_data:
        local_data["system"] = {}
    if "inverters" not in local_data:
        local_data["inverters"] = {}

    local_data["system"]["device_ip"] = DEVICE_IP
    local_data["system"]["im20_status"] = "connected" if im20_connected else "disconnected"
    
    if im20_connected:
        # 1. Đọc dữ liệu tổng & Total Yield hệ thống từ IM20 (Unit ID = 125, đọc 16 thanh ghi từ 40195 đến 40210)
        handle_web_server()
        data_total = client.read_holding_registers(125, 40195, 16)
        if data_total and len(data_total) >= 16:
            local_data["system"]["voltage"] = round(data_total[0] * 0.1, 1)
            p_total = int(data_total[4] * 100)
            if p_total > 1200000 or p_total < 0 or data_total[4] in (0xFFFF, 0xFFFE):
                p_total = 0
            local_data["system"]["power_total"] = p_total
            local_data["system"]["frequency"] = round(data_total[6] * 0.001, 2)
            
            # Total Yield (Sản lượng tích lũy kWh tại thanh ghi 40209-40210 -> offset 14-15)
            raw_yield = (data_total[14] << 16) | data_total[15]
            if raw_yield != 0x80000000 and raw_yield != 0:
                local_data["system"]["total_yield_wh"] = raw_yield
        elif data_total and len(data_total) >= 7:
            local_data["system"]["voltage"] = round(data_total[0] * 0.1, 1)
            p_total = int(data_total[4] * 100)
            if p_total > 1200000 or p_total < 0 or data_total[4] in (0xFFFF, 0xFFFE):
                p_total = 0
            local_data["system"]["power_total"] = p_total
            local_data["system"]["frequency"] = round(data_total[6] * 0.001, 2)
        
        # 2. Đọc 16 Inverter thành phần (Unit ID: 126 đến 141) - Đọc gộp 24 thanh ghi (40187 đến 40210)
        for inv_id in range(126, 142):
            handle_web_server()
            inv_key = "inv_{}".format(inv_id)
            data_inv = client.read_holding_registers(inv_id, 40187, 24)
            if data_inv and len(data_inv) >= 13 and data_inv[0] != 0xFFFF and data_inv[12] != 0x8000:
                inv_info = {
                    "ia": round(data_inv[1] * 0.01, 2),
                    "ib": round(data_inv[2] * 0.01, 2),
                    "ic": round(data_inv[3] * 0.01, 2),
                    "va": round(data_inv[8] * 0.1, 1),
                    "vb": round(data_inv[9] * 0.1, 1),
                    "vc": round(data_inv[10] * 0.1, 1),
                    "power": int(data_inv[12] * 10)
                }
                # Lấy yield_wh từ thanh ghi 40209-40210 (offset 22-23 trong mảng 24 thanh ghi)
                if len(data_inv) >= 24:
                    raw_y = (data_inv[22] << 16) | data_inv[23]
                    if raw_y != 0x80000000 and raw_y != 0:
                        inv_info["yield_wh"] = raw_y
                
                local_data["inverters"][inv_key] = inv_info
            else:
                # Xóa inverter khỏi danh sách nếu bị ngắt kết nối (offline)
                local_data["inverters"].pop(inv_key, None)
            time.sleep(0.01)

        # Nếu power_total của IM20 bị 0 hoặc không đọc được, tính tổng từ các inverter thành phần
        if local_data["system"].get("power_total", 0) == 0 and local_data.get("inverters"):
            inv_p_sum = sum(inv.get("power", 0) for inv in local_data["inverters"].values() if inv.get("power", 0) > 0)
            if inv_p_sum <= 1200000:
                local_data["system"]["power_total"] = inv_p_sum

        # Bổ sung tính trung bình điện áp & tần số nếu chưa có
        if (local_data["system"].get("voltage", 0) == 0 or local_data["system"].get("frequency", 0) == 0) and local_data.get("inverters"):
            valid_v = [inv["va"] for inv in local_data["inverters"].values() if inv.get("va", 0) > 0]
            if valid_v:
                local_data["system"]["voltage"] = round(sum(valid_v) / len(valid_v), 1)
            if local_data["system"].get("frequency", 0) == 0:
                local_data["system"]["frequency"] = 50.0
        
        client.close()
    else:
        # Nếu mất kết nối hoàn toàn với IM20, xóa danh sách inverter và reset công suất về 0
        local_data["inverters"].clear()
        local_data["system"]["power_total"] = 0
        local_data["system"]["voltage"] = 0
        local_data["system"]["frequency"] = 0
    
    try:
        client.close()
        del client
    except:
        pass
    gc.collect()

    # Đẩy dữ liệu hiện tại lên Firebase (10 giây / lần) - chỉ đẩy nếu được bật
    base_url = get_firebase_base_url()
    if firebase_enabled and base_url and FIREBASE_API_KEY:
        push_url = base_url + "/solarsystem/live.json?key=" + FIREBASE_API_KEY
        if safe_firebase_put(push_url, local_data):
            local_data["system"]["firebase_status"] = "connected"
        else:
            local_data["system"]["firebase_status"] = "disconnected"
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
        
        if safe_firebase_put(history_url, local_data):
            log_info("Đã lưu dữ liệu lịch sử vào Firebase: {}/{}.".format(date_str, time_str))
    except Exception as e:
        log_info("Lỗi lưu dữ liệu lịch sử Firebase:", e)
        gc.collect()

# --- HÀM KIỂM TRA LỆNH RESET TỪ XA TỪ FIREBASE ---
def check_remote_commands():
    base_url = get_firebase_base_url()
    if not firebase_enabled or not base_url or not FIREBASE_API_KEY:
        return
    res = None
    try:
        gc.collect()
        cmd_url = "{}/solarsystem/commands/reset.json?key={}".format(base_url, FIREBASE_API_KEY)
        headers = {'Content-Type': 'application/json'}
        res = urequests.get(cmd_url, headers=headers)
        val = res.json()
        res.close()
        del res, headers
        gc.collect()

        if val is True or val == "reboot" or (isinstance(val, dict) and val.get("action") == "reboot"):
            log_info("⚠️ NHẬN LỆNH RESET TỪ XA TỪ FIREBASE! ĐANG KHỞI ĐỘNG LẠI ESP32...")
            
            # Xóa lệnh reset trên Firebase để tránh lặp vô tận
            clear_url = "{}/solarsystem/commands/reset.json?key={}".format(base_url, FIREBASE_API_KEY)
            safe_firebase_put(clear_url, False)

            # Nháy LED báo hiệu 5 lần trước khi reset
            for _ in range(5):
                led.value(1)
                time.sleep(0.1)
                led.value(0)
                time.sleep(0.1)

            machine.reset()
    except Exception as e:
        if res:
            try: res.close()
            except: pass
            del res
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
        log_info("Cập nhật start_of_month: {} -> {} kWh".format(month_str, curr_total))

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
                log_info("Daily yield tính cho {}: {} kWh".format(yield_cache["last_date"], daily_yield_kwh))

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
        if safe_firebase_put(url, daily_yield_data):
            log_info("Đã đẩy daily yield lên Firebase: {} -> {} kWh".format(date, daily_yield_data["total_yield_kwh"]))
            daily_yield_data = None
    except Exception as e:
        log_info("Lỗi đẩy daily yield lên Firebase:", e)
        gc.collect()

# --- VÒNG LẶP CHÍNH ---
def main():
    global time_synced, last_ntp_sync
    last_modbus_scan = 0
    scan_interval = 10
    last_firebase_push = 0
    firebase_interval = 900
    last_cmd_check = 0
    cmd_check_interval = 10
    
    log_info("Mạch đã sẵn sàng chạy tác vụ nền!")
    while True:
        handle_web_server()
        
        current_time = time.time()
        
        # 1. Tự động đồng bộ thời gian NTP định kỳ (mỗi 1 giờ) hoặc mỗi 30s nếu chưa đồng bộ thành công
        if not time_synced:
            if current_time - last_ntp_sync >= 30 or last_ntp_sync == 0:
                sync_ntp_time()
        elif current_time - last_ntp_sync >= 3600:  # Chống lệch giờ RTC: Đồng bộ lại mỗi 1 giờ
            sync_ntp_time()
        
        # 2. Kiểm tra lệnh điều khiển từ xa (Reset ESP32) từ Firebase (10s/lần)
        if current_time - last_cmd_check >= cmd_check_interval:
            check_remote_commands()
            last_cmd_check = time.time()

        if current_time - last_modbus_scan >= scan_interval:
            task_modbus_scan()
            last_modbus_scan = time.time()
            calculate_daily_yield(local_data)
        
        if daily_yield_data is not None:
            push_daily_yield_to_firebase()
        
        if current_time - last_firebase_push >= firebase_interval:
            if not time_synced:
                sync_ntp_time()
            if time_synced:
                push_history_to_firebase()
                last_firebase_push = time.time()
        
        if current_time % 30 < 0.1:
            gc.collect()
            
        time.sleep(0.02)

if __name__ == "__main__":
    main()
