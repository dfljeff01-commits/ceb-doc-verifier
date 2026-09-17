# -*- coding: utf-8 -*-
"""APK 局域网分发服务（App任务书·构建与分发）。

作用：
  1. 在本机 8001 端口提供 APK 下载（http://<局域网IP>:8001/app-release.apk）
  2. 自动生成扫码下载二维码（apk_qrcode.png，项目根目录）

用法：python serve_apk.py   （手机与电脑需同一Wi-Fi）
"""

import socket
from pathlib import Path

import qrcode
import qrcode.image.pil

ROOT = Path(__file__).parent
APK_DIR = ROOT / "mobile_app" / "build" / "app" / "outputs" / "flutter-apk"
# 分发arm64版（现代手机，17MB）；老设备请改用 app-release.apk（49MB通用版）
APK = APK_DIR / "app-arm64-v8a-release.apk"
PORT = 8001


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def main():
    if not APK.exists():
        print(f"❌ 未找到 APK：{APK}\n请先执行 flutter build apk --release")
        raise SystemExit(1)
    ip = lan_ip()
    url = f"http://{ip}:{PORT}/{APK.name}"

    img = qrcode.make(url)
    qr_path = ROOT / "apk_qrcode.png"
    img.save(qr_path)
    print(f"✅ APK: {APK.name} ({APK.stat().st_size / 1024 / 1024:.1f} MB, arm64)")
    print(f"   通用版(含全部ABI): {APK_DIR / 'app-release.apk'}")
    print(f"✅ 下载地址: {url}")
    print(f"✅ 二维码已生成: {qr_path}")
    print(f"   手机与电脑连同一Wi-Fi，扫码即装。后端API地址填 http://{ip}:8000")

    from http.server import HTTPServer, SimpleHTTPRequestHandler

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(APK.parent), **kw)

    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
