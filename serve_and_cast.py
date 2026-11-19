import http.server
import socketserver
import threading
import sys
import os
import time
import socket
import json
import logging

# Bỏ qua các file nhị phân Cython (.pyd) chưa được ký chứng chỉ số để khắc phục triệt để lỗi
# "An Application Control policy has blocked this file" trên Windows 11 Smart App Control / WDAC
class _ZeroconfPurePythonFinder:
    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        if fullname.startswith('zeroconf'):
            for p in (path or sys.path):
                parts = fullname.split('.')
                mod_name = parts[-1]
                py_path = os.path.join(p, mod_name + '.py')
                init_path = os.path.join(p, mod_name, '__init__.py')
                if os.path.isfile(py_path):
                    from importlib.util import spec_from_file_location
                    return spec_from_file_location(fullname, py_path)
                elif os.path.isfile(init_path):
                    from importlib.util import spec_from_file_location
                    return spec_from_file_location(fullname, init_path, submodule_search_locations=[os.path.join(p, mod_name)])
        return None

sys.meta_path.insert(0, _ZeroconfPurePythonFinder)

# Tắt các cảnh báo retry không cần thiết từ các thiết bị khác trên mạng LAN (như Android TV box, TV khác)
logging.getLogger('pychromecast').setLevel(logging.ERROR)

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

    # 2. Tìm kiếm Chromecast (ưu tiên chính xác IP đã cấu hình)
    cast = None
    browser = None

    if CHROMECAST_IP:
        print(f"🔍 Đang kết nối tới Chromecast tại {CHROMECAST_IP}...")
        chromecasts, browser = pychromecast.get_chromecasts(known_hosts=[CHROMECAST_IP])
        target_casts = [c for c in chromecasts if getattr(c.cast_info, 'host', '') == CHROMECAST_IP]
        if target_casts:
            cast = target_casts[0]
            print(f"✅ Đã tìm thấy đúng thiết bị: {cast.name} ({cast.cast_info.host})")
        elif chromecasts:
            cast = chromecasts[0]
            print(f"⚠️ Không khớp IP chính xác, chọn thiết bị đầu tiên: {cast.name} ({cast.cast_info.host})")
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

    print(f"⏳ Đang chờ thiết bị '{cast.name}' phản hồi...")
    cast.wait(timeout=10)

    # 3. Thoát phiên ứng dụng cũ nếu có để đảm bảo Chromecast luôn mở phiên mới tinh
    if getattr(cast, 'app_id', None) == DASHCAST_APP_ID:
        print("🔄 Phát hiện phiên DashCast cũ trên TV, đang khởi động lại ứng dụng...")
        try:
            cast.quit_app()
            time.sleep(1.0)
            cast.wait(timeout=5)
        except Exception:
            pass

    dash_controller = DashCastController()
    cast.register_handler(dash_controller)

    # 4. Cast Dashboard URL lên TV (Kèm timestamp session chống cache để Chromecast luôn refresh)
    session_id = int(time.time())
    dashboard_url_fresh = f"{dashboard_url}?t={session_id}"
    print(f"📺 Đang truyền Live Dashboard lên màn hình TV: {dashboard_url_fresh}")
    dash_controller.load_url(dashboard_url_fresh, force=True)

    print("🎉 Truyền thành công! Dashboard đang hiển thị Realtime trên TV.")
    print("👉 Nhấn Ctrl + C để dừng dịch vụ.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 Đang dừng dịch vụ...")
        try:
            print("📺 Đang thoát ứng dụng trên Chromecast về màn hình chờ...")
            cast.quit_app()
            time.sleep(0.5)
        except Exception:
            pass
        if browser:
            pychromecast.discovery.stop_discovery(browser)
