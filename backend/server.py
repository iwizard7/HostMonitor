import csv
import io
import math
import os
from contextlib import asynccontextmanager
from typing import Optional, List
from datetime import datetime, timezone, timedelta

import aiosqlite
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.monitor import DB_PATH, init_db, monitor_manager, check_host
from backend.traceroute import run_traceroute

class HostCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    target: str = Field(..., min_length=1, max_length=255)
    interval_sec: float = Field(2.0, ge=0.5, le=60.0)
    check_type: str = Field("icmp", pattern="^(icmp|tcp|http)$")
    port: Optional[int] = Field(None, ge=1, le=65535)
    group_name: str = Field("Основное", max_length=50)

class HostUpdate(BaseModel):
    name: Optional[str] = None
    interval_sec: Optional[float] = None
    is_active: Optional[bool] = None
    check_type: Optional[str] = None
    port: Optional[int] = None
    group_name: Optional[str] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await monitor_manager.start()
    yield
    await monitor_manager.stop()

app = FastAPI(title="Host Monitor macOS", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")

def calculate_jitter(recent_records: List[dict]) -> Optional[float]:
    """Calculates RFC 3550 style interarrival jitter in ms."""
    latencies = [r["latency_ms"] for r in recent_records if r.get("is_reachable") and r.get("latency_ms") is not None]
    if len(latencies) < 2:
        return 0.0
    diffs = [abs(latencies[i] - latencies[i-1]) for i in range(1, len(latencies))]
    return round(sum(diffs) / len(diffs), 2)

def calculate_quality_score(uptime_pct: float, loss_pct: float, avg_latency: Optional[float], jitter: Optional[float]) -> dict:
    """Computes connection quality rating (A+, A, B, C, F) and numeric score 0-100."""
    score = uptime_pct * 0.5 + (100.0 - loss_pct) * 0.3
    if avg_latency is not None:
        lat_penalty = min(20.0, (avg_latency / 150.0) * 15.0)
        score -= lat_penalty
    if jitter is not None:
        jit_penalty = min(10.0, (jitter / 20.0) * 8.0)
        score -= jit_penalty

    score = max(0.0, min(100.0, round(score, 1)))
    grade = "A+" if score >= 96 else "A" if score >= 88 else "B" if score >= 75 else "C" if score >= 60 else "F"
    return {"score": score, "grade": grade}

@app.get("/api/hosts")
async def get_hosts():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM hosts ORDER BY group_name ASC, id ASC")
        rows = await cursor.fetchall()
        
        result = []
        for row in rows:
            h = dict(row)
            stats_cursor = await db.execute("""
                SELECT 
                    COUNT(*) as total_pings,
                    SUM(CASE WHEN is_reachable = 1 THEN 1 ELSE 0 END) as successful_pings,
                    MIN(latency_ms) as min_latency,
                    AVG(latency_ms) as avg_latency,
                    MAX(latency_ms) as max_latency
                FROM ping_records 
                WHERE host_id = ?
            """, (h["id"],))
            stats = dict(await stats_cursor.fetchone() or {})
            
            recent = monitor_manager.get_recent(h["id"])
            latest = recent[-1] if recent else None
            
            total = stats.get("total_pings") or 0
            succ = stats.get("successful_pings") or 0
            lost = total - succ
            loss_pct = round((1.0 - (succ / total)) * 100, 1) if total > 0 else 0.0
            uptime_pct = round((succ / total) * 100, 1) if total > 0 else 100.0
            avg_lat = round(stats["avg_latency"], 2) if stats.get("avg_latency") is not None else None
            jitter = calculate_jitter(recent[-30:]) if recent else 0.0

            h["stats"] = {
                "total_pings": total,
                "successful_pings": succ,
                "lost_pings": lost,
                "packet_loss_pct": loss_pct,
                "uptime_pct": uptime_pct,
                "min_latency": round(stats["min_latency"], 2) if stats.get("min_latency") is not None else None,
                "avg_latency": avg_lat,
                "max_latency": round(stats["max_latency"], 2) if stats.get("max_latency") is not None else None,
                "jitter_ms": jitter,
                "quality": calculate_quality_score(uptime_pct, loss_pct, avg_lat, jitter)
            }
            h["latest"] = latest
            h["is_active"] = bool(h["is_active"])
            h["check_type"] = h.get("check_type") or "icmp"
            h["group_name"] = h.get("group_name") or "Основное"
            result.append(h)

        return result

@app.post("/api/hosts")
async def add_host(payload: HostCreate):
    target = payload.target.strip()
    name = payload.name.strip()
    group = (payload.group_name or "Основное").strip()
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cursor = await db.execute(
                """INSERT INTO hosts (name, target, interval_sec, is_active, check_type, port, group_name) 
                   VALUES (?, ?, ?, 1, ?, ?, ?)""",
                (name, target, payload.interval_sec, payload.check_type, payload.port, group)
            )
            await db.commit()
            host_id = cursor.lastrowid
        except aiosqlite.IntegrityError:
            raise HTTPException(status_code=400, detail="Хост с таким адресом уже добавлен!")

    await monitor_manager.refresh_monitors()
    return {
        "id": host_id,
        "name": name,
        "target": target,
        "interval_sec": payload.interval_sec,
        "check_type": payload.check_type,
        "port": payload.port,
        "group_name": group,
        "is_active": True
    }

@app.patch("/api/hosts/{host_id}")
async def update_host(host_id: int, payload: HostUpdate):
    async with aiosqlite.connect(DB_PATH) as db:
        updates = []
        params = []
        if payload.name is not None:
            updates.append("name = ?")
            params.append(payload.name.strip())
        if payload.interval_sec is not None:
            updates.append("interval_sec = ?")
            params.append(payload.interval_sec)
        if payload.is_active is not None:
            updates.append("is_active = ?")
            params.append(1 if payload.is_active else 0)
        if payload.check_type is not None:
            updates.append("check_type = ?")
            params.append(payload.check_type)
        if payload.port is not None:
            updates.append("port = ?")
            params.append(payload.port)
        if payload.group_name is not None:
            updates.append("group_name = ?")
            params.append(payload.group_name.strip())

        if not updates:
            return {"status": "no change"}

        params.append(host_id)
        await db.execute(f"UPDATE hosts SET {', '.join(updates)} WHERE id = ?", params)
        await db.commit()

    await monitor_manager.refresh_monitors()
    return {"status": "ok"}

@app.delete("/api/hosts/{host_id}")
async def delete_host(host_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM ping_records WHERE host_id = ?", (host_id,))
        await db.execute("DELETE FROM hosts WHERE id = ?", (host_id,))
        await db.commit()

    await monitor_manager.refresh_monitors()
    return {"status": "deleted"}

@app.get("/api/hosts/{host_id}/history")
async def get_host_history(
    host_id: int,
    limit: int = Query(60, ge=10, le=2000),
    time_range: str = Query("1m", pattern="^(1m|5m|15m|1h|24h|all)$")
):
    """Returns historical points filtered by time range (1m, 5m, 15m, 1h, 24h, all)."""
    now = datetime.now(timezone.utc)
    delta_map = {
        "1m": timedelta(minutes=1),
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "24h": timedelta(hours=24),
    }

    if time_range == "1m":
        recent = monitor_manager.get_recent(host_id)
        if recent:
            cutoff = (now - timedelta(minutes=1)).isoformat()
            filtered = [r for r in recent if r["timestamp"] >= cutoff]
            if filtered:
                return filtered[-limit:]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if time_range in delta_map:
            since = (now - delta_map[time_range]).isoformat()
            cursor = await db.execute("""
                SELECT host_id, timestamp, is_reachable, latency_ms, ttl, error_msg 
                FROM ping_records 
                WHERE host_id = ? AND timestamp >= ?
                ORDER BY timestamp ASC
            """, (host_id, since))
        else: # 'all'
            cursor = await db.execute("""
                SELECT host_id, timestamp, is_reachable, latency_ms, ttl, error_msg 
                FROM ping_records 
                WHERE host_id = ? 
                ORDER BY id DESC LIMIT ?
            """, (host_id, limit))

        rows = await cursor.fetchall()
        if time_range == "all":
            return [dict(r) for r in reversed(rows)]
        return [dict(r) for r in rows]

@app.get("/api/hosts/{host_id}/export/csv")
async def export_host_csv(host_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        h_cursor = await db.execute("SELECT * FROM hosts WHERE id = ?", (host_id,))
        host = await h_cursor.fetchone()
        if not host:
            raise HTTPException(status_code=404, detail="Host not found")

        cursor = await db.execute("""
            SELECT timestamp, is_reachable, latency_ms, ttl, error_msg 
            FROM ping_records 
            WHERE host_id = ? 
            ORDER BY timestamp ASC
        """, (host_id,))
        records = await cursor.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Timestamp (ISO)",
        "Host Name",
        "Target (IP/Domain)",
        "Group",
        "Protocol",
        "Status",
        "Latency (ms)",
        "TTL",
        "Error Details"
    ])

    for r in records:
        writer.writerow([
            r["timestamp"],
            host["name"],
            host["target"],
            host.get("group_name", "Основное"),
            host.get("check_type", "icmp").upper(),
            "ONLINE" if r["is_reachable"] else "OFFLINE",
            f"{r['latency_ms']:.2f}" if r["latency_ms"] is not None else "N/A",
            r["ttl"] if r["ttl"] is not None else "N/A",
            r["error_msg"] or ""
        ])

    output.seek(0)
    safe_name = "".join(c if c.isalnum() else "_" for c in host["name"])
    filename = f"host_monitor_{safe_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.get("/api/hosts/{host_id}/traceroute")
async def get_host_traceroute(host_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        h_cursor = await db.execute("SELECT * FROM hosts WHERE id = ?", (host_id,))
        host = await h_cursor.fetchone()
        if not host:
            raise HTTPException(status_code=404, detail="Host not found")

    target = host["target"]
    route_data = await run_traceroute(target, max_hops=18)
    return {
        "host_id": host_id,
        "name": host["name"],
        "target": target,
        "is_vpn": route_data.get("is_vpn", False),
        "interface": route_data.get("interface", ""),
        "route_description": route_data.get("route_description", ""),
        "hops": route_data.get("hops", [])
    }

if os.path.exists(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
