"""Read-only TCP/UDP snapshot for explicitly selected PIDs (stdlib only).

Usage: python scripts/check_connections.py --pid 1234 [--pid 5678] [--include-children]
Exit 0 means collection completed, NOT that traffic is safe; exit 1 means partial
visibility/error. No DNS lookups, packet capture, firewall changes or command-line
collection. Explicit selection also supports project servers started separately.
Windows netstat enumerates system sockets internally; only selected rows enter
the report. Linux reads selected socket FDs and namespace tables via /proc.
"""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys

LIMITATIONS = [
    "Snapshot only; not proof that no outbound traffic ever occurred.",
    "TCP/UDP only; no traffic history, payloads, DNS names or connection direction inference.",
    "Process exits, PID reuse, permissions and namespace changes can race collection.",
    "Detached services and new descendants outside the selected snapshot are not covered.",
]


def classify(host):
    if host == "*":
        return "unspecified"
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return "unknown"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    for attr, label in (("is_unspecified", "unspecified"), ("is_loopback", "loopback"),
                        ("is_link_local", "link_local"), ("is_multicast", "multicast")):
        if getattr(address, attr):
            return label
    networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
    if any(address in ipaddress.ip_network(n) for n in networks):
        return "private"
    return "external"


def endpoint(value):
    host, _, port = value.rpartition(":")
    host = host.strip("[]")
    return {"address": host, "port": int(port) if port.isdigit() else None,
            "classification": classify(host)}


def parse_netstat(text, pids):
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("TCP", "UDP") or not parts[-1].isdigit():
            continue
        if int(parts[-1]) not in pids:
            continue
        if len(parts) != (5 if parts[0] == "TCP" else 4):
            raise ValueError("Unrecognized selected netstat row")
        rows.append({"pid": int(parts[-1]), "protocol": parts[0].lower(),
                     "local": endpoint(parts[1]), "remote": endpoint(parts[2]),
                     "state": parts[3] if parts[0] == "TCP" else "UNCONNECTED"})
    return rows


def process_parents():
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Entry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                        ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                        ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                        ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
                        ("flags", wintypes.DWORD), ("exe", wintypes.WCHAR * 260)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        for name in ("Process32FirstW", "Process32NextW"):
            fn = getattr(kernel, name)
            fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
            fn.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateToolhelp32Snapshot(2, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        parents = {}
        try:
            entry = Entry()
            entry.size = ctypes.sizeof(entry)
            ok = kernel.Process32FirstW(handle, ctypes.byref(entry))
            while ok:
                parents[entry.pid] = entry.parent
                ok = kernel.Process32NextW(handle, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(handle)
        return parents
    parents = {}
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            try:
                # comm can contain spaces and parentheses.
                tail = (path / "stat").read_text().rsplit(")", 1)[1].split()
                parents[int(path.name)] = int(tail[1])
            except FileNotFoundError:
                continue
    return parents


def select_pids(roots, parents, include_children):
    missing = set(roots) - parents.keys()
    if missing:
        raise ValueError("Selected PID unavailable: " + ", ".join(map(str, sorted(missing))))
    selected = set(roots)
    if include_children:
        while True:
            children = {pid for pid, parent in parents.items() if parent in selected}
            if children <= selected:
                break
            selected.update(children)
    return selected


def proc_endpoint(value):
    address, port = value.split(":")
    data = bytes.fromhex(address)
    # /proc stores each 32-bit word in host byte order (IPv4 and IPv6).
    packed = b"".join(struct.pack("!I", word) for word in struct.unpack("=" + "I" * (len(data) // 4), data))
    host = socket.inet_ntop(socket.AF_INET if len(data) == 4 else socket.AF_INET6, packed)
    return endpoint(f"[{host}]:{int(port, 16)}")


TCP_STATES = {"01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1",
              "05": "FIN_WAIT2", "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT",
              "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING", "0C": "NEW_SYN_RECV"}


def linux_connections(pids, errors):
    rows = []
    for pid in sorted(pids):
        base = Path("/proc") / str(pid)
        try:
            inodes = set()
            for fd in (base / "fd").iterdir():
                try:
                    link = os.readlink(fd)
                except FileNotFoundError:
                    errors.append(f"PID {pid}: file descriptor disappeared during snapshot")
                    continue
                if link.startswith("socket:["):
                    inodes.add(link[8:-1])
            for protocol in ("tcp", "tcp6", "udp", "udp6"):
                table = base / "net" / protocol
                if not table.exists() and protocol.endswith("6"):
                    continue
                for line in table.read_text().splitlines()[1:]:
                    fields = line.split()
                    if len(fields) < 10:
                        raise ValueError("Malformed /proc socket table")
                    if fields[9] in inodes:
                        rows.append({"pid": pid, "protocol": protocol, "local": proc_endpoint(fields[1]),
                                     "remote": proc_endpoint(fields[2]),
                                     "state": TCP_STATES.get(fields[3], fields[3])
                                     if protocol.startswith("tcp") else fields[3]})
        except (OSError, ValueError) as exc:
            errors.append(f"PID {pid}: {type(exc).__name__}: {exc}")
    return rows


def snapshot(roots, include_children=False):
    report = {"schema_version": 1, "kind": "network_snapshot", "started_at": datetime.now(timezone.utc).isoformat(),
              "platform": sys.platform, "requested_pids": sorted(set(roots)), "selected_pids": [],
              "include_children": include_children, "complete": False, "connections": [],
              "errors": [], "limitations": LIMITATIONS,
              "classification_note": "Private means RFC1918/IPv6 ULA; external means outside local scopes, not necessarily publicly routable or outbound."}
    try:
        if not roots or any(type(pid) is not int or pid <= 0 for pid in roots):
            raise ValueError("Explicit positive PIDs are required")
        if sys.platform != "win32" and not sys.platform.startswith("linux"):
            raise ValueError("Supported platforms: Windows and Linux")
        selected = select_pids(roots, process_parents(), include_children)
        report["selected_pids"] = sorted(selected)
        if sys.platform == "win32":
            report["backend"] = "netstat -ano -n"
            result = subprocess.run(["netstat", "-ano", "-n"], capture_output=True, text=True,
                                    errors="replace", timeout=30, check=True)
            report["connections"] = parse_netstat(result.stdout, selected)
            if not any(line.split() and line.split()[0] in ("TCP", "UDP")
                       for line in result.stdout.splitlines()):
                report["errors"].append("netstat returned no socket rows; visibility cannot be verified")
            report["limitations"] = [*LIMITATIONS, "Windows netstat visibility depends on OS permissions; an empty result is not an absence guarantee."]
        else:
            report["backend"] = "proc"
            report["connections"] = linux_connections(selected, report["errors"])
        report["complete"] = not report["errors"]
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # Do not leak command output containing unrelated system socket rows.
        report["errors"].append(f"Snapshot collection failed: {type(exc).__name__}")
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["external_remote_count"] = sum(row["remote"]["classification"] == "external"
                                            for row in report["connections"])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, action="append", required=True, help="Explicit PID; repeat for separately launched project processes")
    parser.add_argument("--include-children", action="store_true")
    args = parser.parse_args(argv)
    report = snapshot(args.pid, args.include_children)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
