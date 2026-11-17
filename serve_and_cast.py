import http.server
import socketserver
import threading
import sys
import os
import time
import socket
import json
import pychromecast
from pychromecast.controllers import BaseController
from server import DashboardHandler, background_collector, load_config, get_local_ip

sys.stdout.reconfigure(encoding='utf-8')

DIRECTORY = os.path.dirname(os.path.abspath(__file__))
CONFIG = load_config()

PORT = CONFIG.get("server_port", 8080)
PC_IP = CONFIG.get("pc_ip") or get_local_ip()
CHROMECAST_IP = CONFIG.get("chromecast_ip", "").strip()

DASHCAST_APP_ID = "84912283"
DASHCAST_NAMESPACE = "urn:x-cast:com.madmod.dashcast"

class DashCastController(BaseController):
    def __init__(self):
        super(DashCastController, self).__init__(DASHCAST_NAMESPACE, DASHCAST_APP_ID)

    def load_url(self, url, force=True, reload_time=0):
        self.send_message({
            "url": url,
            "force": force,
            "reload": reload_time != 0,
            "reload_time": reload_time
        })

def start_server():
    t_collector = threading.Thread(target=background_collector, daemon=True)
    t_collector.start()
    with socketserver.TCPServer(("", PORT), DashboardHandler) as httpd:
        httpd.serve_forever()

if __name__ == "__main__":
    print("=====================================================")
    print("   CHROMECAST PC TELEMETRY HUD - SERVE & CAST       ")
    print("=====================================================")

    # 1. Khởi chạy HTTP Server & Background Collector
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    time.sleep(1)

    dashboard_url = f"http://{PC_IP}:{PORT}/dashboard.html"
    print(f"📡 HTTP Server đang hoạt động tại: {dashboard_url}")

    # 2. Tìm kiếm Chromecast (theo IP cấu hình hoặc tự động quét)
    cast = None
    browser = None

    if CHROMECAST_IP:
        print(f"🔍 Đang kết nối tới Chromecast tại {CHROMECAST_IP}...")
        chromecasts, browser = pychromecast.get_chromecasts(known_hosts=[CHROMECAST_IP])
        if chromecasts:
            cast = chromecasts[0]
    else:
        print("🔍 Không có chromecast_ip trong config.json, đang tự động quét mạng LAN...")
        chromecasts, browser = pychromecast.get_chromecasts()
        if chromecasts:
            cast = chromecasts[0]
            print(f"✅ Đã tìm thấy Chromecast: {cast.name} ({cast.cast_info.host})")

    if not cast:
        print("❌ Không tìm thấy thiết bị Chromecast nào trên mạng LAN!")
        print("💡 Gợi ý: Hãy kiểm tra IP của Chromecast và điền vào file config.json.")
        sys.exit(1)

    cast.wait()
    dash_controller = DashCastController()
    cast.register_handler(dash_controller)

    # 3. Cast Dashboard URL lên TV
    print(f"📺 Đang truyền Live Dashboard lên màn hình TV: {dashboard_url}")
    dash_controller.load_url(dashboard_url)

    print("🎉 Truyền thành công! Dashboard đang hiển thị Realtime trên TV.")
    print("👉 Nhấn Ctrl + C để dừng dịch vụ.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nĐang dừng dịch vụ và ngắt kết nối...")
        if browser:
            pychromecast.discovery.stop_discovery(browser)
