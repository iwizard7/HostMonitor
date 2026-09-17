import asyncio
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
import aiosqlite

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "monitor.db")

PING_TIME_REGEX = re.compile(r'time[=<]([\d\.]+)\s*ms', re.IGNORECASE)
PING_TTL_REGEX = re.compile(r'ttl[=<](\d+)', re.IGNORECASE)

async def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS hosts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                target TEXT NOT NULL UNIQUE,
                interval_sec REAL DEFAULT 2.0,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ping_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                is_reachable INTEGER NOT NULL,
                latency_ms REAL,
                ttl INTEGER,
                error_msg TEXT,
                FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ping_host_ts ON ping_records(host_id, timestamp)")
        await db.commit()

        # Insert default hosts if empty
        cursor = await db.execute("SELECT COUNT(*) FROM hosts")
        count = (await cursor.fetchone())[0]
        if count == 0:
            default_hosts = [
                ("Cloudflare DNS", "1.1.1.1", 1.5),
                ("Google DNS", "8.8.8.8", 2.0),
                ("Google Web", "google.com", 2.0),
            ]
            for name, target, interval in default_hosts:
                await db.execute(
                    "INSERT INTO hosts (name, target, interval_sec, is_active) VALUES (?, ?, ?, 1)",
                    (name, target, interval)
                )
            await db.commit()

async def ping_host(target: str, timeout_sec: float = 1.5) -> Dict[str, Any]:
    timeout_ms = int(timeout_sec * 1000)
    # macOS ping: ping -c 1 -W <timeout_ms> <target>
    cmd = ["ping", "-c", "1", "-W", str(timeout_ms), target]
    t0 = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec + 1.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return {
                "is_reachable": False,
                "latency_ms": None,
                "ttl": None,
                "error_msg": "Ping timed out"
            }

        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")

        if proc.returncode == 0:
            time_match = PING_TIME_REGEX.search(out)
            ttl_match = PING_TTL_REGEX.search(out)
            latency = float(time_match.group(1)) if time_match else round((time.perf_counter() - t0) * 1000, 2)
            ttl = int(ttl_match.group(1)) if ttl_match else None
            return {
                "is_reachable": True,
                "latency_ms": latency,
                "ttl": ttl,
                "error_msg": None
            }
        else:
            msg = "Host unreachable"
            if "Unknown host" in err or "cannot resolve" in err or "Unknown host" in out:
                msg = "Cannot resolve hostname"
            elif "100.0% packet loss" in out or "100% packet loss" in out:
                msg = "100% packet loss (timeout)"
            return {
                "is_reachable": False,
                "latency_ms": None,
                "ttl": None,
                "error_msg": msg
            }
    except Exception as e:
        return {
            "is_reachable": False,
            "latency_ms": None,
            "ttl": None,
            "error_msg": str(e)
        }

class HostMonitorManager:
    def __init__(self):
        self.running = False
        self.tasks: Dict[int, asyncio.Task] = {}
        # In-memory recent cache for instant UI response: host_id -> list of ping dicts
        self.recent_history: Dict[int, List[Dict[str, Any]]] = {}
        self.max_cached_history = 120

    async def start(self):
        self.running = True
        await self.refresh_monitors()

    async def stop(self):
        self.running = False
        for task in self.tasks.values():
            task.cancel()
        self.tasks.clear()

    async def refresh_monitors(self):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM hosts")
            hosts = await cursor.fetchall()

        active_ids = set()
        for host in hosts:
            hid = host["id"]
            if host["is_active"]:
                active_ids.add(hid)
                if hid not in self.tasks or self.tasks[hid].done():
                    self.tasks[hid] = asyncio.create_task(
                        self._monitor_loop(hid, host["target"], host["interval_sec"])
                    )
            else:
                if hid in self.tasks:
                    self.tasks[hid].cancel()
                    del self.tasks[hid]

        # Cancel tasks for removed hosts
        for hid in list(self.tasks.keys()):
            if hid not in active_ids:
                self.tasks[hid].cancel()
                del self.tasks[hid]

    async def _monitor_loop(self, host_id: int, target: str, interval: float):
        if host_id not in self.recent_history:
            self.recent_history[host_id] = []

        while self.running:
            try:
                res = await ping_host(target, timeout_sec=min(2.0, max(1.0, interval * 0.9)))
                now_utc = datetime.now(timezone.utc).isoformat()
                record = {
                    "host_id": host_id,
                    "timestamp": now_utc,
                    "is_reachable": 1 if res["is_reachable"] else 0,
                    "latency_ms": res["latency_ms"],
                    "ttl": res["ttl"],
                    "error_msg": res["error_msg"]
                }

                # Update in-memory buffer
                hist = self.recent_history.setdefault(host_id, [])
                hist.append(record)
                if len(hist) > self.max_cached_history:
                    hist.pop(0)

                # Persist to SQLite
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("""
                        INSERT INTO ping_records (host_id, timestamp, is_reachable, latency_ms, ttl, error_msg)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (host_id, now_utc, record["is_reachable"], record["latency_ms"], record["ttl"], record["error_msg"]))
                    await db.commit()

            except asyncio.CancelledError:
                break
            except Exception as ex:
                pass

            await asyncio.sleep(max(0.5, interval))

    def get_recent(self, host_id: int) -> List[Dict[str, Any]]:
        return list(self.recent_history.get(host_id, []))

monitor_manager = HostMonitorManager()
