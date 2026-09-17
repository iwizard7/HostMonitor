import asyncio
import os
import re
import socket
import ssl
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
                check_type TEXT DEFAULT 'icmp', -- 'icmp', 'tcp', 'http'
                port INTEGER DEFAULT NULL,
                group_name TEXT DEFAULT 'Основное',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Safe migration if table already exists
        columns = [row[1] for row in await (await db.execute("PRAGMA table_info(hosts)")).fetchall()]
        if "check_type" not in columns:
            await db.execute("ALTER TABLE hosts ADD COLUMN check_type TEXT DEFAULT 'icmp'")
        if "port" not in columns:
            await db.execute("ALTER TABLE hosts ADD COLUMN port INTEGER DEFAULT NULL")
        if "group_name" not in columns:
            await db.execute("ALTER TABLE hosts ADD COLUMN group_name TEXT DEFAULT 'Основное'")

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
                ("Cloudflare DNS", "1.1.1.1", 1.5, "icmp", None, "DNS Серверы"),
                ("Google DNS", "8.8.8.8", 2.0, "icmp", None, "DNS Серверы"),
                ("Google Web", "google.com", 2.0, "http", 443, "Веб-сайты"),
            ]
            for name, target, interval, c_type, port, group in default_hosts:
                await db.execute(
                    "INSERT INTO hosts (name, target, interval_sec, is_active, check_type, port, group_name) VALUES (?, ?, ?, 1, ?, ?, ?)",
                    (name, target, interval, c_type, port, group)
                )
            await db.commit()

async def ping_icmp(target: str, timeout_sec: float = 1.5) -> Dict[str, Any]:
    timeout_ms = int(timeout_sec * 1000)
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
                msg = "100% packet loss"
            return {
                "is_reachable": False,
                "latency_ms": None,
                "ttl": None,
                "error_msg": msg
            }
    except Exception as e:
        return {"is_reachable": False, "latency_ms": None, "ttl": None, "error_msg": str(e)}

async def ping_tcp(target: str, port: int = 80, timeout_sec: float = 2.0) -> Dict[str, Any]:
    t0 = time.perf_counter()
    try:
        fut = asyncio.open_connection(target, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout_sec)
        latency = round((time.perf_counter() - t0) * 1000, 2)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return {
            "is_reachable": True,
            "latency_ms": latency,
            "ttl": None,
            "error_msg": None
        }
    except asyncio.TimeoutError:
        return {"is_reachable": False, "latency_ms": None, "ttl": None, "error_msg": f"TCP timeout (порт {port})"}
    except ConnectionRefusedError:
        return {"is_reachable": False, "latency_ms": None, "ttl": None, "error_msg": f"Порт {port} закрыт (Connection Refused)"}
    except Exception as e:
        return {"is_reachable": False, "latency_ms": None, "ttl": None, "error_msg": str(e)}

async def ping_http(target: str, port: Optional[int] = None, timeout_sec: float = 3.0) -> Dict[str, Any]:
    # Clean up url
    url_target = target
    use_ssl = True
    if target.startswith("http://"):
        use_ssl = False
        url_target = target[7:]
    elif target.startswith("https://"):
        use_ssl = True
        url_target = target[8:]

    host_only = url_target.split("/")[0].split(":")[0]
    effective_port = port if port else (443 if use_ssl else 80)

    t0 = time.perf_counter()
    try:
        ssl_ctx = ssl.create_default_context() if use_ssl else None
        fut = asyncio.open_connection(host_only, effective_port, ssl=ssl_ctx)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout_sec)
        
        req = f"HEAD / HTTP/1.1\r\nHost: {host_only}\r\nUser-Agent: HostMonitor/2.0\r\nConnection: close\r\n\r\n"
        writer.write(req.encode("utf-8"))
        await writer.drain()

        line = await asyncio.wait_for(reader.readline(), timeout=timeout_sec)
        latency = round((time.perf_counter() - t0) * 1000, 2)
        
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

        status_str = line.decode("utf-8", errors="replace").strip()
        return {
            "is_reachable": True,
            "latency_ms": latency,
            "ttl": None,
            "error_msg": status_str if status_str else "HTTP OK"
        }
    except asyncio.TimeoutError:
        return {"is_reachable": False, "latency_ms": None, "ttl": None, "error_msg": "HTTP request timeout"}
    except Exception as e:
        return {"is_reachable": False, "latency_ms": None, "ttl": None, "error_msg": str(e)}

async def check_host(target: str, check_type: str = "icmp", port: Optional[int] = None, timeout_sec: float = 2.0) -> Dict[str, Any]:
    if check_type == "tcp":
        return await ping_tcp(target, port=port or 80, timeout_sec=timeout_sec)
    elif check_type == "http":
        return await ping_http(target, port=port, timeout_sec=timeout_sec)
    else:
        return await ping_icmp(target, timeout_sec=timeout_sec)

class HostMonitorManager:
    def __init__(self):
        self.running = False
        self.tasks: Dict[int, asyncio.Task] = {}
        # In-memory recent cache: host_id -> list of ping dicts (max 500)
        self.recent_history: Dict[int, List[Dict[str, Any]]] = {}
        self.max_cached_history = 500

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
                        self._monitor_loop(
                            hid,
                            host["target"],
                            host["interval_sec"],
                            host["check_type"] or "icmp",
                            host["port"]
                        )
                    )
            else:
                if hid in self.tasks:
                    self.tasks[hid].cancel()
                    del self.tasks[hid]

        for hid in list(self.tasks.keys()):
            if hid not in active_ids:
                self.tasks[hid].cancel()
                del self.tasks[hid]

    async def _monitor_loop(self, host_id: int, target: str, interval: float, check_type: str, port: Optional[int]):
        if host_id not in self.recent_history:
            self.recent_history[host_id] = []

        while self.running:
            try:
                res = await check_host(target, check_type=check_type, port=port, timeout_sec=min(2.5, max(1.0, interval * 0.9)))
                now_utc = datetime.now(timezone.utc).isoformat()
                record = {
                    "host_id": host_id,
                    "timestamp": now_utc,
                    "is_reachable": 1 if res["is_reachable"] else 0,
                    "latency_ms": res["latency_ms"],
                    "ttl": res["ttl"],
                    "error_msg": res["error_msg"]
                }

                # In-memory buffer
                hist = self.recent_history.setdefault(host_id, [])
                hist.append(record)
                if len(hist) > self.max_cached_history:
                    hist.pop(0)

                # Persist
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("""
                        INSERT INTO ping_records (host_id, timestamp, is_reachable, latency_ms, ttl, error_msg)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (host_id, now_utc, record["is_reachable"], record["latency_ms"], record["ttl"], record["error_msg"]))
                    await db.commit()

            except asyncio.CancelledError:
                break
            except Exception:
                pass

            await asyncio.sleep(max(0.5, interval))

    def get_recent(self, host_id: int) -> List[Dict[str, Any]]:
        return list(self.recent_history.get(host_id, []))

monitor_manager = HostMonitorManager()
