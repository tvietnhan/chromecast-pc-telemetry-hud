import http.server
import socketserver
import json
import os
import sys
import subprocess
import time
import threading
import socket
import psutil

DIRECTORY = os.path.dirname(os.path.abspath(__file__))

def get_local_ip():
    """Tự động phát hiện địa chỉ IP LAN của máy tính"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def load_config():
    """Đọc file cấu hình config.json hoặc config.example.json"""
    config_path = os.path.join(DIRECTORY, "config.json")
    if not os.path.exists(config_path):
        config_path = os.path.join(DIRECTORY, "config.example.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "server_port": 8080,
        "pc_ip": get_local_ip(),
        "chromecast_ip": "",
        "device_label": "CHROMECAST TV",
        "weather": {
            "city": "Ho Chi Minh City",
            "latitude": 10.8231,
            "longitude": 106.6297
        }
    }

CONFIG = load_config()
PORT = CONFIG.get("server_port", 8080)

# Biến toàn cục lưu cache data mới nhất
cached_stats = {
    "cpu": {"percent": 15.0, "freq": "2.4 GHz", "cores": psutil.cpu_count(logical=True)},
    "gpu": {"name": "GPU", "load": 0.0, "mem_used": 0.0, "mem_total": 4096.0, "temp": 45.0},
    "ram": {"percent": 65.0, "used": "10.0 GB", "total": "16.0 GB"},
    "disk": {"percent": 65.0, "free": "70.0 GB"},
    "network": {"download_mb": 0.0, "upload_mb": 0.0},
    "processes": [],
    "config": {
        "host_ip": CONFIG.get("pc_ip") or get_local_ip(),
        "target_ip": CONFIG.get("chromecast_ip", ""),
        "device_label": CONFIG.get("device_label", "CHROMECAST TV"),
        "weather": CONFIG.get("weather", {})
    }
}

stats_lock = threading.Lock()

class IntelGPUWatcher:
    """Thu thập thông số GPU tích hợp Intel qua Windows PDH & DXGI"""
    def __init__(self):
        self.pdh = None
        self.query = None
        self.counter = None
        self.intel_luid = None
        self.name = "Intel(R) UHD Graphics"
        self._init_dxgi_and_pdh()

    def _init_dxgi_and_pdh(self):
        if os.name != 'nt':
            return
        try:
            import ctypes
            from ctypes import wintypes, byref, c_void_p, POINTER, Structure, cast

            class GUID(Structure):
                _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD), ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]
            class LUID(Structure):
                _fields_ = [('LowPart', wintypes.DWORD), ('HighPart', wintypes.LONG)]
            class DXGI_ADAPTER_DESC(Structure):
                _fields_ = [
                    ('Description', wintypes.WCHAR * 128),
                    ('VendorId', wintypes.UINT), ('DeviceId', wintypes.UINT), ('SubSysId', wintypes.UINT), ('Revision', wintypes.UINT),
                    ('DedicatedVideoMemory', ctypes.c_size_t), ('DedicatedSystemMemory', ctypes.c_size_t), ('SharedSystemMemory', ctypes.c_size_t),
                    ('AdapterLuid', LUID),
                ]

            IID_IDXGIFactory = GUID(0x7b7166ec, 0x21c7, 0x44ae, (ctypes.c_ubyte * 8)(0xb2, 0x1a, 0xc9, 0xae, 0x32, 0x1a, 0xe3, 0x69))
            pFactory = c_void_p()
            dxgi = ctypes.windll.dxgi
            if dxgi.CreateDXGIFactory(byref(IID_IDXGIFactory), byref(pFactory)) == 0:
                vtable = cast(cast(pFactory, POINTER(c_void_p)).contents, POINTER(c_void_p))
                EnumAdapters = ctypes.WINFUNCTYPE(wintypes.HRESULT, c_void_p, wintypes.UINT, POINTER(c_void_p))(vtable[7])
                for i in range(5):
                    pAdapter = c_void_p()
                    if EnumAdapters(pFactory, i, byref(pAdapter)) != 0:
                        break
                    vtable_adapter = cast(cast(pAdapter, POINTER(c_void_p)).contents, POINTER(c_void_p))
                    GetDesc = ctypes.WINFUNCTYPE(wintypes.HRESULT, c_void_p, POINTER(DXGI_ADAPTER_DESC))(vtable_adapter[8])
                    desc = DXGI_ADAPTER_DESC()
                    GetDesc(pAdapter, byref(desc))
                    if 'intel' in desc.Description.lower():
                        self.name = desc.Description
                        self.intel_luid = f"{desc.AdapterLuid.HighPart:08X}_{desc.AdapterLuid.LowPart:08X}"
                        break

            self.pdh = ctypes.windll.pdh
            self.query = wintypes.HANDLE()
            if self.pdh.PdhOpenQueryW(None, 0, byref(self.query)) == 0:
                self.counter = wintypes.HANDLE()
                if self.pdh.PdhAddEnglishCounterW(self.query, r'\GPU Engine(*)\Utilization Percentage', 0, byref(self.counter)) == 0:
                    self.pdh.PdhCollectQueryData(self.query)
        except Exception:
            pass

    def get_load(self):
        if not self.pdh or not self.counter:
            return 0.0
        try:
            import ctypes
            from ctypes import wintypes, byref, Structure, cast
            self.pdh.PdhCollectQueryData(self.query)

            class PDH_FMT_COUNTERVALUE_DOUBLE(Structure):
                _fields_ = [('CStatus', wintypes.DWORD), ('doubleValue', ctypes.c_double)]
            class PDH_FMT_COUNTERVALUE_ITEM_W(Structure):
                _fields_ = [('szName', wintypes.LPWSTR), ('FmtValue', PDH_FMT_COUNTERVALUE_DOUBLE)]

            buffer_size = wintypes.DWORD(0)
            item_count = wintypes.DWORD(0)
            PDH_FMT_DOUBLE = 0x00000200
            self.pdh.PdhGetFormattedCounterArrayW(self.counter, PDH_FMT_DOUBLE, byref(buffer_size), byref(item_count), None)

            if buffer_size.value <= 0:
                return 0.0

            buf = (ctypes.c_byte * buffer_size.value)()
            self.pdh.PdhGetFormattedCounterArrayW(self.counter, PDH_FMT_DOUBLE, byref(buffer_size), byref(item_count), buf)
            items = cast(buf, POINTER(PDH_FMT_COUNTERVALUE_ITEM_W))

            intel_sum = 0.0
            for i in range(item_count.value):
                name = items[i].szName.upper()
                val = items[i].FmtValue.doubleValue
                if val > 0.0:
                    if self.intel_luid and self.intel_luid in name:
                        intel_sum += val
                    elif not self.intel_luid and ('3D' in name or 'VIDEODECODE' in name):
                        intel_sum += val

            return min(round(intel_sum, 1), 100.0)
        except Exception:
            return 0.0

intel_watcher = IntelGPUWatcher()

cached_gpu = {
    "intel": {
        "name": "Intel(R) UHD Graphics",
        "load": 0.0,
        "status": "Active"
    },
    "nvidia": {
        "name": "RTX 3050 Laptop GPU",
        "load": 0.0,
        "mem_used": 0.0,
        "mem_total": 4096.0,
        "temp": 50.0,
        "status": "Standby (Optimus)"
    },
    "name": "Intel + RTX 3050",
    "load": 0.0,
    "temp": 50.0
}
last_gpu_query = 0

def query_gpu():
    """Lấy thông số GPU đồng thời cho cả Intel iGPU và NVIDIA dGPU"""
    global cached_gpu, last_gpu_query
    now = time.time()

    # 1. Đo Intel GPU qua PDH
    intel_load = intel_watcher.get_load()

    # 2. Đo NVIDIA GPU qua nvidia-smi (đệm 2s)
    if now - last_gpu_query >= 2.0:
        last_gpu_query = now
        try:
            res = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2.0
            )
            parts = [p.strip() for p in res.strip().split(",")]
            gpu_name = parts[0].replace("NVIDIA GeForce ", "")
            load = float(parts[1])
            mem_u = float(parts[2])
            mem_t = float(parts[3])
            temp = float(parts[4])
            is_standby = (load == 0.0 and mem_u == 0.0)
            cached_gpu["nvidia"] = {
                "name": gpu_name,
                "load": load,
                "mem_used": mem_u,
                "mem_total": mem_t,
                "temp": temp,
                "status": "Standby (Optimus)" if is_standby else "Active"
            }
            cached_gpu["temp"] = temp
        except Exception:
            pass

    cached_gpu["intel"] = {
        "name": intel_watcher.name,
        "load": intel_load,
        "status": "Active" if intel_load > 0 else "Idle"
    }
    cached_gpu["load"] = max(intel_load, cached_gpu["nvidia"]["load"])
    return cached_gpu

def background_collector():
    """Luồng chạy ngầm thu thập toàn bộ dữ liệu phần cứng và top 10 tiến trình"""
    global cached_stats
    prev_net = psutil.net_io_counters()
    prev_time = time.time()
    prev_proc_io = {}

    # Khởi tạo mốc tính toán CPU ban đầu cho psutil
    psutil.cpu_percent(interval=None)

    while True:
        try:
            time.sleep(1.0)
            now = time.time()
            dt = max(now - prev_time, 0.5)

            # 1. Network toàn hệ thống
            curr_net = psutil.net_io_counters()
            dl_b = (curr_net.bytes_recv - prev_net.bytes_recv) / dt
            ul_b = (curr_net.bytes_sent - prev_net.bytes_sent) / dt
            dl_mb = round(dl_b / (1024 * 1024), 2)
            ul_mb = round(ul_b / (1024 * 1024), 2)
            prev_net = curr_net
            prev_time = now

            # 2. CPU, RAM, Disk toàn hệ thống
            sys_cpu_percent = psutil.cpu_percent(interval=None)
            cpu_freq = psutil.cpu_freq()
            freq_str = f"{round(cpu_freq.current / 1000, 2)} GHz" if (cpu_freq and cpu_freq.current) else "2.40 GHz"

            ram = psutil.virtual_memory()
            ram_p = ram.percent
            ram_used_gb = round(ram.used / (1024**3), 1)
            ram_total_gb = round(ram.total / (1024**3), 1)

            # Kiểm tra ổ C: (Windows) hoặc / (Linux/Mac)
            sys_drive = "C:\\" if os.name == "nt" else "/"
            disk_c = psutil.disk_usage(sys_drive)
            disk_p = disk_c.percent
            disk_free_gb = round(disk_c.free / (1024**3), 1)

            gpu = query_gpu()

            # 3. Quét Top 10 ứng dụng đang chạy trên hệ thống (Gom nhóm & sắp xếp theo RAM)
            app_map = {}
            curr_proc_io = {}

            for p in psutil.process_iter(['pid', 'name', 'io_counters', 'cpu_percent', 'memory_info']):
                try:
                    pid = p.info['pid']
                    name = p.info['name']
                    if not name or pid == 0:
                        continue

                    mem_info = p.info.get('memory_info')
                    mem_mb = round(mem_info.rss / (1024 * 1024), 1) if mem_info else 0.0
                    proc_cpu = round(p.info.get('cpu_percent') or 0.0, 1)

                    io = p.info.get('io_counters')
                    dr = 0.0
                    dw = 0.0
                    if io:
                        curr_proc_io[pid] = (io.read_bytes, io.write_bytes)
                        if pid in prev_proc_io:
                            old_r, old_w = prev_proc_io[pid]
                            dr = max((io.read_bytes - old_r) / dt, 0.0)
                            dw = max((io.write_bytes - old_w) / dt, 0.0)

                    if name not in app_map:
                        app_map[name] = {
                            "name": name,
                            "main_pid": pid,
                            "count": 1,
                            "download_kb": dr / 1024.0,
                            "upload_kb": dw / 1024.0,
                            "cpu": proc_cpu,
                            "ram_mb": mem_mb
                        }
                    else:
                        app_map[name]["count"] += 1
                        app_map[name]["download_kb"] += dr / 1024.0
                        app_map[name]["upload_kb"] += dw / 1024.0
                        app_map[name]["cpu"] += proc_cpu
                        app_map[name]["ram_mb"] += mem_mb
                        if mem_mb > (app_map[name]["ram_mb"] - mem_mb):
                            app_map[name]["main_pid"] = pid

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            prev_proc_io = curr_proc_io

            proc_list = []
            for app in app_map.values():
                display_name = app["name"]
                if app["count"] > 1:
                    display_name = f"{app['name']} ({app['count']})"

                proc_list.append({
                    "pid": app["main_pid"],
                    "name": display_name,
                    "download_kb": round(app["download_kb"], 1),
                    "upload_kb": round(app["upload_kb"], 1),
                    "total_rate_kb": round(app["download_kb"] + app["upload_kb"], 1),
                    "cpu": round(app["cpu"], 1),
                    "ram_mb": round(app["ram_mb"], 1)
                })

            # Sắp xếp theo RAM giảm dần -> Lấy Top 10 ứng dụng tốn nhiều tài nguyên nhất
            proc_list.sort(key=lambda x: (x["ram_mb"], x["cpu"]), reverse=True)
            top_10_procs = proc_list[:10]

            with stats_lock:
                cached_stats = {
                    "cpu": {
                        "percent": sys_cpu_percent,
                        "freq": freq_str,
                        "cores": psutil.cpu_count(logical=True)
                    },
                    "gpu": gpu,
                    "ram": {
                        "percent": ram_p,
                        "used": f"{ram_used_gb} GB",
                        "total": f"{ram_total_gb} GB"
                    },
                    "disk": {
                        "percent": disk_p,
                        "free": f"{disk_free_gb} GB"
                    },
                    "network": {
                        "download_mb": dl_mb,
                        "upload_mb": ul_mb
                    },
                    "processes": top_10_procs,
                    "config": {
                        "host_ip": CONFIG.get("pc_ip") or get_local_ip(),
                        "target_ip": CONFIG.get("chromecast_ip", ""),
                        "device_label": CONFIG.get("device_label", "CHROMECAST TV"),
                        "weather": CONFIG.get("weather", {})
                    }
                }
        except Exception:
            pass

class ThreadedHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def do_GET(self):
        if self.path == "/api/stats":
            with stats_lock:
                payload = json.dumps(cached_stats).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(payload)
        else:
            super().do_GET()

    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    psutil.cpu_percent(interval=None)
    t = threading.Thread(target=background_collector, daemon=True)
    t.start()

    local_ip = CONFIG.get("pc_ip") or get_local_ip()
    print("=====================================================")
    print("   CHROMECAST PC TELEMETRY HUD - SERVER RUNNING     ")
    print("=====================================================")
    print(f" Web Dashboard: http://localhost:{PORT}/dashboard.html")
    print(f" LAN Access:    http://{local_ip}:{PORT}/dashboard.html")
    print("=====================================================")
    
    with ThreadedHTTPServer(("", PORT), DashboardHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")
