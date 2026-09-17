import asyncio
import re
from typing import List, Dict, Any, Optional

HOP_REGEX = re.compile(
    r'^\s*(\d+)\s+([^\s\(]+)?\s*(?:\(([\d\.]+)\))?\s+([\d\.\*ms\s]+)',
    re.MULTILINE
)
TIME_REGEX = re.compile(r'([\d\.]+)\s*ms')

async def run_traceroute(target: str, max_hops: int = 15, timeout_sec: int = 1) -> List[Dict[str, Any]]:
    """
    Runs macOS traceroute with ICMP (-I) which works without root on modern macOS.
    Parses output into structured hops with hop_number, hostname, ip, latencies, and avg_ms.
    """
    cmd = ["traceroute", "-I", "-m", str(max_hops), "-q", "2", "-w", str(timeout_sec), target]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=35.0)
        output = stdout.decode("utf-8", errors="replace")
    except Exception as e:
        return [{"hop": 1, "host": "Ошибка", "ip": target, "times": [], "avg_ms": None, "error": str(e)}]

    hops = []
    lines = output.splitlines()

    for line in lines:
        line_s = line.strip()
        if not line_s or line_s.startswith("traceroute"):
            continue

        parts = line_s.split()
        if not parts or not parts[0].isdigit():
            continue

        hop_num = int(parts[0])
        
        # Check if all timed out "*"
        if all(p == '*' for p in parts[1:]):
            hops.append({
                "hop": hop_num,
                "host": "*",
                "ip": "*",
                "times": [],
                "avg_ms": None,
                "status": "timeout"
            })
            continue

        # Extract times
        times = [float(m) for m in TIME_REGEX.findall(line_s)]
        avg_ms = round(sum(times) / len(times), 2) if times else None

        # Extract host and IP
        host_name = None
        ip_addr = None

        ip_match = re.search(r'\(([\d\.]+)\)', line_s)
        if ip_match:
            ip_addr = ip_match.group(1)
            idx = parts[1].strip("()")
            if idx != ip_addr:
                host_name = parts[1]
            else:
                host_name = ip_addr
        else:
            # Maybe pure IP without brackets
            for p in parts[1:]:
                if re.match(r'^\d+\.\d+\.\d+\.\d+$', p):
                    ip_addr = p
                    break
            host_name = parts[1] if len(parts) > 1 else ip_addr

        hops.append({
            "hop": hop_num,
            "host": host_name or ip_addr or "*",
            "ip": ip_addr or host_name or "*",
            "times": times,
            "avg_ms": avg_ms,
            "status": "ok" if times else "timeout"
        })

    return hops
