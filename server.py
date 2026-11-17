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

def query_gpu():
    """Lấy thông số GPU qua nvidia-smi nếu có NVIDIA GPU"""
    try:
        res = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1
        )
        parts = [p.strip() for p in res.strip().split(",")]
        gpu_name = parts[0]
        load = float(parts[1])
        mem_u = float(parts[2])
        mem_t = float(parts[3])
        temp = float(parts[4])
        return {
            "name": gpu_name,
            "load": load,
            "mem_used": mem_u,
            "mem_total": mem_t,
            "temp": temp
        }
    except Exception:
        return {
            "name": "Integrated GPU / Idle",
            "load": 5.0,
            "mem_used": 512.0,
            "mem_total": 4096.0,
            "temp": 42.0
        }

def background_collector():
    """Luồng chạy ngầm thu thập toàn bộ dữ liệu phần cứng và top 10 tiến trình"""
    global cached_stats
    prev_net = psutil.net_io_counters()
    prev_time = time.time()
    prev_proc_io = {}

    while True:
        try:
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

            # 2. CPU, RAM, Disk
            cpu_p = psutil.cpu_percent(interval=None)
            cpu_freq = psutil.cpu_freq()
            freq_str = f"{round(cpu_freq.current / 1000, 2)} GHz" if cpu_freq else "2.4 GHz"

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
                    cpu_p = round(p.info.get('cpu_percent') or 0.0, 1)

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
                            "cpu": cpu_p,
                            "ram_mb": mem_mb
                        }
                    else:
                        app_map[name]["count"] += 1
                        app_map[name]["download_kb"] += dr / 1024.0
                        app_map[name]["upload_kb"] += dw / 1024.0
                        app_map[name]["cpu"] += cpu_p
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
                        "percent": cpu_p,
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
        time.sleep(1.0)

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
