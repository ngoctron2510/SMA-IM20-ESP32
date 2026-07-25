import socket
import ustruct

class ModbusTCP:
    def __init__(self, ip, port=502):
        self.ip = ip
        self.port = port
        self.socket = None
        self.tx_id = 0

    def connect(self):
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.settimeout(5.0)
            self.socket.connect((self.ip, self.port))
            return True
        except Exception as e:
            print("Lỗi kết nối Modbus TCP:", e)
            return False

    def read_holding_registers(self, unit_id, reg_addr, reg_count):
        self.tx_id += 1
        # MBAP Header: Transaction ID (2B), Protocol ID (2B=0), Length (2B=6), Unit ID (1B)
        # PDU: Function Code (1B=3), Starting Address (2B), Quantity of Regs (2B)
        req = ustruct.pack(">HHHBBHH", self.tx_id, 0, 6, unit_id, 3, reg_addr, reg_count)
        
        try:
            self.socket.send(req)
            resp = self.socket.recv(1024)
            if len(resp) >= 9 and resp[7] == 3: # Đảm bảo đúng Function Code
                byte_count = resp[8]
                data = resp[9:9+byte_count]
                # Chuyển đổi mảng byte Big-Endian sang danh sách số nguyên 16-bit
                return list(ustruct.unpack(">" + "H" * (byte_count // 2), data))
            else:
                print(f"Phản hồi không hợp lệ từ Unit {unit_id}")
                return None
        except Exception as e:
            print(f"Lỗi đọc Unit {unit_id}:", e)
            return None

    def close(self):
        if self.socket:
            self.socket.close()