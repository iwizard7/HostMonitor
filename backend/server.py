import csv
import io
import os
from contextlib import asynccontextmanager
from typing import Optional, List
from datetime import datetime, timezone

import aiosqlite
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.monitor import DB_PATH, init_db, monitor_manager, ping_host

class HostCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    target: str = Field(..., min_length=1, max_length=255)
    interval_sec: float = Field(2.0, ge=0.5, le=60.0)

class HostUpdate(BaseModel):
    name: Optional[str] = None
    interval_sec: Optional[float] = None
    is_active: Optional[bool] = None

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

@app.get("/api/hosts")
async def get_hosts():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM hosts ORDER BY id ASC")
        rows = await cursor.fetchall()
        
        result = []
        for row in rows:
            h = dict(row)
            # Fetch summary stats
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
            
            # Get latest ping
            recent = monitor_manager.get_recent(h["id"])
            latest = recent[-1] if recent else None
            
            total = stats.get("total_pings") or 0
            succ = stats.get("successful_pings") or 0
            lost = total - succ
            loss_pct = round((1.0 - (succ / total)) * 100, 1) if total > 0 else 0.0
            uptime_pct = round((succ / total) * 100, 1) if total > 0 else 100.0

            h["stats"] = {
                "total_pings": total,
                "successful_pings": succ,
                "lost_pings": lost,
                "packet_loss_pct": loss_pct,
                "uptime_pct": uptime_pct,
                "min_latency": round(stats["min_latency"], 2) if stats.get("min_latency") is not None else None,
                "avg_latency": round(stats["avg_latency"], 2) if stats.get("avg_latency") is not None else None,
                "max_latency": round(stats["max_latency"], 2) if stats.get("max_latency") is not None else None,
            }
            h["latest"] = latest
            h["is_active"] = bool(h["is_active"])
            result.append(h)

        return result

@app.post("/api/hosts")
async def add_host(payload: HostCreate):
    target = payload.target.strip()
    name = payload.name.strip()
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            cursor = await db.execute(
                "INSERT INTO hosts (name, target, interval_sec, is_active) VALUES (?, ?, ?, 1)",
                (name, target, payload.interval_sec)
            )
            await db.commit()
            host_id = cursor.lastrowid
        except aiosqlite.IntegrityError:
            raise HTTPException(status_code=400, detail="Хост с таким адресом уже добавлен!")

    await monitor_manager.refresh_monitors()
    return {"id": host_id, "name": name, "target": target, "interval_sec": payload.interval_sec, "is_active": True}

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
async def get_host_history(host_id: int, limit: int = Query(60, ge=10, le=1000)):
    # Combine in-memory live points with DB if needed
    recent = monitor_manager.get_recent(host_id)
    if len(recent) >= limit:
        return recent[-limit:]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT host_id, timestamp, is_reachable, latency_ms, ttl, error_msg 
            FROM ping_records 
            WHERE host_id = ? 
            ORDER BY id DESC LIMIT ?
        """, (host_id, limit))
        rows = await cursor.fetchall()
        db_records = [dict(r) for r in reversed(rows)]
        return db_records

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

@app.get("/api/export/all/csv")
async def export_all_csv():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT p.timestamp, h.name as host_name, h.target, p.is_reachable, p.latency_ms, p.ttl, p.error_msg
            FROM ping_records p
            JOIN hosts h ON p.host_id = h.id
            ORDER BY p.timestamp ASC
        """)
        records = await cursor.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Timestamp (ISO)",
        "Host Name",
        "Target (IP/Domain)",
        "Status",
        "Latency (ms)",
        "TTL",
        "Error Details"
    ])

    for r in records:
        writer.writerow([
            r["timestamp"],
            r["host_name"],
            r["target"],
            "ONLINE" if r["is_reachable"] else "OFFLINE",
            f"{r['latency_ms']:.2f}" if r["latency_ms"] is not None else "N/A",
            r["ttl"] if r["ttl"] is not None else "N/A",
            r["error_msg"] or ""
        ])

    output.seek(0)
    filename = f"host_monitor_ALL_HOSTS_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

from backend.traceroute import run_traceroute

@app.get("/api/hosts/{host_id}/traceroute")
async def get_host_traceroute(host_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        h_cursor = await db.execute("SELECT * FROM hosts WHERE id = ?", (host_id,))
        host = await h_cursor.fetchone()
        if not host:
            raise HTTPException(status_code=404, detail="Host not found")

    target = host["target"]
    hops = await run_traceroute(target, max_hops=18)
    return {
        "host_id": host_id,
        "name": host["name"],
        "target": target,
        "hops": hops
    }

# Static frontend files mount
if os.path.exists(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
