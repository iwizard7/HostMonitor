import json
import urllib.request
import webbrowser
import rumps

API_URL = "http://127.0.0.1:8000/api/hosts"

class HostMonitorMenuBarApp(rumps.App):
    def __init__(self):
        super(HostMonitorMenuBarApp, self).__init__("Host Monitor", title="📡 -- ms")
        self.menu = ["Открыть интерфейс", None, "Хосты: Загрузка...", None, "Выход"]
        self.timer = rumps.Timer(self.update_status, 2)
        self.timer.start()

    def update_status(self, _):
        try:
            req = urllib.request.Request(API_URL, headers={"User-Agent": "HostMonitorTray"})
            with urllib.request.urlopen(req, timeout=1.5) as res:
                if res.status == 200:
                    hosts = json.loads(res.read().decode())
                    total = len(hosts)
                    offline = [h for h in hosts if h.get("is_active") and h.get("latest") and not h["latest"].get("is_reachable")]
                    online = [h for h in hosts if h.get("is_active") and h.get("latest") and h["latest"].get("is_reachable")]

                    if offline:
                        self.title = f"🔴 {len(offline)} DOWN"
                    elif online:
                        # average latency of active hosts
                        lats = [h["latest"]["latency_ms"] for h in online if h["latest"].get("latency_ms") is not None]
                        avg_lat = round(sum(lats) / len(lats), 1) if lats else 0
                        self.title = f"🟢 {avg_lat}ms"
                    else:
                        self.title = "⚪ 0 хостов"

                    # Build menu items
                    new_menu = [
                        rumps.MenuItem("Открыть интерфейс", callback=self.open_ui),
                        None,
                    ]
                    for h in hosts:
                        status = "🟢" if (h.get("latest") and h["latest"].get("is_reachable")) else ("⏸️" if not h.get("is_active") else "🔴")
                        lat = f"{h['latest']['latency_ms']:.1f}ms" if (h.get("latest") and h["latest"].get("latency_ms") is not None) else "--"
                        name = h.get("name", "Host")[:20]
                        item = rumps.MenuItem(f"{status} {name}: {lat}")
                        new_menu.append(item)

                    new_menu.extend([None, rumps.MenuItem("Выход", callback=rumps.quit_application)])
                    self.menu.clear()
                    self.menu = new_menu
        except Exception:
            self.title = "⚠️ Офлайн"

    def open_ui(self, _):
        webbrowser.open("http://127.0.0.1:8000")

if __name__ == "__main__":
    app = HostMonitorMenuBarApp()
    app.run()
