#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║       LAPTOP SPEC EXTRACTOR  v5  —  by Claude        ║
║  Drop your DxDiag .txt or .docx → get a sick HTML    ║
╚══════════════════════════════════════════════════════╝

Fixes in v5:
  • GPU role detection: NVIDIA/AMD → Dedicated (Primary), Intel → Integrated (Secondary)
  • Ports & I/O tab — dynamically detected from DxDiag device lists
  • Performance tab — CPU details, benchmarks, thermal
  • GPU cards now always side-by-side with correct labels
  • System tab: detailed CPU info (logical CPUs, GHz, cache)
  • Improved GPU block splitting reliability

Run:  python laptop_extractor_v5.py
Then open your browser at http://localhost:7474
"""

import http.server
import threading
import webbrowser
import json
import re
import io
import time
import pathlib
import urllib.parse
import traceback
from datetime import datetime

# ─────────────────────────────────────────────
#  PARSER  — reads DxDiag text or docx
# ─────────────────────────────────────────────

def _gpu_brand(name: str) -> str:
    n = name.lower()
    if "nvidia" in n: return "NVIDIA"
    if "intel"  in n: return "Intel"
    if "amd"    in n: return "AMD"
    if "radeon" in n: return "AMD"
    return "GPU"

def _is_discrete_gpu(name: str) -> bool:
    """Returns True if this GPU is likely a discrete/dedicated GPU."""
    n = name.lower()
    # Dedicated GPUs
    if "nvidia" in n: return True
    if "geforce" in n: return True
    if "quadro" in n: return True
    if "rtx" in n: return True
    if "gtx" in n: return True
    if "radeon rx" in n: return True
    if "rx 6" in n or "rx 7" in n or "rx 5" in n: return True
    # Integrated GPUs
    if "intel" in n: return False
    if "uhd" in n: return False
    if "iris" in n: return False
    if "vega" in n and "radeon" not in n: return False
    return False  # default to integrated if unknown

def _extract_gpu_fields(src: str) -> dict:
    """Pull every known GPU field out of a single GPU block of text."""
    def f(pat, default="N/A"):
        m = re.search(pat, src, re.IGNORECASE | re.MULTILINE)
        return m.group(1).strip() if m else default

    # CPU count / GHz — sometimes appear near GPU blocks; skip
    return {
        "name":           f(r"Card name:\s*(.+)"),
        "manufacturer":   f(r"Manufacturer:\s*(.+)"),
        "chip_type":      f(r"Chip type:\s*(.+)"),
        "dac_type":       f(r"DAC type:\s*(.+)"),
        "device_type":    f(r"Device Type:\s*(.+)"),
        "display_mem":    f(r"Display Memory:\s*(.+)"),
        "dedicated_mem":  f(r"Dedicated Memory:\s*(.+)"),
        "shared_mem":     f(r"Shared Memory:\s*(.+)"),
        "current_mode":   f(r"Current Mode:\s*(.+)"),
        "monitor_id":     f(r"Monitor Id:\s*(.+)"),
        "monitor_name":   f(r"Monitor Name:\s*(.+)"),
        "native_mode":    f(r"Native Mode:\s*(.+)"),
        "driver_version": f(r"Driver Version:\s*(.+)"),
        "driver_date":    f(r"Driver Date/Size:\s*(.+)"),
        "wddm":           f(r"Driver Model:\s*(.+)"),
        "vendor_id":      f(r"Vendor ID:\s*(.+)"),
        "device_id":      f(r"Device ID:\s*(.+)"),
        "subsys_id":      f(r"SubSys ID:\s*(.+)"),
        "revision":       f(r"Revision:\s*(.+)"),
        "hw_scheduling":  f(r"Hardware Scheduling:\s*(.+)"),
        "hybrid_gpu":     f(r"Hybrid Graphics GPU:\s*(.+)"),
        "hdr":            f(r"HDR Support:\s*(.+)"),
        "color_space":    f(r"Display Color Space:\s*(.+)"),
        "wcg":            f(r"WCG:\s*(.+)"),
        "advanced_color": f(r"Active Color Mode:\s*(.+)"),
        "output_type":    f(r"Output Type:\s*(.+)"),
        "feature_levels": f(r"Feature Levels:\s*(.+)"),
        "dx_accel":       f(r"DirectX Acceleration:\s*(.+)"),
    }


def _split_gpu_blocks(text: str):
    """
    Robustly split DxDiag text into one block per GPU.
    Uses three strategies in order of reliability.
    """
    # Strategy 1 — split on "Display Devices" section header + dashes line
    blocks = re.split(
        r"(?=^\s*Display Devices\s*\r?\n\s*-)",
        text, flags=re.IGNORECASE | re.MULTILINE
    )
    blocks = [b for b in blocks if re.search(r"Card name:", b, re.IGNORECASE)]
    if len(blocks) >= 2:
        return blocks

    # Strategy 2 — split on every occurrence of "Card name:" line
    positions = [m.start() for m in re.finditer(
        r"(?:^|\n)[ \t]*Card name:", text, re.IGNORECASE
    )]
    if len(positions) >= 2:
        slices = []
        for i, pos in enumerate(positions):
            end = positions[i + 1] if i + 1 < len(positions) else len(text)
            slices.append(text[pos:end])
        return slices

    # Strategy 3 — whole text is one block (single GPU or unusual format)
    if re.search(r"Card name:", text, re.IGNORECASE):
        return [text]
    return []


def _extract_devices(text: str) -> dict:
    """Extract device lists from DxDiag (sound, input, USB, network)."""
    devices = {
        "sound": [],
        "input": [],
        "usb": [],
        "network": [],
    }

    # Sound devices
    sound_section = re.search(
        r"Sound Devices\s*[-]+(.+?)(?=\n[ \t]*[-]{5,}|\Z)",
        text, re.IGNORECASE | re.DOTALL
    )
    if sound_section:
        for m in re.finditer(r"Description:\s*(.+)", sound_section.group(1), re.IGNORECASE):
            val = m.group(1).strip()
            if val and val not in devices["sound"]:
                devices["sound"].append(val)

    # Input devices
    input_section = re.search(
        r"Input Related Devices\s*[-]+(.+?)(?=\n[ \t]*[-]{5,}|\Z)",
        text, re.IGNORECASE | re.DOTALL
    )
    if input_section:
        for m in re.finditer(r"Device Name:\s*(.+)", input_section.group(1), re.IGNORECASE):
            val = m.group(1).strip()
            if val and val not in devices["input"]:
                devices["input"].append(val)

    # USB — look for USB controller mentions anywhere
    for m in re.finditer(r"(USB[\s\w\.\-]+Host Controller[^\n]*|USB[\s\d\.]+eXtensible[^\n]*)", text, re.IGNORECASE):
        val = m.group(1).strip()
        if val not in devices["usb"]:
            devices["usb"].append(val)

    # Network — look for "Driver" lines near "Network" sections
    net_section = re.search(
        r"(Network Devices|DirectPlay Registered Service Providers).{0,200}",
        text, re.IGNORECASE | re.DOTALL
    )
    if net_section:
        for m in re.finditer(r"Description:\s*(.+)", net_section.group(0), re.IGNORECASE):
            val = m.group(1).strip()
            if val and val not in devices["network"]:
                devices["network"].append(val)

    return devices


def _detect_ports_from_model(model: str, processor: str) -> dict:
    """
    Detect likely ports based on model name and processor.
    Returns a dict with left/right side port lists.
    Falls back to generic gaming laptop spec if unknown.
    """
    m = model.lower()
    p = processor.lower()

    ports = {
        "left": [],
        "right": [],
        "wireless": [],
        "note": "⚠ Port layout is estimated from the model database — actual ports may differ by sub-variant or region. Verify with your laptop's official spec sheet."
    }

    # Acer ALG AL15G series
    if "al15g" in m:
        ports["left"] = [
            ("🔌", "amber", "DC-in Jack", "120W AC Adapter"),
            ("🌐", "green", "RJ-45 Ethernet", "Gigabit LAN · Low-latency wired"),
            ("🖥️", "cyan", "HDMI 1.4", "Up to 4K@30Hz · Audio + Video"),
            ("⚡", "green", "USB 3.2 Gen 2 Type-C × 2", "10 Gbps · DisplayPort Alt · Power Delivery"),
        ]
        ports["right"] = [
            ("💻", "cyan", "USB 3.2 Gen 1 Type-A", "5 Gbps · SuperSpeed USB"),
            ("🖱️", "amber", "USB 2.0 Type-A × 2", "480 Mbps · Peripherals"),
            ("🎧", "purple", "3.5mm Combo Audio", "Headphone + Microphone combo"),
            ("🔒", "muted", "Kensington Lock Slot", "Physical security"),
        ]
        ports["wireless"] = ["Wi-Fi 6 (Intel AX201)", "Bluetooth 5.1", "Miracast (HDCP)"]
        ports["summary"] = {"USB-C": "2", "USB-A": "3", "HDMI": "1", "Audio": "1", "LAN": "1"}
        return ports

    # Acer Nitro 5
    if "nitro" in m and "5" in m:
        ports["left"] = [
            ("🔌", "amber", "DC-in Jack", "135W/180W AC Adapter"),
            ("🌐", "green", "RJ-45 Ethernet", "Gigabit LAN"),
            ("🖥️", "cyan", "HDMI 2.0", "Up to 4K@60Hz"),
            ("⚡", "green", "USB 3.2 Gen 1 Type-C", "5 Gbps · DisplayPort"),
        ]
        ports["right"] = [
            ("💻", "cyan", "USB 3.2 Gen 1 Type-A × 3", "5 Gbps each"),
            ("🎧", "purple", "3.5mm Combo Audio", "Headphone + Mic"),
            ("🔒", "muted", "Kensington Lock Slot", "Physical security"),
        ]
        ports["wireless"] = ["Wi-Fi 6 (802.11ax)", "Bluetooth 5.1"]
        ports["summary"] = {"USB-C": "1", "USB-A": "3", "HDMI": "1", "Audio": "1", "LAN": "1"}
        return ports

    # ASUS ROG / TUF
    if "rog" in m or "tuf" in m:
        ports["left"] = [
            ("🔌", "amber", "DC-in Jack", "240W AC Adapter"),
            ("🖥️", "cyan", "HDMI 2.0b", "4K@120Hz"),
            ("🔵", "cyan", "Thunderbolt 4 / USB-C", "40 Gbps · DP 1.4 · PD"),
        ]
        ports["right"] = [
            ("💻", "cyan", "USB 3.2 Gen 1 Type-A × 2", "5 Gbps"),
            ("💻", "green", "USB 2.0 Type-A", "Peripheral use"),
            ("🎧", "purple", "3.5mm Combo Audio", "Headphone + Mic"),
            ("🔒", "muted", "Kensington Lock Slot", "Physical security"),
        ]
        ports["wireless"] = ["Wi-Fi 6E (802.11ax)", "Bluetooth 5.2"]
        ports["summary"] = {"USB-C": "1", "USB-A": "3", "HDMI": "1", "Audio": "1", "TB4": "1"}
        return ports

    # MSI Gaming
    if "msi" in m or "gf" in m or "gp" in m:
        ports["left"] = [
            ("🔌", "amber", "DC-in Jack", "180W AC Adapter"),
            ("🌐", "green", "RJ-45 Ethernet", "Killer Gigabit LAN"),
            ("🖥️", "cyan", "HDMI 2.0", "4K@60Hz"),
            ("⚡", "green", "USB-C (Gen 2)", "10 Gbps · DisplayPort"),
        ]
        ports["right"] = [
            ("💻", "cyan", "USB 3.2 Gen 1 Type-A × 3", "5 Gbps each"),
            ("🎧", "purple", "3.5mm Combo Audio", "Mic-in + Headphone"),
            ("🔒", "muted", "Kensington Lock Slot", "Security"),
        ]
        ports["wireless"] = ["Wi-Fi 6 (Killer AX1650)", "Bluetooth 5.1"]
        ports["summary"] = {"USB-C": "1", "USB-A": "3", "HDMI": "1", "Audio": "1", "LAN": "1"}
        return ports

    # Lenovo Legion
    if "legion" in m:
        ports["left"] = [
            ("🔌", "amber", "DC-in Jack", "170W AC Adapter"),
            ("🌐", "green", "RJ-45 Ethernet", "Gigabit LAN"),
            ("🖥️", "cyan", "HDMI 2.1", "4K@120Hz"),
            ("🔵", "cyan", "Thunderbolt 4 Type-C", "40 Gbps · DP 1.4"),
            ("⚡", "green", "USB 3.2 Gen 2 Type-C", "10 Gbps"),
        ]
        ports["right"] = [
            ("💻", "cyan", "USB 3.2 Gen 1 Type-A × 4", "5 Gbps each"),
            ("🎧", "purple", "3.5mm Combo Audio", "Combo Jack"),
            ("🔒", "muted", "Kensington Lock", "Security"),
        ]
        ports["wireless"] = ["Wi-Fi 6E (802.11ax)", "Bluetooth 5.1"]
        ports["summary"] = {"USB-C": "2", "USB-A": "4", "HDMI": "1", "TB4": "1", "Audio": "1"}
        return ports

    # Dell G-series / Inspiron Gaming
    if "dell" in m or "inspiron" in m or "vostro" in m or "latitude" in m:
        ports["left"] = [
            ("🔌", "amber", "DC-in Jack", "130W AC Adapter"),
            ("🌐", "green", "RJ-45 Ethernet", "Gigabit LAN"),
            ("🖥️", "cyan", "HDMI 1.4", "External Display"),
            ("⚡", "green", "USB-C Gen 2", "10 Gbps"),
        ]
        ports["right"] = [
            ("💻", "cyan", "USB 3.2 Type-A × 2", "5 Gbps"),
            ("💻", "amber", "USB 2.0 Type-A", "Peripheral"),
            ("🎧", "purple", "3.5mm Audio Jack", "Combo Jack"),
            ("🔒", "muted", "Noble Lock Slot", "Security"),
        ]
        ports["wireless"] = ["Wi-Fi 5 / 6 (Intel)", "Bluetooth 5.0/5.1"]
        ports["summary"] = {"USB-C": "1", "USB-A": "3", "HDMI": "1", "Audio": "1", "LAN": "1"}
        return ports

    # HP Pavilion / OMEN
    if "hp" in m or "omen" in m or "pavilion" in m or "victus" in m:
        ports["left"] = [
            ("🔌", "amber", "DC-in Jack", "150W AC Adapter"),
            ("🌐", "green", "RJ-45 Ethernet", "Gigabit LAN"),
            ("🖥️", "cyan", "HDMI 2.0", "4K@60Hz"),
            ("⚡", "green", "USB-C Gen 2 × 2", "10 Gbps · DP"),
        ]
        ports["right"] = [
            ("💻", "cyan", "USB 3.2 Type-A × 2", "5 Gbps"),
            ("💻", "amber", "USB 2.0 Type-A", "Peripheral"),
            ("🎧", "purple", "3.5mm Combo Jack", "Headphone + Mic"),
            ("🔒", "muted", "Security Slot", "Lock-compatible"),
        ]
        ports["wireless"] = ["Wi-Fi 6 (Intel AX200)", "Bluetooth 5.0"]
        ports["summary"] = {"USB-C": "2", "USB-A": "3", "HDMI": "1", "Audio": "1", "LAN": "1"}
        return ports

    # Generic gaming laptop fallback — show only what virtually all laptops have, with a clear warning
    ports["left"] = [
        ("🔌", "amber", "DC-in Jack", "AC Power Adapter"),
        ("🖥️", "cyan", "HDMI", "External Display"),
    ]
    ports["right"] = [
        ("💻", "cyan", "USB Type-A", "USB port(s)"),
        ("🎧", "purple", "3.5mm Audio Jack", "Combo Jack"),
    ]
    ports["wireless"] = ["Wi-Fi (see DxDiag network section)", "Bluetooth"]
    ports["summary"] = {"USB-A": "?", "HDMI": "?", "Audio": "?"}
    ports["note"] = "⚠ Model not in database — port layout is NOT confirmed. Verify all port types and counts with your laptop's official spec sheet."
    return ports


def _extract_cpu_details(processor_str: str) -> dict:
    """Parse CPU details from the processor string."""
    cpu = {"raw": processor_str, "brand": "N/A", "model": "N/A",
           "gen": "N/A", "logical": "N/A", "ghz": "N/A", "boost": "N/A", "cache": "N/A"}

    # Brand
    if "intel" in processor_str.lower():
        cpu["brand"] = "Intel"
    elif "amd" in processor_str.lower():
        cpu["brand"] = "AMD"

    # Core model
    m = re.search(r"(Core(?:\(TM\))?\s+i\d[-\w]+|Ryzen\s+\d+\s+\w+|Core\s+Ultra\s+\d+\s+\w+)", processor_str, re.IGNORECASE)
    if m:
        cpu["model"] = m.group(1).replace("(TM)", "").replace("(R)", "").strip()

    # Generation (Intel)
    gen_m = re.search(r"(\d+)th Gen", processor_str, re.IGNORECASE)
    if gen_m:
        cpu["gen"] = gen_m.group(1) + "th Gen"

    # Logical CPU count
    lc_m = re.search(r"\((\d+)\s*CPUs?\)", processor_str, re.IGNORECASE)
    if lc_m:
        cpu["logical"] = lc_m.group(1)

    # Base GHz
    ghz_m = re.search(r"~?([\d.]+)\s*GHz", processor_str, re.IGNORECASE)
    if ghz_m:
        cpu["ghz"] = ghz_m.group(1) + " GHz"

    # Known boost speeds (approximate from well-known models)
    known_boost = {
        "i5-13500H": "4.7 GHz", "i7-13620H": "4.9 GHz", "i9-13900H": "5.4 GHz",
        "i5-12500H": "4.5 GHz", "i7-12700H": "4.7 GHz",
        "i5-11400H": "4.5 GHz", "i7-11800H": "4.6 GHz",
        "Ryzen 5 6600H": "4.5 GHz", "Ryzen 7 6800H": "4.7 GHz",
        "Ryzen 9 6900HX": "4.9 GHz",
    }
    for k, v in known_boost.items():
        if k.lower() in processor_str.lower():
            cpu["boost"] = v
            break

    # Known cache
    known_cache = {
        "i5-13500H": "18 MB", "i7-13620H": "24 MB", "i9-13900H": "24 MB",
        "i5-12500H": "18 MB", "i7-12700H": "24 MB",
        "i5-11400H": "12 MB", "i7-11800H": "24 MB",
        "Ryzen 5 6600H": "16 MB", "Ryzen 7 6800H": "16 MB",
        "Ryzen 9 6900HX": "16 MB",
    }
    for k, v in known_cache.items():
        if k.lower() in processor_str.lower():
            cpu["cache"] = v
            break

    # Architecture
    arch_map = {
        "13th Gen": "Raptor Lake",
        "12th Gen": "Alder Lake",
        "11th Gen": "Tiger Lake",
        "10th Gen": "Comet Lake",
        "Ryzen 6": "Rembrandt",
        "Ryzen 7": "Cezanne / Rembrandt",
    }
    cpu["arch"] = "N/A"
    for k, v in arch_map.items():
        if k.lower() in processor_str.lower():
            cpu["arch"] = v
            break

    return cpu


def _detect_ddr_type(text: str, processor: str) -> tuple:
    """
    Detect RAM DDR generation strictly from the DxDiag text.
    Returns (ddr_string, is_detected).
    is_detected=True  -> found explicitly in the DxDiag file (trustworthy).
    is_detected=False -> not found in text; caller should show "Not detected".

    CPU-generation heuristics are intentionally NOT used: the same CPU model
    (e.g. i7-13700H) ships with DDR4 or DDR5 depending on the OEM board,
    so any guess based on CPU alone is frequently wrong.
    """
    # Search for explicit DDR keyword anywhere in the DxDiag text.
    # Order matters: match the most specific variants first so LPDDR5X
    # is not swallowed by the shorter DDR5 pattern.
    ddr_m = re.search(
        r'\b(LPDDR5X|LPDDR5|LPDDR4X|LPDDR4|DDR5|DDR4|DDR3L|DDR3|DDR2|DDR)\b',
        text, re.IGNORECASE
    )
    if ddr_m:
        return ddr_m.group(1).upper(), True

    # Not found in text - return sentinel so the UI shows a neutral label.
    return 'Unknown', False


def parse_dxdiag(text: str) -> dict:
    """Extract all useful fields from DxDiag output."""
    data = {}

    def g(pattern, default="N/A"):
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        return m.group(1).strip() if m else default

    # ── System fields ──────────────────────────────────────────────────────────
    data["machine_name"]  = g(r"Machine name:\s*(.+)")
    data["os"]            = g(r"Operating System:\s*(.+)")
    data["manufacturer"]  = g(r"System Manufacturer:\s*(.+)")
    data["model"]         = g(r"System Model:\s*(.+)")
    data["bios"]          = g(r"BIOS:\s*(.+)")
    data["processor"]     = g(r"Processor:\s*(.+)")
    data["memory"]        = g(r"Memory:\s*(\d+MB RAM)")
    data["available_mem"] = g(r"Available OS Memory:\s*(.+)")
    data["directx"]       = g(r"DirectX Version:\s*(.+)")
    data["dx_db"]         = g(r"DirectX Database Version:\s*(.+)")
    data["report_time"]   = g(r"Time of this report:\s*(.+)")
    data["miracast"]      = g(r"Miracast:\s*(.+)")
    data["page_file"]     = g(r"Page File:\s*(.+)")
    data["dpi"]           = g(r"User DPI Setting:\s*(.+)")
    data["windows_dir"]   = g(r"Windows Dir:\s*(.+)")
    data["machine_id"]    = g(r"Machine Id:\s*(.+)")
    data["mux_support"]   = g(r"System Mux Support:\s*(.+)")
    data["mux_target"]    = g(r"Mux Target GPU:\s*(.+)")
    data["dxdiag_ver"]    = g(r"DxDiag Version:\s*(.+)")

    # ── CPU Details ────────────────────────────────────────────────────────────
    data["_cpu"] = _extract_cpu_details(data["processor"])
    data["cpu_logical"] = data["_cpu"]["logical"]
    data["cpu_ghz"]     = data["_cpu"]["ghz"]
    data["cpu_model"]   = data["_cpu"]["model"]
    data["cpu_gen"]     = data["_cpu"]["gen"]
    data["cpu_arch"]    = data["_cpu"]["arch"]
    data["cpu_boost"]   = data["_cpu"]["boost"]
    data["cpu_cache"]   = data["_cpu"]["cache"]

    # ── GPU blocks ─────────────────────────────────────────────────────────────
    gpu_blocks = _split_gpu_blocks(text)
    raw_gpus = [_extract_gpu_fields(b) for b in gpu_blocks]

    # Sort: discrete first, integrated second
    discrete = [g for g in raw_gpus if _is_discrete_gpu(g.get("name", ""))]
    integrated = [g for g in raw_gpus if not _is_discrete_gpu(g.get("name", ""))]

    # Primary = dedicated, Secondary = integrated
    if discrete and integrated:
        gpu1 = discrete[0]
        gpu2 = integrated[0]
        data["_gpu2_found"] = True
        data["_gpu1_is_discrete"] = True
    elif discrete:
        gpu1 = discrete[0]
        gpu2 = {}
        data["_gpu2_found"] = False
        data["_gpu1_is_discrete"] = True
    elif integrated:
        # Only an integrated GPU found — label it correctly, do NOT call it dedicated
        gpu1 = integrated[0]
        gpu2 = {}
        data["_gpu2_found"] = False
        data["_gpu1_is_discrete"] = False
    else:
        gpu1 = {}
        gpu2 = {}
        data["_gpu2_found"] = False
        data["_gpu1_is_discrete"] = False

    data["_gpu1"] = gpu1
    data["_gpu2"] = gpu2

    # Flat aliases
    data["gpu1_name"]      = gpu1.get("name", "N/A")
    data["gpu2_name"]      = gpu2.get("name", "N/A") if data["_gpu2_found"] else "N/A"
    data["display_mem"]    = gpu1.get("display_mem", "N/A")
    data["dedicated_mem"]  = gpu1.get("dedicated_mem", "N/A")
    data["shared_mem"]     = gpu1.get("shared_mem", "N/A")
    data["current_mode"]   = gpu1.get("current_mode", "N/A")
    data["monitor_id"]     = gpu1.get("monitor_id", "N/A")
    data["monitor_name"]   = gpu1.get("monitor_name", "N/A")
    data["native_mode"]    = gpu1.get("native_mode", "N/A")
    data["driver_version"] = gpu1.get("driver_version", "N/A")
    data["driver_date"]    = gpu1.get("driver_date", "N/A")
    data["wddm"]           = gpu1.get("wddm", "N/A")
    data["vendor_id"]      = gpu1.get("vendor_id", "N/A")
    data["device_id"]      = gpu1.get("device_id", "N/A")
    data["hw_scheduling"]  = gpu1.get("hw_scheduling", "N/A")
    data["hybrid_gpu"]     = gpu1.get("hybrid_gpu", "N/A")
    data["hdr"]            = gpu1.get("hdr", "N/A")
    data["color_space"]    = gpu1.get("color_space", "N/A")
    data["wcg"]            = gpu1.get("wcg", "N/A")
    data["advanced_color"] = gpu1.get("advanced_color", "N/A")
    data["output_type"]    = gpu1.get("output_type", "N/A")

    # ── Resolution / refresh ───────────────────────────────────────────────────
    # Try primary GPU first, then secondary
    for mode_src in [data["current_mode"], data["native_mode"],
                     gpu2.get("current_mode","N/A"), gpu2.get("native_mode","N/A")]:
        mode_m = re.search(r"(\d{3,4})\s*x\s*(\d{3,4})\s*\(32 bit\)\s*\((\d+)Hz\)", mode_src)
        if mode_m:
            data["res_w"], data["res_h"], data["refresh"] = mode_m.group(1), mode_m.group(2), mode_m.group(3)
            break
        native_m = re.search(r"(\d{3,4})\s*x\s*(\d{3,4})\(p\)\s*\((\d+)", mode_src)
        if native_m:
            data["res_w"], data["res_h"], data["refresh"] = native_m.group(1), native_m.group(2), native_m.group(3)
            break
    else:
        data["res_w"], data["res_h"], data["refresh"] = "1920", "1080", "60"

    # ── RAM ────────────────────────────────────────────────────────────────────
    ram_m = re.search(r"(\d+)MB RAM", data["memory"], re.IGNORECASE)
    if ram_m:
        mb = int(ram_m.group(1))
        data["ram_gb"] = f"{mb // 1024} GB" if mb >= 1024 else f"{mb} MB"
    else:
        data["ram_gb"] = data["memory"]

    # DDR type detection
    ddr_type, ddr_confirmed = _detect_ddr_type(text, data["processor"])
    data["ram_ddr"]           = ddr_type
    data["ram_ddr_confirmed"] = ddr_confirmed          # True = found in text, False = estimated

    # ── Display size ───────────────────────────────────────────────────────────
    data["display_size"] = "15.6\""
    size_m = re.search(r"(\d{2}[\.,]\d)\s*(?:inch|\")", data["model"], re.IGNORECASE)
    if size_m:
        data["display_size"] = f"{size_m.group(1)}\""

    # ── DxDiag notes ──────────────────────────────────────────────────────────
    notes = re.findall(r"(Display Tab \d+:.*?)\n", text, re.IGNORECASE)
    data["dxdiag_notes"] = " | ".join(n.strip() for n in notes) if notes else "No issues found"

    # ── Device lists ──────────────────────────────────────────────────────────
    data["_devices"] = _extract_devices(text)

    # ── Port data ─────────────────────────────────────────────────────────────
    data["_ports"] = _detect_ports_from_model(data["model"], data["processor"])

    return data


def read_file(filepath: str) -> str:
    """Read .txt or .docx and return plain text."""
    ext = pathlib.Path(filepath).suffix.lower()
    if ext == ".docx":
        try:
            from docx import Document
            doc = Document(filepath)
            return "\n".join(p.text for p in doc.paragraphs)
        except ImportError:
            raise RuntimeError("python-docx not installed. Run: pip install python-docx")
    else:
        for enc in ("utf-8", "utf-16", "latin-1"):
            try:
                with open(filepath, "r", encoding=enc) as f:
                    return f.read()
            except Exception:
                continue
        raise RuntimeError("Could not read file — try saving as UTF-8.")


# ─────────────────────────────────────────────
#  GPU CARD HTML HELPER
# ─────────────────────────────────────────────

def _gpu_card_rows(g: dict) -> str:
    """Render all spec-rows for one GPU dict."""
    fields = [
        ("Manufacturer",    g.get("manufacturer","N/A")),
        ("Chip Type",       g.get("chip_type","N/A")),
        ("DAC Type",        g.get("dac_type","N/A")),
        ("Device Type",     g.get("device_type","N/A")),
        ("Display Memory",  g.get("display_mem","N/A")),
        ("Dedicated VRAM",  g.get("dedicated_mem","N/A")),
        ("Shared Memory",   g.get("shared_mem","N/A")),
        ("Driver Version",  g.get("driver_version","N/A")),
        ("Driver Date",     g.get("driver_date","N/A")[:40] if g.get("driver_date") else "N/A"),
        ("WDDM Model",      g.get("wddm","N/A")),
        ("Feature Levels",  g.get("feature_levels","N/A")[:38] if g.get("feature_levels") else "N/A"),
        ("DX Accel.",       g.get("dx_accel","N/A")),
        ("Vendor ID",       g.get("vendor_id","N/A")),
        ("Device ID",       g.get("device_id","N/A")),
        ("SubSys ID",       g.get("subsys_id","N/A")),
        ("Revision",        g.get("revision","N/A")),
        ("HW Scheduling",   g.get("hw_scheduling","N/A")[:32] if g.get("hw_scheduling") else "N/A"),
        ("Hybrid GPU Role", g.get("hybrid_gpu","N/A")),
        ("HDR Support",     g.get("hdr","N/A")),
        ("WCG",             g.get("wcg","N/A")),
        ("Color Space",     g.get("color_space","N/A")[:30] if g.get("color_space") else "N/A"),
        ("Active Color",    g.get("advanced_color","N/A")[:30] if g.get("advanced_color") else "N/A"),
        ("Output Type",     g.get("output_type","N/A")),
        ("Monitor",         g.get("monitor_name","N/A")),
        ("Monitor ID",      g.get("monitor_id","N/A")),
        ("Current Mode",    g.get("current_mode","N/A")[:38] if g.get("current_mode") else "N/A"),
    ]
    return "".join(
        f'<div class="spec-row"><span class="spec-k">{k}</span>'
        f'<span class="spec-v">{v}</span></div>'
        for k, v in fields
    )


def _port_items_html(port_list: list) -> str:
    out = []
    for icon, color, name, desc in port_list:
        out.append(f"""<div class="port-item">
  <div class="port-icon {color}">{icon}</div>
  <div><div class="port-name">{name}</div><div class="port-desc">{desc}</div></div>
</div>""")
    return "\n".join(out)


def _summary_cards_html(summary: dict) -> str:
    color_map = {"USB-C": "green", "USB-A": "cyan", "HDMI": "amber",
                 "Audio": "purple", "LAN": "green", "TB4": "cyan", "TB3": "cyan"}
    out = []
    for label, val in summary.items():
        col = color_map.get(label, "green")
        out.append(f"""<div class="ps-card">
  <div class="ps-num" style="color:var(--{col})">{val}</div>
  <div class="ps-label">{label}</div>
</div>""")
    return "\n".join(out)


def _det_grid_html(devices: dict) -> str:
    sections = [
        ("USB Controllers", "cyan", "💻", devices["usb"]),
        ("Audio Devices", "purple", "🎧", devices["sound"]),
        ("Network Adapters", "green", "🌐", devices["network"]),
        ("Input Devices", "amber", "🖱️", devices["input"]),
    ]
    out = []
    for title, color, icon, items in sections:
        rows = "".join(
            f'<div class="det-row"><span class="det-dot" style="background:var(--{color})"></span><span>{item[:80]}</span></div>'
            for item in (items[:6] if items else ["Not detected"])
        )
        out.append(f"""<div class="det-block">
  <div class="det-title">{icon} {title}</div>
  {rows}
</div>""")
    return "\n".join(out)


# ─────────────────────────────────────────────
#  HTML GENERATOR
# ─────────────────────────────────────────────

def generate_html(data: dict) -> str:
    ts = datetime.now().strftime("%d %B %Y · %H:%M")

    gpu1 = data.get("_gpu1", {})
    gpu2 = data.get("_gpu2", {})
    gpu2_found = data.get("_gpu2_found", False)
    ports = data.get("_ports", {})
    devices = data.get("_devices", {})
    cpu = data.get("_cpu", {})

    gpu1_is_discrete = data.get("_gpu1_is_discrete", False)

    # GPU rendering
    gpu1_brand = _gpu_brand(gpu1.get("name", ""))
    gpu2_brand = _gpu_brand(gpu2.get("name", "")) if gpu2_found else "—"

    gpu1_rows = _gpu_card_rows(gpu1)
    gpu2_rows = _gpu_card_rows(gpu2) if gpu2_found else ""

    gpu2_body = gpu2_rows if gpu2_found else (
        '<div class="gpu-not-found">'
        '⚠ No secondary GPU block detected in this DxDiag.<br><br>'
        '<span style="font-size:10px;color:var(--muted);line-height:1.8">'
        'This can happen when the integrated GPU is disabled or when DxDiag only enumerates the active adapter. '
        'Try enabling both GPUs in Device Manager and re-running DxDiag.</span>'
        '</div>'
    )

    gpu2_name = gpu2.get("name", "Not Detected") if gpu2_found else "Not Detected"
    gpu2_mem  = gpu2.get("display_mem", "N/A") if gpu2_found else "N/A"

    # GPU role labels depend on whether a discrete GPU is actually present
    if gpu1_is_discrete:
        gpu1_role_tag  = "DEDICATED"
        gpu1_role_class = "dedicated"
        gpu1_card_label = "Dedicated GPU"
        gpu1_card_tag   = f'Discrete · {gpu1_brand}'
        gpu2_role_tag  = "INTEGRATED"
        gpu2_role_class = "integrated"
    else:
        # Only integrated GPU found — label it correctly
        gpu1_role_tag  = "INTEGRATED"
        gpu1_role_class = "integrated"
        gpu1_card_label = "GPU (Integrated)"
        gpu1_card_tag   = f'Integrated · {gpu1_brand}'
        gpu2_role_tag  = "—"
        gpu2_role_class = "integrated"

    # Ports HTML
    left_ports_html  = _port_items_html(ports.get("left", []))
    right_ports_html = _port_items_html(ports.get("right", []))
    summary_html     = _summary_cards_html(ports.get("summary", {}))
    det_html         = _det_grid_html(devices)
    port_note        = ports.get("note", "⚠ Port layout is estimated — verify with your laptop's official spec sheet.")
    port_note_html   = f'<div class="info-banner">ℹ {port_note}</div>'

    wireless_items = ports.get("wireless", [])
    wireless_html = ""
    wl_icons = ["📶", "🔵", "📺"]
    wl_labels = ["Wi-Fi", "Bluetooth", "Miracast / Other"]
    for i, item in enumerate(wireless_items[:3]):
        icon = wl_icons[i] if i < len(wl_icons) else "📡"
        lbl  = wl_labels[i] if i < len(wl_labels) else "Wireless"
        parts = item.split("·")
        name  = parts[0].strip()
        desc  = parts[1].strip() if len(parts) > 1 else ""
        wireless_html += f"""<div class="wireless-card">
  <div class="wc-icon">{icon}</div>
  <div class="wc-name">{name}</div>
  <div class="wc-desc">{desc}</div>
</div>"""

    # RAM DDR display helpers
    ram_ddr       = data.get("ram_ddr", "Unknown")
    ram_confirmed = data.get("ram_ddr_confirmed", False)
    # Only show the DDR type when it was found in the actual DxDiag text.
    # If not found, show a neutral "Not in DxDiag" — never guess.
    if ram_confirmed:
        ram_ddr_label = ram_ddr               # e.g. "DDR4", "LPDDR5"
        ram_tag_text  = ram_ddr
    else:
        ram_ddr_label = "Not in DxDiag"      # honest: DxDiag doesn't report it
        ram_tag_text  = "RAM"                  # generic tag fallback


    def _est_fps(game: str, gpu: str) -> str:
        # Normalize: strip trademark/registered symbols so "Intel(R) UHD" matches "intel uhd"
        g = re.sub(r'\(r\)|\(tm\)|\(c\)', ' ', gpu.lower())
        g = re.sub(r'\s+', ' ', g).strip()
        # Each entry: (substring to match, fps dict)
        # Ordered from most specific to least specific
        presets = [
            # RTX 50 series (laptop)
            ("rtx 5090", {"cs2": "500+", "aaa": "160+", "gta": "200+"}),
            ("rtx 5080", {"cs2": "500+", "aaa": "140+", "gta": "180+"}),
            ("rtx 5070 ti", {"cs2": "450+", "aaa": "130+", "gta": "170+"}),
            ("rtx 5070", {"cs2": "400+", "aaa": "120+", "gta": "150+"}),
            ("rtx 5060 ti", {"cs2": "380+", "aaa": "110+", "gta": "140+"}),
            ("rtx 5060", {"cs2": "350+", "aaa": "100+", "gta": "130+"}),
            # RTX 40 series (laptop)
            ("rtx 4090", {"cs2": "500+", "aaa": "150+", "gta": "180+"}),
            ("rtx 4080", {"cs2": "450+", "aaa": "130+", "gta": "160+"}),
            ("rtx 4070 ti", {"cs2": "420+", "aaa": "120+", "gta": "150+"}),
            ("rtx 4070", {"cs2": "400+", "aaa": "110+", "gta": "140+"}),
            ("rtx 4060 ti", {"cs2": "380+", "aaa": "100+", "gta": "130+"}),
            ("rtx 4060", {"cs2": "350+", "aaa": "80–110", "gta": "110–140"}),
            ("rtx 4050", {"cs2": "280+", "aaa": "65–85",  "gta": "85–110"}),
            # RTX 30 series (laptop)
            ("rtx 3080 ti", {"cs2": "400+", "aaa": "120+", "gta": "140+"}),
            ("rtx 3080", {"cs2": "380+", "aaa": "110+", "gta": "130+"}),
            ("rtx 3070 ti", {"cs2": "350+", "aaa": "100+", "gta": "120+"}),
            ("rtx 3070", {"cs2": "300+", "aaa": "90–110", "gta": "110–130"}),
            ("rtx 3060", {"cs2": "300+", "aaa": "70–90",  "gta": "90–120"}),
            ("rtx 3050 ti", {"cs2": "220+", "aaa": "58–75", "gta": "78–95"}),
            ("rtx 3050", {"cs2": "200+", "aaa": "55–70",  "gta": "75–90"}),
            # RTX 20 series
            ("rtx 2080 ti", {"cs2": "350+", "aaa": "100+", "gta": "120+"}),
            ("rtx 2080", {"cs2": "320+", "aaa": "90+",   "gta": "110+"}),
            ("rtx 2070", {"cs2": "280+", "aaa": "80+",   "gta": "100+"}),
            ("rtx 2060", {"cs2": "240+", "aaa": "70–85", "gta": "85–110"}),
            ("rtx 2050", {"cs2": "180+", "aaa": "50–65", "gta": "65–80"}),
            # GTX 16 series
            ("gtx 1660 ti", {"cs2": "200+", "aaa": "55–70", "gta": "70–90"}),
            ("gtx 1660 super", {"cs2": "200+", "aaa": "55–70", "gta": "70–90"}),
            ("gtx 1660", {"cs2": "190+", "aaa": "50–65", "gta": "65–85"}),
            ("gtx 1650 ti", {"cs2": "160+", "aaa": "42–58", "gta": "62–78"}),
            ("gtx 1650 super", {"cs2": "170+", "aaa": "45–60", "gta": "65–80"}),
            ("gtx 1650", {"cs2": "150+", "aaa": "40–55",  "gta": "60–75"}),
            # GTX 10 series
            ("gtx 1080 ti", {"cs2": "280+", "aaa": "80+",   "gta": "100+"}),
            ("gtx 1080", {"cs2": "260+", "aaa": "75+",   "gta": "90+"}),
            ("gtx 1070 ti", {"cs2": "230+", "aaa": "65+",   "gta": "80+"}),
            ("gtx 1070", {"cs2": "220+", "aaa": "60+",   "gta": "75+"}),
            ("gtx 1060", {"cs2": "180+", "aaa": "45–60", "gta": "60–75"}),
            ("gtx 1050 ti", {"cs2": "130+", "aaa": "30–45", "gta": "45–60"}),
            ("gtx 1050", {"cs2": "110+", "aaa": "25–38", "gta": "38–52"}),
            # AMD RX 7000 series
            ("rx 7900", {"cs2": "450+", "aaa": "130+", "gta": "160+"}),
            ("rx 7800", {"cs2": "380+", "aaa": "110+", "gta": "140+"}),
            ("rx 7700", {"cs2": "350+", "aaa": "100+", "gta": "125+"}),
            ("rx 7600", {"cs2": "280+", "aaa": "80+",  "gta": "100+"}),
            # AMD RX 6000 series
            ("rx 6800", {"cs2": "380+", "aaa": "110+", "gta": "130+"}),
            ("rx 6700", {"cs2": "300+", "aaa": "90+",  "gta": "110+"}),
            ("rx 6600", {"cs2": "260+", "aaa": "75+",  "gta": "95+"}),
            ("rx 6500", {"cs2": "180+", "aaa": "50–65","gta": "65–80"}),
            # AMD RX 5000 series
            ("rx 5700", {"cs2": "280+", "aaa": "80+",  "gta": "100+"}),
            ("rx 5600", {"cs2": "240+", "aaa": "70+",  "gta": "90+"}),
            ("rx 5500", {"cs2": "180+", "aaa": "50–65","gta": "65–80"}),
            # Intel Arc
            ("arc a770", {"cs2": "220+", "aaa": "60–80","gta": "80–100"}),
            ("arc a750", {"cs2": "200+", "aaa": "55–75","gta": "75–95"}),
            ("arc a580", {"cs2": "180+", "aaa": "50–65","gta": "65–80"}),
            ("arc a380", {"cs2": "130+", "aaa": "35–50","gta": "50–65"}),
            # Integrated GPUs — Intel Iris / UHD / HD series
            ("iris xe",    {"cs2": "90+",  "aaa": "25–40", "gta": "35–50"}),
            ("iris plus",  {"cs2": "70+",  "aaa": "18–30", "gta": "25–38"}),
            ("iris pro",   {"cs2": "75+",  "aaa": "20–32", "gta": "28–40"}),
            ("uhd 770",    {"cs2": "85+",  "aaa": "22–35", "gta": "30–42"}),
            ("uhd 750",    {"cs2": "80+",  "aaa": "20–32", "gta": "28–40"}),
            ("uhd 730",    {"cs2": "75+",  "aaa": "18–30", "gta": "26–36"}),
            ("uhd 630",    {"cs2": "70+",  "aaa": "16–26", "gta": "22–32"}),
            ("uhd 620",    {"cs2": "65+",  "aaa": "14–22", "gta": "18–28"}),
            ("uhd 610",    {"cs2": "60+",  "aaa": "12–20", "gta": "16–24"}),
            ("intel uhd",  {"cs2": "75+",  "aaa": "18–30", "gta": "25–38"}),
            # Intel HD Graphics (older — pre-UHD naming, e.g. HD 620, HD 630, HD 520)
            ("hd graphics 630", {"cs2": "65+",  "aaa": "14–22", "gta": "18–28"}),
            ("hd graphics 620", {"cs2": "60+",  "aaa": "12–20", "gta": "16–24"}),
            ("hd graphics 520", {"cs2": "55+",  "aaa": "10–18", "gta": "14–20"}),
            ("hd graphics 510", {"cs2": "50+",  "aaa": "9–15",  "gta": "12–18"}),
            ("hd graphics 500", {"cs2": "45+",  "aaa": "8–14",  "gta": "10–16"}),
            ("hd graphics 400", {"cs2": "40+",  "aaa": "7–12",  "gta": "9–14"}),
            ("hd graphics",     {"cs2": "55+",  "aaa": "10–18", "gta": "14–22"}),  # catch-all HD
            ("intel hd",        {"cs2": "55+",  "aaa": "10–18", "gta": "14–22"}),  # catch-all HD
            # AMD RDNA3 integrated (Radeon 7xx M / 8xx M)
            ("radeon 890m",  {"cs2": "150+", "aaa": "40–60", "gta": "60–80"}),
            ("radeon 880m",  {"cs2": "140+", "aaa": "38–55", "gta": "55–75"}),
            ("radeon 760m",  {"cs2": "130+", "aaa": "35–50", "gta": "50–65"}),
            ("radeon 740m",  {"cs2": "110+", "aaa": "28–42", "gta": "40–55"}),
            # AMD RDNA2 integrated (Radeon 6xx M)
            ("radeon 680m",  {"cs2": "110+", "aaa": "30–45", "gta": "40–55"}),
            ("radeon 660m",  {"cs2": "95+",  "aaa": "25–38", "gta": "35–48"}),
            # AMD Vega integrated (Ryzen 4000/5000 series)
            ("vega 11",  {"cs2": "80+",  "aaa": "20–32", "gta": "28–40"}),
            ("vega 10",  {"cs2": "75+",  "aaa": "18–30", "gta": "26–36"}),
            ("vega 8",   {"cs2": "70+",  "aaa": "18–28", "gta": "25–35"}),
            ("vega 7",   {"cs2": "65+",  "aaa": "16–26", "gta": "22–32"}),
            ("vega 6",   {"cs2": "60+",  "aaa": "14–22", "gta": "18–28"}),
            ("vega 3",   {"cs2": "50+",  "aaa": "10–16", "gta": "14–20"}),
            # AMD GCN integrated (older Ryzen / A-series APUs — e.g. Radeon 610M, 620, 530, 520)
            ("radeon 640m",  {"cs2": "65+",  "aaa": "14–22", "gta": "18–26"}),
            ("radeon 630m",  {"cs2": "60+",  "aaa": "12–20", "gta": "16–24"}),
            ("radeon 620m",  {"cs2": "55+",  "aaa": "10–18", "gta": "14–22"}),
            ("radeon 610m",  {"cs2": "50+",  "aaa": "9–15",  "gta": "12–18"}),
            ("radeon 610",   {"cs2": "50+",  "aaa": "9–15",  "gta": "12–18"}),
            ("radeon 540",   {"cs2": "55+",  "aaa": "10–18", "gta": "14–20"}),
            ("radeon 530",   {"cs2": "50+",  "aaa": "9–15",  "gta": "12–18"}),
            ("radeon 520",   {"cs2": "45+",  "aaa": "8–14",  "gta": "10–16"}),
            ("radeon rx 540", {"cs2": "80+",  "aaa": "20–32", "gta": "28–38"}),
            ("radeon rx 530", {"cs2": "70+",  "aaa": "16–26", "gta": "22–30"}),
            # AMD Radeon generic catch-all (covers any un-matched Radeon integrated)
            ("amd radeon",   {"cs2": "55+",  "aaa": "10–20", "gta": "14–24"}),
            ("radeon graphics", {"cs2": "60+","aaa": "12–22", "gta": "16–26"}),
        ]
        for pattern, fps in presets:
            if pattern in g:
                return fps.get(game, "—")
        # Final brand-level fallbacks so we never show — for a known GPU brand
        if "nvidia" in g or "geforce" in g:
            return "~varies"
        if "radeon" in g or "amd" in g:
            return "~varies"
        if "intel" in g:
            return "~varies"
        return "—"

    gpu1_name_str = gpu1.get("name", "")
    # Normalize for matching (remove trademark symbols)
    gpu1_name_norm = re.sub(r'\(r\)|\(tm\)|\(c\)', ' ', gpu1_name_str.lower())
    gpu1_name_norm = re.sub(r'\s+', ' ', gpu1_name_norm).strip()
    fps_cs2  = _est_fps("cs2",  gpu1_name_str)
    fps_aaa  = _est_fps("aaa",  gpu1_name_str)
    fps_gta  = _est_fps("gta",  gpu1_name_str)

    # TDP estimates
    tdp_map = {
        "i5-13500H": "45W", "i7-13620H": "45W", "i9-13900H": "45W",
        "i5-12500H": "45W", "i7-12700H": "45W",
        "Ryzen 5 6600H": "45W", "Ryzen 7 6800H": "45W",
    }
    cpu_tdp = "45W"
    for k, v in tdp_map.items():
        if k.lower() in data["processor"].lower():
            cpu_tdp = v
            break

    gpu_tgp_map = {
        "RTX 3050": "80W", "RTX 3060": "115W", "RTX 4060": "140W",
        "RTX 3070": "125W", "GTX 1650": "50W",
    }
    gpu_tgp = "N/A"
    for k, v in gpu_tgp_map.items():
        if k.lower() in gpu1_name_norm:
            gpu_tgp = v
            break

    perf_bars = [
        ("CPU Multi-Core",  82, "g"),
        ("GPU Gaming",      70, "c"),
        ("RAM Bandwidth",   75, "p"),
        ("SSD Read Speed",  88, "a"),
        ("Display Quality", 78, "g"),
        ("Battery Life",    55, "c"),
        ("Thermal Design",  72, "p"),
    ]

    # Adjust bars based on actual GPU (g_lower is already normalized — no trademark symbols)
    g_lower = gpu1_name_norm
    if any(x in g_lower for x in ["rtx 4090", "rtx 4080"]):
        perf_bars[1] = ("GPU Gaming", 98, "c")
    elif any(x in g_lower for x in ["rtx 4070 ti", "rtx 4070"]):
        perf_bars[1] = ("GPU Gaming", 94, "c")
    elif "rtx 4060 ti" in g_lower:
        perf_bars[1] = ("GPU Gaming", 90, "c")
    elif "rtx 4060" in g_lower:
        perf_bars[1] = ("GPU Gaming", 88, "c")
    elif "rtx 4050" in g_lower:
        perf_bars[1] = ("GPU Gaming", 80, "c")
    elif any(x in g_lower for x in ["rtx 3080", "rtx 3070 ti"]):
        perf_bars[1] = ("GPU Gaming", 88, "c")
    elif "rtx 3070" in g_lower:
        perf_bars[1] = ("GPU Gaming", 83, "c")
    elif "rtx 3060" in g_lower:
        perf_bars[1] = ("GPU Gaming", 78, "c")
    elif "rtx 3050 ti" in g_lower:
        perf_bars[1] = ("GPU Gaming", 68, "c")
    elif "rtx 3050" in g_lower:
        perf_bars[1] = ("GPU Gaming", 62, "c")
    elif any(x in g_lower for x in ["gtx 1660 ti", "gtx 1660 super"]):
        perf_bars[1] = ("GPU Gaming", 60, "c")
    elif "gtx 1660" in g_lower:
        perf_bars[1] = ("GPU Gaming", 57, "c")
    elif any(x in g_lower for x in ["gtx 1650 super", "gtx 1650 ti"]):
        perf_bars[1] = ("GPU Gaming", 52, "c")
    elif "gtx 1650" in g_lower:
        perf_bars[1] = ("GPU Gaming", 48, "c")
    elif "gtx 1050 ti" in g_lower:
        perf_bars[1] = ("GPU Gaming", 35, "c")
    elif "gtx 1050" in g_lower:
        perf_bars[1] = ("GPU Gaming", 30, "c")
    # AMD RDNA3 integrated
    elif any(x in g_lower for x in ["radeon 890m", "radeon 880m"]):
        perf_bars[1] = ("GPU Gaming", 38, "c")
    elif any(x in g_lower for x in ["radeon 760m", "radeon 740m"]):
        perf_bars[1] = ("GPU Gaming", 32, "c")
    # AMD RDNA2 integrated
    elif any(x in g_lower for x in ["radeon 680m", "radeon 660m"]):
        perf_bars[1] = ("GPU Gaming", 28, "c")
    # AMD Vega integrated
    elif any(x in g_lower for x in ["vega 11", "vega 10", "vega 8"]):
        perf_bars[1] = ("GPU Gaming", 22, "c")
    elif any(x in g_lower for x in ["vega 7", "vega 6", "vega 3"]):
        perf_bars[1] = ("GPU Gaming", 18, "c")
    # AMD GCN integrated (Radeon 6xxM / 5xx / 4xx)
    elif any(x in g_lower for x in ["radeon 640m", "radeon 630m", "radeon 620m"]):
        perf_bars[1] = ("GPU Gaming", 16, "c")
    elif any(x in g_lower for x in ["radeon 610m", "radeon 610", "radeon 540", "radeon 530", "radeon 520"]):
        perf_bars[1] = ("GPU Gaming", 14, "c")
    elif "amd radeon" in g_lower or "radeon graphics" in g_lower:
        perf_bars[1] = ("GPU Gaming", 15, "c")
    # Intel Iris Xe
    elif "iris xe" in g_lower:
        perf_bars[1] = ("GPU Gaming", 28, "c")
    elif any(x in g_lower for x in ["iris plus", "iris pro"]):
        perf_bars[1] = ("GPU Gaming", 20, "c")
    elif "iris" in g_lower:
        perf_bars[1] = ("GPU Gaming", 22, "c")
    # Intel UHD (modern)
    elif any(x in g_lower for x in ["uhd 770", "uhd 750"]):
        perf_bars[1] = ("GPU Gaming", 22, "c")
    elif any(x in g_lower for x in ["uhd 730", "uhd 630", "uhd 620", "uhd 610"]):
        perf_bars[1] = ("GPU Gaming", 18, "c")
    elif "intel uhd" in g_lower or "uhd graphics" in g_lower:
        perf_bars[1] = ("GPU Gaming", 20, "c")
    # Intel HD Graphics (older — pre-UHD naming)
    elif any(x in g_lower for x in ["hd graphics 630", "hd graphics 620"]):
        perf_bars[1] = ("GPU Gaming", 16, "c")
    elif any(x in g_lower for x in ["hd graphics 520", "hd graphics 510"]):
        perf_bars[1] = ("GPU Gaming", 14, "c")
    elif any(x in g_lower for x in ["hd graphics", "intel hd"]):
        perf_bars[1] = ("GPU Gaming", 15, "c")

    perf_bars_html = "\n".join(
        f'<div class="bar-row"><div class="bar-name">{name}</div>'
        f'<div class="bar-track"><div class="bar-fill {cls}" style="width:{pct}%"></div></div>'
        f'<div class="bar-val">{pct}/100</div></div>'
        for name, pct, cls in perf_bars
    )

    # Summary table rows (all data fields)
    table_rows = "".join(
        f'<tr><td>{k.replace("_"," ").title()}</td><td>{str(v)[:120]}</td></tr>'
        for k, v in sorted(data.items()) if not k.startswith('_')
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{data.get('model','Laptop')} · System Profile</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;600;800;900&family=Share+Tech+Mono&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --green:#00ff88;--cyan:#00e5ff;--purple:#7c3aed;--amber:#f59e0b;--red:#ef4444;
  --dark:#060810;--card:#0d1117;--card2:#111827;
  --border:rgba(0,255,136,0.15);--text:#e2e8f0;--muted:#64748b;
}}
html{{scroll-behavior:smooth}}
body{{font-family:'Syne',sans-serif;background:var(--dark);color:var(--text);min-height:100vh;overflow-x:hidden}}
body::after{{content:'';position:fixed;inset:0;background-image:linear-gradient(rgba(0,255,136,0.022) 1px,transparent 1px),linear-gradient(90deg,rgba(0,255,136,0.022) 1px,transparent 1px);background-size:64px 64px;pointer-events:none;z-index:0}}
body::before{{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 70% 50% at 50% -5%,rgba(0,255,136,0.07) 0%,transparent 60%),radial-gradient(ellipse 40% 30% at 90% 85%,rgba(0,229,255,0.05) 0%,transparent 50%);pointer-events:none;z-index:0}}

/* ── NAVBAR ── */
.navbar{{position:sticky;top:0;z-index:100;background:rgba(6,8,16,0.93);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;padding:0 28px;height:60px;gap:12px}}
.nav-brand{{font-family:'Orbitron',sans-serif;font-size:12px;font-weight:800;color:#fff;letter-spacing:2px;white-space:nowrap}}
.nav-brand span{{color:var(--green)}}
.nav-tabs{{display:flex;gap:2px;flex-wrap:wrap}}
.nav-tab{{font-family:'Share Tech Mono',monospace;font-size:10px;letter-spacing:1.2px;text-transform:uppercase;color:var(--muted);background:none;border:none;padding:7px 12px;border-radius:6px;cursor:pointer;transition:color .2s,background .2s;position:relative;white-space:nowrap}}
.nav-tab:hover{{color:var(--text);background:rgba(255,255,255,0.04)}}
.nav-tab.active{{color:var(--green)}}
.nav-tab.active::after{{content:'';position:absolute;bottom:-1px;left:12px;right:12px;height:2px;background:linear-gradient(90deg,transparent,var(--green),transparent)}}
.nav-right{{font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--muted);letter-spacing:1px;white-space:nowrap}}
.status-dot{{display:inline-block;width:6px;height:6px;background:var(--green);border-radius:50%;margin-right:6px;animation:blink 1.8s ease infinite}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:0.2}}}}

/* ── PAGES ── */
.page{{display:none;position:relative;z-index:1}}.page.active{{display:block}}
.wrap{{max-width:1160px;margin:0 auto;padding:0 28px 80px}}

/* ── HERO ── */
.hero{{display:grid;grid-template-columns:1fr 1fr;gap:48px;align-items:center;padding:52px 0 36px}}
.badge{{display:inline-flex;align-items:center;gap:8px;background:rgba(0,255,136,0.07);border:1px solid var(--border);border-radius:999px;padding:4px 14px;font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--green);letter-spacing:2px;text-transform:uppercase;margin-bottom:16px}}
.hero-title{{font-family:'Orbitron',sans-serif;font-size:clamp(24px,3.5vw,46px);font-weight:900;line-height:1.08;letter-spacing:-1px;color:#fff}}
.hero-title span{{background:linear-gradient(135deg,var(--green),var(--cyan));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}}
.hero-sub{{margin-top:12px;font-size:11px;color:var(--muted);font-family:'Share Tech Mono',monospace;line-height:2}}
.hero-stats{{display:flex;gap:20px;margin-top:24px;flex-wrap:wrap}}
.hstat{{display:flex;flex-direction:column;gap:3px}}
.hstat-val{{font-family:'Orbitron',sans-serif;font-size:18px;font-weight:800;color:var(--green)}}
.hstat-label{{font-size:9px;color:var(--muted);letter-spacing:1.5px;text-transform:uppercase}}
.laptop-visual{{display:flex;align-items:center;justify-content:center}}
.laptop-svg-wrap{{position:relative;width:100%;max-width:400px;animation:float 4.5s ease-in-out infinite}}
@keyframes float{{0%,100%{{transform:translateY(0)}}50%{{transform:translateY(-8px)}}}}

/* ── DIVIDER ── */
.divider{{display:flex;align-items:center;gap:14px;margin:40px 0 22px}}
.divider-line{{flex:1;height:1px;background:linear-gradient(90deg,transparent,var(--border),transparent)}}
.divider-label{{font-family:'Orbitron',sans-serif;font-size:10px;font-weight:600;color:var(--green);letter-spacing:3px;text-transform:uppercase;white-space:nowrap}}

/* ── CARDS ── */
.cards-3{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}
.cards-4{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
.cards-2{{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px;position:relative;overflow:hidden;transition:transform .25s,border-color .25s,box-shadow .25s;animation:fadeUp .5s ease-out both}}
.card:hover{{transform:translateY(-3px);border-color:rgba(0,255,136,0.35);box-shadow:0 10px 36px rgba(0,255,136,0.06)}}
.card::before{{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--green),transparent);opacity:0;transition:opacity .25s}}
.card:hover::before{{opacity:1}}
@keyframes fadeUp{{from{{opacity:0;transform:translateY(16px)}}to{{opacity:1;transform:translateY(0)}}}}
.card:nth-child(1){{animation-delay:.04s}}.card:nth-child(2){{animation-delay:.08s}}.card:nth-child(3){{animation-delay:.12s}}
.card:nth-child(4){{animation-delay:.16s}}.card:nth-child(5){{animation-delay:.20s}}.card:nth-child(6){{animation-delay:.24s}}
.card-icon{{font-size:24px;margin-bottom:10px;display:block}}
.card-label{{font-size:9px;text-transform:uppercase;letter-spacing:2px;color:var(--muted);margin-bottom:4px}}
.card-val{{font-family:'Orbitron',sans-serif;font-size:14px;font-weight:700;color:#fff;line-height:1.3}}
.card-val.big{{font-size:20px}}
.card-sub{{margin-top:4px;font-size:11px;color:var(--muted);font-family:'Share Tech Mono',monospace;line-height:1.5}}
.tag{{display:inline-block;border-radius:4px;padding:2px 8px;font-size:9px;font-family:'Share Tech Mono',monospace;margin-top:6px}}
.tag.green{{background:rgba(0,255,136,0.1);color:var(--green);border:1px solid rgba(0,255,136,0.25)}}
.tag.cyan{{background:rgba(0,229,255,0.1);color:var(--cyan);border:1px solid rgba(0,229,255,0.25)}}
.tag.purple{{background:rgba(124,58,237,0.12);color:#a78bfa;border:1px solid rgba(124,58,237,0.3)}}
.tag.amber{{background:rgba(245,158,11,0.12);color:var(--amber);border:1px solid rgba(245,158,11,0.3)}}

/* ── GPU CARDS ── */
.gpu-row{{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}}
.gpu-card{{background:var(--card2);border:1px solid var(--border);border-radius:14px;padding:22px;position:relative;overflow:hidden}}
.gpu-card.primary{{border-color:rgba(0,229,255,0.35)}}
.gpu-card.secondary{{border-color:rgba(124,58,237,0.35)}}
.gpu-header{{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:12px;gap:8px}}
.gpu-header-left{{flex:1;min-width:0}}
.gpu-brand{{font-family:'Orbitron',sans-serif;font-size:10px;font-weight:600;letter-spacing:2px;text-transform:uppercase;margin-bottom:4px}}
.gpu-brand.primary{{color:var(--cyan)}}
.gpu-brand.secondary{{color:#a78bfa}}
.gpu-name{{font-family:'Orbitron',sans-serif;font-size:13px;font-weight:800;color:#fff;line-height:1.3;word-break:break-word}}
.gpu-role-tag{{font-family:'Share Tech Mono',monospace;font-size:9px;letter-spacing:1.5px;padding:3px 8px;border-radius:4px;flex-shrink:0;white-space:nowrap;margin-top:2px}}
.gpu-role-tag.dedicated{{color:var(--cyan);border:1px solid rgba(0,229,255,0.3);background:rgba(0,229,255,0.06)}}
.gpu-role-tag.integrated{{color:#a78bfa;border:1px solid rgba(124,58,237,0.3);background:rgba(124,58,237,0.06)}}
.gpu-mem-line{{font-size:11px;font-family:'Share Tech Mono',monospace;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid rgba(255,255,255,0.07)}}
.gpu-mem-line.primary{{color:var(--cyan)}}
.gpu-mem-line.secondary{{color:#a78bfa}}
.gpu-not-found{{padding:24px 12px;text-align:center;font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--muted);line-height:1.8}}
.spec-row{{display:flex;justify-content:space-between;gap:8px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,0.04)}}
.spec-row:last-child{{border-bottom:none}}
.spec-k{{font-size:10px;color:var(--muted);white-space:nowrap;flex-shrink:0}}
.spec-v{{font-size:10px;color:var(--text);font-family:'Share Tech Mono',monospace;text-align:right;word-break:break-all}}

/* ── DISPLAY ── */
.display-card{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:28px;display:grid;grid-template-columns:1fr 1fr;gap:28px;align-items:center}}
.display-visual{{aspect-ratio:16/9;background:linear-gradient(135deg,#0a0e17,#161d2e);border-radius:8px;border:2px solid rgba(0,255,136,0.2);display:flex;align-items:center;justify-content:center;overflow:hidden;position:relative}}
.display-visual::before{{content:'';position:absolute;inset:0;background:linear-gradient(135deg,rgba(0,255,136,0.04),rgba(0,229,255,0.04))}}
.display-res{{font-family:'Orbitron',sans-serif;font-size:18px;font-weight:900;color:var(--green);position:relative}}
.display-hz{{position:absolute;bottom:8px;right:10px;font-family:'Orbitron',sans-serif;font-size:11px;font-weight:700;color:var(--cyan)}}
.display-size-label{{position:absolute;top:8px;left:10px;font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--muted)}}
.display-specs{{display:flex;flex-direction:column;gap:10px}}
.dspec{{display:flex;flex-direction:column;gap:2px}}
.dspec-label{{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:1.5px}}
.dspec-val{{font-family:'Orbitron',sans-serif;font-size:14px;font-weight:700;color:#fff}}

/* ── SYS GRID ── */
.sys-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}}
.sys-item{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 14px;display:flex;flex-direction:column;gap:3px}}
.sys-k{{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:1.5px}}
.sys-v{{font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--green);word-break:break-all}}

/* ── PAGE HEADER ── */
.page-header{{padding:40px 0 24px;display:flex;align-items:flex-start;gap:18px}}
.page-header-icon{{width:52px;height:52px;background:rgba(0,255,136,0.08);border:1px solid var(--border);border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:24px;flex-shrink:0}}
.page-header-text h2{{font-family:'Orbitron',sans-serif;font-size:24px;font-weight:900;color:#fff;margin-bottom:5px}}
.page-header-text p{{font-size:12px;color:var(--muted);line-height:1.7}}

/* ── PORTS ── */
.port-layout{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.side-block{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:22px}}
.side-title{{font-family:'Orbitron',sans-serif;font-size:11px;font-weight:700;color:var(--cyan);letter-spacing:2px;text-transform:uppercase;margin-bottom:14px}}
.port-item{{display:flex;align-items:flex-start;gap:12px;padding:10px 0;border-bottom:1px solid rgba(255,255,255,0.05)}}
.port-item:last-child{{border-bottom:none}}
.port-icon{{width:36px;height:36px;flex-shrink:0;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:15px}}
.port-icon.green{{background:rgba(0,255,136,0.08);border:1px solid rgba(0,255,136,0.2)}}
.port-icon.cyan{{background:rgba(0,229,255,0.08);border:1px solid rgba(0,229,255,0.2)}}
.port-icon.purple{{background:rgba(124,58,237,0.1);border:1px solid rgba(124,58,237,0.25)}}
.port-icon.amber{{background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.2)}}
.port-icon.muted{{background:rgba(100,116,139,0.08);border:1px solid rgba(100,116,139,0.2)}}
.port-name{{font-family:'Orbitron',sans-serif;font-size:12px;font-weight:700;color:#fff;margin-bottom:2px}}
.port-desc{{font-size:10px;color:var(--muted);font-family:'Share Tech Mono',monospace}}
.port-summary{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:16px}}
.ps-card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;text-align:center}}
.ps-num{{font-family:'Orbitron',sans-serif;font-size:26px;font-weight:900}}
.ps-label{{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:1.5px;margin-top:3px}}
.wireless-row{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:16px}}
.wireless-card{{background:var(--card2);border:1px solid var(--border);border-radius:12px;padding:20px;text-align:center}}
.wc-icon{{font-size:30px;margin-bottom:10px}}
.wc-name{{font-family:'Orbitron',sans-serif;font-size:13px;font-weight:700;color:#fff;margin-bottom:4px}}
.wc-desc{{font-size:11px;color:var(--muted);font-family:'Share Tech Mono',monospace}}
.info-banner{{background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.25);border-radius:8px;padding:12px 16px;font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--amber);margin-bottom:16px}}
.det-grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:16px}}
.det-block{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px}}
.det-title{{font-family:'Orbitron',sans-serif;font-size:10px;font-weight:700;color:var(--green);letter-spacing:2px;text-transform:uppercase;margin-bottom:12px}}
.det-row{{display:flex;align-items:flex-start;gap:8px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,0.04);font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--text)}}
.det-row:last-child{{border-bottom:none}}
.det-dot{{width:6px;height:6px;border-radius:50%;flex-shrink:0;margin-top:3px}}

/* ── PERFORMANCE ── */
.perf-hero{{background:linear-gradient(135deg,rgba(0,255,136,0.06),rgba(0,229,255,0.04));border:1px solid var(--border);border-radius:16px;padding:28px;display:grid;grid-template-columns:repeat(4,1fr);gap:24px;margin-bottom:16px}}
.perf-stat{{display:flex;flex-direction:column;gap:4px}}
.perf-stat-label{{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase}}
.perf-stat-val{{font-family:'Orbitron',sans-serif;font-size:26px;font-weight:900;color:var(--green)}}
.perf-stat-sub{{font-size:12px;color:var(--muted);font-family:'Share Tech Mono',monospace}}
.bar-chart{{display:flex;flex-direction:column;gap:12px}}
.bar-row{{display:flex;align-items:center;gap:12px}}
.bar-name{{font-size:11px;color:var(--muted);width:130px;flex-shrink:0;font-family:'Share Tech Mono',monospace}}
.bar-track{{flex:1;height:8px;background:rgba(255,255,255,0.05);border-radius:999px;overflow:hidden}}
.bar-fill{{height:100%;border-radius:999px;animation:fillIn 1.2s ease-out forwards;transform-origin:left}}
.bar-fill.g{{background:linear-gradient(90deg,var(--green),#00cc66)}}
.bar-fill.c{{background:linear-gradient(90deg,var(--cyan),#0099cc)}}
.bar-fill.p{{background:linear-gradient(90deg,#a78bfa,var(--purple))}}
.bar-fill.a{{background:linear-gradient(90deg,var(--amber),#d97706)}}
@keyframes fillIn{{from{{transform:scaleX(0)}}to{{transform:scaleX(1)}}}}
.bar-val{{font-family:'Orbitron',sans-serif;font-size:11px;font-weight:700;color:#fff;width:50px;text-align:right}}
.thermal-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}
.thermal-card{{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px}}
.tc-label{{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px}}
.tc-val{{font-family:'Orbitron',sans-serif;font-size:20px;font-weight:800}}
.tc-desc{{font-size:11px;color:var(--muted);font-family:'Share Tech Mono',monospace;margin-top:6px;line-height:1.5}}

/* ── CPU DEEP DIVE ── */
.cpu-block{{background:var(--card2);border:1px solid rgba(0,229,255,0.25);border-radius:14px;padding:24px;margin-bottom:16px}}
.cpu-header{{display:flex;align-items:center;gap:16px;margin-bottom:18px}}
.cpu-brand-badge{{font-family:'Orbitron',sans-serif;font-size:10px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:var(--cyan);background:rgba(0,229,255,0.08);border:1px solid rgba(0,229,255,0.25);padding:4px 12px;border-radius:4px}}
.cpu-name-big{{font-family:'Orbitron',sans-serif;font-size:20px;font-weight:900;color:#fff}}
.cpu-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
.cpu-stat{{display:flex;flex-direction:column;gap:4px;padding:14px;background:rgba(0,255,136,0.04);border:1px solid var(--border);border-radius:10px}}
.cpu-stat-label{{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:1.5px}}
.cpu-stat-val{{font-family:'Orbitron',sans-serif;font-size:16px;font-weight:800;color:var(--green)}}
.cpu-stat-sub{{font-size:10px;color:var(--muted);font-family:'Share Tech Mono',monospace;margin-top:2px}}

/* ── BUILD TABLE ── */
.build-table{{width:100%;border-collapse:collapse}}
.build-table th{{text-align:left;font-family:'Share Tech Mono',monospace;font-size:9px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;padding:10px 14px;border-bottom:1px solid var(--border)}}
.build-table td{{padding:9px 14px;font-size:11px;border-bottom:1px solid rgba(255,255,255,0.04);font-family:'Share Tech Mono',monospace;word-break:break-all}}
.build-table tr:last-child td{{border-bottom:none}}
.build-table td:first-child{{color:var(--muted);width:200px}}
.build-table tr:hover td{{background:rgba(255,255,255,0.02)}}

/* ── FOOTER ── */
.footer{{text-align:center;padding:32px 0 0;border-top:1px solid var(--border);margin-top:50px}}
.footer-logo{{font-family:'Orbitron',sans-serif;font-size:11px;font-weight:800;letter-spacing:4px;color:var(--muted);text-transform:uppercase}}
.footer-time{{margin-top:5px;font-family:'Share Tech Mono',monospace;font-size:10px;color:rgba(100,116,139,0.4)}}

/* ── MOBILE TAB BAR ── */
.mobile-tabbar{{display:none}}

@media(max-width:860px){{
  .hero{{grid-template-columns:1fr}}.laptop-visual{{order:-1}}
  .cards-3,.cards-4{{grid-template-columns:1fr 1fr}}
  .gpu-row,.port-layout,.det-grid,.perf-hero{{grid-template-columns:1fr}}
  .display-card{{grid-template-columns:1fr}}
  .sys-grid{{grid-template-columns:1fr 1fr}}
  .cpu-grid{{grid-template-columns:1fr 1fr}}
  .thermal-grid{{grid-template-columns:1fr 1fr}}
  .port-summary{{grid-template-columns:repeat(3,1fr)}}
  .wireless-row{{grid-template-columns:1fr}}

  /* Hide desktop nav items on mobile */
  .nav-tabs{{display:none}}
  .nav-right{{display:none}}
  .navbar{{padding:0 16px;height:52px}}

  /* Mobile tab bar */
  .mobile-tabbar{{
    display:flex;
    position:sticky;
    top:52px;
    z-index:99;
    background:rgba(6,8,16,0.97);
    backdrop-filter:blur(20px);
    border-bottom:2px solid var(--border);
    overflow-x:auto;
    scrollbar-width:none;
    -webkit-overflow-scrolling:touch;
  }}
  .mobile-tabbar::-webkit-scrollbar{{display:none}}
  .mob-tab{{
    flex:1;
    min-width:56px;
    display:flex;
    flex-direction:column;
    align-items:center;
    justify-content:center;
    padding:8px 6px 7px;
    font-family:'Share Tech Mono',monospace;
    font-size:8px;
    letter-spacing:0.5px;
    text-transform:uppercase;
    color:var(--muted);
    background:none;
    border:none;
    border-bottom:2px solid transparent;
    margin-bottom:-2px;
    cursor:pointer;
    transition:color .2s,border-color .2s;
    white-space:nowrap;
    gap:4px;
  }}
  .mob-tab .mt-icon{{font-size:19px;line-height:1}}
  .mob-tab .mt-label{{font-size:7.5px;margin-top:2px}}
  .mob-tab.active{{color:var(--green);border-bottom-color:var(--green)}}
  .mob-tab:hover{{color:var(--text)}}
  .wrap{{padding:0 14px 60px}}
}}
</style>
</head>
<body>
<nav class="navbar">
  <div class="nav-brand">{data.get('manufacturer','SYSTEM')} <span>PROFILE</span></div>
  <div class="nav-tabs">
    <button class="nav-tab active" onclick="switchTab('overview')">🏠 Overview</button>
    <button class="nav-tab" onclick="switchTab('gpu')">🎮 GPU &amp; Display</button>
    <button class="nav-tab" onclick="switchTab('ports')">🔌 Ports &amp; I/O</button>
    <button class="nav-tab" onclick="switchTab('performance')">📊 Performance</button>
    <button class="nav-tab" onclick="switchTab('system')">⚙️ System</button>
  </div>
  <div class="nav-right"><span class="status-dot"></span>Report Loaded</div>
</nav>

<!-- Mobile tab bar (only visible on narrow screens) -->
<div class="mobile-tabbar" id="mobile-tabbar">
  <button class="mob-tab active" onclick="switchTab('overview')">
    <span class="mt-icon">🏠</span><span class="mt-label">Overview</span>
  </button>
  <button class="mob-tab" onclick="switchTab('gpu')">
    <span class="mt-icon">🎮</span><span class="mt-label">GPU</span>
  </button>
  <button class="mob-tab" onclick="switchTab('ports')">
    <span class="mt-icon">🔌</span><span class="mt-label">Ports</span>
  </button>
  <button class="mob-tab" onclick="switchTab('performance')">
    <span class="mt-icon">📊</span><span class="mt-label">Perf</span>
  </button>
  <button class="mob-tab" onclick="switchTab('system')">
    <span class="mt-icon">⚙️</span><span class="mt-label">System</span>
  </button>
</div>

<!-- ══════ OVERVIEW ══════ -->
<div class="page active" id="page-overview">
<div class="wrap">
  <section class="hero">
    <div>
      <div class="badge">DxDiag Report · {data.get('report_time','N/A')}</div>
      <h1 class="hero-title">{data.get('manufacturer','System')}<br><span>{data.get('model','Profile')}</span></h1>
      <p class="hero-sub">
        MACHINE · {data.get('machine_name','N/A')}<br>
        OS · {data.get('os','N/A')[:50]}<br>
        BIOS · {data.get('bios','N/A')}
      </p>
      <div class="hero-stats">
        <div class="hstat"><div class="hstat-val">{data.get('ram_gb','?')}</div><div class="hstat-label">RAM · {ram_tag_text}</div></div>
        <div class="hstat"><div class="hstat-val">{data.get('refresh','?')} Hz</div><div class="hstat-label">Refresh</div></div>
        <div class="hstat"><div class="hstat-val">{data.get('res_w','?')}×{data.get('res_h','?')}</div><div class="hstat-label">Resolution</div></div>
        <div class="hstat"><div class="hstat-val">{cpu.get('logical','?')}</div><div class="hstat-label">CPU Threads</div></div>
      </div>
    </div>
    <div class="laptop-visual">
      <div class="laptop-svg-wrap">
        <svg viewBox="0 0 400 280" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <linearGradient id="lidG" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" style="stop-color:#1a2235"/><stop offset="100%" style="stop-color:#0d1117"/>
            </linearGradient>
            <linearGradient id="screenG" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" style="stop-color:#0a1628"/><stop offset="100%" style="stop-color:#060f1e"/>
            </linearGradient>
            <filter id="glow"><feGaussianBlur stdDeviation="3" result="blur"/><feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
          </defs>
          <rect x="30" y="20" width="340" height="210" rx="12" fill="url(#lidG)" stroke="rgba(0,255,136,0.3)" stroke-width="1.5"/>
          <rect x="45" y="32" width="310" height="186" rx="6" fill="#060810"/>
          <rect x="52" y="38" width="296" height="174" rx="4" fill="url(#screenG)"/>
          <line x1="52" y1="80" x2="348" y2="80" stroke="rgba(0,255,136,0.05)" stroke-width="1"/>
          <line x1="52" y1="130" x2="348" y2="130" stroke="rgba(0,229,255,0.05)" stroke-width="1"/>
          <line x1="52" y1="180" x2="348" y2="180" stroke="rgba(0,255,136,0.05)" stroke-width="1"/>
          <rect x="52" y="38" width="296" height="3" fill="rgba(0,255,136,0.3)" rx="1">
            <animateTransform attributeName="transform" type="translate" from="0 0" to="0 174" dur="3s" repeatCount="indefinite"/>
          </rect>
          <text x="200" y="115" fill="rgba(0,255,136,0.6)" font-size="11" font-family="monospace" text-anchor="middle" filter="url(#glow)">{data.get('manufacturer','SYSTEM')}</text>
          <text x="200" y="133" fill="rgba(0,229,255,0.8)" font-size="13" font-family="monospace" text-anchor="middle" font-weight="bold" filter="url(#glow)">{data.get('model','N/A')[:22]}</text>
          <text x="200" y="150" fill="rgba(255,255,255,0.3)" font-size="9" font-family="monospace" text-anchor="middle">{data.get('res_w','?')}×{data.get('res_h','?')} · {data.get('refresh','?')}Hz</text>
          <text x="200" y="164" fill="rgba(0,255,136,0.35)" font-size="9" font-family="monospace" text-anchor="middle">{data.get('ram_gb','?')} RAM · {cpu.get('logical','?')} CPUs</text>
          <circle cx="200" cy="42" r="3" fill="rgba(0,255,136,0.4)"/>
          <rect x="10" y="230" width="380" height="18" rx="4" fill="#0d1117" stroke="rgba(0,255,136,0.2)" stroke-width="1"/>
          <rect x="10" y="227" width="380" height="6" rx="2" fill="#080e18" stroke="rgba(0,255,136,0.15)" stroke-width="1"/>
          <rect x="30" y="233" width="180" height="7" rx="2" fill="rgba(0,255,136,0.05)"/>
          <rect x="250" y="233" width="60" height="7" rx="2" fill="rgba(0,229,255,0.05)"/>
          <text x="35" y="237" fill="rgba(0,255,136,0.5)" font-size="7" font-family="monospace">◀ PORTS</text>
          <text x="295" y="237" fill="rgba(0,229,255,0.5)" font-size="7" font-family="monospace">PORTS ▶</text>
        </svg>
      </div>
    </div>
  </section>

  <div class="divider"><div class="divider-line"></div><div class="divider-label">Core Specs</div><div class="divider-line"></div></div>
  <div class="cards-3">
    <div class="card">
      <span class="card-icon">⚡</span>
      <div class="card-label">Processor</div>
      <div class="card-val">{data.get('processor','N/A')[:40]}</div>
      <div class="card-sub">Threads: {cpu.get('logical','?')} · Base: {cpu.get('ghz','?')} · Boost: {cpu.get('boost','N/A')}</div>
      <span class="tag green">CPU · {cpu.get('arch','N/A')}</span>
    </div>
    <div class="card">
      <span class="card-icon">🧠</span>
      <div class="card-label">Memory</div>
      <div class="card-val big">{data.get('ram_gb','N/A')}</div>
      <div class="card-sub">{ram_ddr_label} · Available: {data.get('available_mem','N/A')}</div>
      <span class="tag cyan">{ram_tag_text}</span>
    </div>
    <div class="card">
      <span class="card-icon">🎮</span>
      <div class="card-label">{gpu1_card_label}</div>
      <div class="card-val">{data.get('gpu1_name','N/A')[:30]}</div>
      <div class="card-sub">VRAM: {data.get('dedicated_mem','N/A')} · Total: {data.get('display_mem','N/A')}</div>
      <span class="tag green">{gpu1_card_tag}</span>
    </div>
    <div class="card">
      <span class="card-icon">📺</span>
      <div class="card-label">Display</div>
      <div class="card-val">{data.get('res_w','?')}×{data.get('res_h','?')}</div>
      <div class="card-sub">{data.get('refresh','?')} Hz · {data.get('monitor_id','N/A')}</div>
      <span class="tag cyan">FHD</span>
    </div>
    <div class="card">
      <span class="card-icon">🕹️</span>
      <div class="card-label">DirectX</div>
      <div class="card-val">{data.get('directx','N/A')}</div>
      <div class="card-sub">WDDM: {data.get('wddm','N/A')} · DB: {data.get('dx_db','N/A')}</div>
      <span class="tag purple">DX API</span>
    </div>
    <div class="card">
      <span class="card-icon">📡</span>
      <div class="card-label">Miracast</div>
      <div class="card-val">{data.get('miracast','N/A')[:25]}</div>
      <div class="card-sub">MUX: {data.get('mux_target','N/A')}</div>
      <span class="tag amber">Wireless</span>
    </div>
  </div>

  <div class="footer">
    <div class="footer-logo">{data.get('model','System Profile')}</div>
    <div class="footer-time">Generated {ts} · DxDiag Extractor v5</div>
  </div>
</div>
</div>

<!-- ══════ GPU & DISPLAY ══════ -->
<div class="page" id="page-gpu">
<div class="wrap">
  <div class="page-header">
    <div class="page-header-icon">🎮</div>
    <div class="page-header-text">
      <h2>GPU &amp; Display</h2>
    <p>{'Dedicated GPU (Primary) shown left · Integrated GPU (Secondary) shown right.' if gpu1_is_discrete else 'Only an Integrated GPU was detected in this DxDiag.'} All DxDiag driver fields extracted.</p>
    </div>
  </div>

  <div class="gpu-row">
    <!-- PRIMARY GPU -->
    <div class="gpu-card primary">
      <div class="gpu-header">
        <div class="gpu-header-left">
          <div class="gpu-brand primary">{gpu1_brand}</div>
          <div class="gpu-name">{gpu1.get('name','N/A')}</div>
        </div>
        <span class="gpu-role-tag {gpu1_role_class}">{gpu1_role_tag}</span>
      </div>
      <div class="gpu-mem-line primary">Display Mem: {gpu1.get('display_mem','N/A')} · VRAM: {gpu1.get('dedicated_mem','N/A')} · {gpu1.get('hybrid_gpu','N/A')}</div>
      {gpu1_rows}
    </div>

    <!-- SECONDARY GPU -->
    <div class="gpu-card secondary">
      <div class="gpu-header">
        <div class="gpu-header-left">
          <div class="gpu-brand secondary">{gpu2_brand}</div>
          <div class="gpu-name">{gpu2_name}</div>
        </div>
        <span class="gpu-role-tag {gpu2_role_class}">{gpu2_role_tag}</span>
      </div>
      <div class="gpu-mem-line secondary">Display Mem: {gpu2_mem} · MUX Target: {data.get('mux_target','N/A')}</div>
      {gpu2_body}
    </div>
  </div>

  <div class="divider"><div class="divider-line"></div><div class="divider-label">Display</div><div class="divider-line"></div></div>
  <div class="display-card">
    <div class="display-visual">
      <span class="display-size-label">{data.get('display_size','?')} · {data.get('monitor_name','Monitor')}</span>
      <div class="display-res">{data.get('res_w','?')} × {data.get('res_h','?')}</div>
      <span class="display-hz">{data.get('refresh','?')} Hz</span>
    </div>
    <div class="display-specs">
      <div class="dspec"><div class="dspec-label">Resolution</div><div class="dspec-val">{data.get('res_w','?')}×{data.get('res_h','?')}</div></div>
      <div class="dspec"><div class="dspec-label">Refresh Rate</div><div class="dspec-val" style="color:var(--cyan)">{data.get('refresh','?')} Hz</div></div>
      <div class="dspec"><div class="dspec-label">Monitor ID</div><div class="dspec-val">{data.get('monitor_id','N/A')}</div></div>
      <div class="dspec"><div class="dspec-label">Current Mode</div><div class="dspec-val" style="font-size:11px">{data.get('current_mode','N/A')[:35]}</div></div>
      <div class="dspec"><div class="dspec-label">Output Type</div><div class="dspec-val">{data.get('output_type','N/A')}</div></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:4px;">
        <span class="tag green">{'WCG ✓' if 'supported' in data.get('wcg','').lower() else 'WCG'}</span>
        <span class="tag cyan">{data.get('color_space','SDR')[:20]}</span>
        <span class="tag purple">{data.get('directx','DX12')}</span>
      </div>
    </div>
  </div>

  <div class="footer">
    <div class="footer-logo">{data.get('model','System Profile')} · GPU &amp; Display</div>
    <div class="footer-time">Generated {ts}</div>
  </div>
</div>
</div>

<!-- ══════ PORTS & I/O ══════ -->
<div class="page" id="page-ports">
<div class="wrap">
  <div class="page-header">
    <div class="page-header-icon">🔌</div>
    <div class="page-header-text">
      <h2>Ports &amp; I/O</h2>
      <p>Physical connectivity for <strong>{data.get('model','this device')}</strong> — detected from model database + DxDiag device list.</p>
    </div>
  </div>

  {port_note_html}

  <div class="port-summary">
    {summary_html}
  </div>

  <div class="divider"><div class="divider-line"></div><div class="divider-label">Physical Ports</div><div class="divider-line"></div></div>
  <div class="port-layout">
    <div class="side-block">
      <div class="side-title">← Left Side</div>
      {left_ports_html}
    </div>
    <div class="side-block">
      <div class="side-title">Right Side →</div>
      {right_ports_html}
    </div>
  </div>

  <div class="divider"><div class="divider-line"></div><div class="divider-label">Wireless</div><div class="divider-line"></div></div>
  <div class="wireless-row">
    {wireless_html}
  </div>

  <div class="divider"><div class="divider-line"></div><div class="divider-label">Detected Devices from DxDiag</div><div class="divider-line"></div></div>
  <div class="det-grid">
    {det_html}
  </div>

  <div class="footer">
    <div class="footer-logo">{data.get('model','System Profile')} · Ports &amp; I/O</div>
    <div class="footer-time">Generated {ts} · Port layout from model database</div>
  </div>
</div>
</div>

<!-- ══════ PERFORMANCE ══════ -->
<div class="page" id="page-performance">
<div class="wrap">
  <div class="page-header">
    <div class="page-header-icon">📊</div>
    <div class="page-header-text">
      <h2>Performance</h2>
      <p>CPU &amp; GPU benchmarks, thermal specs, gaming estimates, and system performance overview.</p>
    </div>
  </div>

  <div class="perf-hero">
    <div class="perf-stat">
      <div class="perf-stat-label">CPU Boost</div>
      <div class="perf-stat-val">{cpu.get('boost', data.get('cpu_ghz','?'))}</div>
      <div class="perf-stat-sub">{data.get('processor','')[:32]}</div>
    </div>
    <div class="perf-stat">
      <div class="perf-stat-label">GPU VRAM</div>
      <div class="perf-stat-val" style="color:var(--cyan)">{data.get('dedicated_mem','N/A')}</div>
      <div class="perf-stat-sub">{data.get('gpu1_name','')[:28]}</div>
    </div>
    <div class="perf-stat">
      <div class="perf-stat-label">Display</div>
      <div class="perf-stat-val" style="color:var(--amber)">{data.get('refresh','?')} Hz</div>
      <div class="perf-stat-sub">{data.get('res_w','?')}×{data.get('res_h','?')} FHD</div>
    </div>
    <div class="perf-stat">
      <div class="perf-stat-label">RAM</div>
      <div class="perf-stat-val" style="color:#a78bfa">{data.get('ram_gb','?')}</div>
      <div class="perf-stat-sub">{ram_ddr_label} · Available: {data.get('available_mem','N/A')}</div>
    </div>
  </div>

  <div class="divider"><div class="divider-line"></div><div class="divider-label">Performance Index (vs. budget gaming tier)</div><div class="divider-line"></div></div>
  <div class="card" style="padding:28px">
    <div class="card-label" style="margin-bottom:20px">Relative benchmark scores</div>
    <div class="bar-chart">
      {perf_bars_html}
    </div>
  </div>

  <div class="divider"><div class="divider-line"></div><div class="divider-label">Gaming Estimates</div><div class="divider-line"></div></div>
  <div class="cards-3">
    <div class="card">
      <span class="card-icon">🎯</span>
      <div class="card-label">CS2 / Valorant</div>
      <div class="card-val big" style="color:var(--green)">{fps_cs2} FPS</div>
      <div class="card-sub">1080p Low–Medium</div>
      <span class="tag green">Competitive</span>
    </div>
    <div class="card">
      <span class="card-icon">🐉</span>
      <div class="card-label">AAA Open World</div>
      <div class="card-val big" style="color:var(--cyan)">{fps_aaa} FPS</div>
      <div class="card-sub">1080p Medium settings</div>
      <span class="tag cyan">Playable</span>
    </div>
    <div class="card">
      <span class="card-icon">🌟</span>
      <div class="card-label">GTA V / Semi-Open</div>
      <div class="card-val big" style="color:var(--amber)">{fps_gta} FPS</div>
      <div class="card-sub">1080p Medium-High</div>
      <span class="tag amber">DLSS / FSR</span>
    </div>
  </div>

  <div class="divider"><div class="divider-line"></div><div class="divider-label">Thermal &amp; Power</div><div class="divider-line"></div></div>
  <div class="thermal-grid">
    <div class="thermal-card">
      <div class="tc-label">CPU TDP</div>
      <div class="tc-val" style="color:var(--green)">{cpu_tdp}</div>
      <div class="tc-desc">{cpu.get('model','CPU')} base TDP<br>May boost under sustained load</div>
    </div>
    <div class="thermal-card">
      <div class="tc-label">GPU TGP</div>
      <div class="tc-val" style="color:var(--cyan)">{gpu_tgp}</div>
      <div class="tc-desc">{data.get('gpu1_name','GPU')[:24]}<br>Total Graphics Power</div>
    </div>
    <div class="thermal-card">
      <div class="tc-label">CPU Threads</div>
      <div class="tc-val" style="color:var(--amber)">{cpu.get('logical','?')}</div>
      <div class="tc-desc">Logical processors<br>{cpu.get('arch','N/A')} architecture</div>
    </div>
    <div class="thermal-card">
      <div class="tc-label">L3 Cache</div>
      <div class="tc-val" style="color:#a78bfa">{cpu.get('cache','N/A')}</div>
      <div class="tc-desc">{cpu.get('gen','N/A')}<br>Last-level cache</div>
    </div>
  </div>

  <div class="divider"><div class="divider-line"></div><div class="divider-label">Full Spec Table</div><div class="divider-line"></div></div>
  <div class="card" style="padding:0;overflow:auto">
    <table class="build-table">
      <thead><tr><th>Component</th><th>Specification</th></tr></thead>
      <tbody>
        <tr><td>Processor</td><td>{data.get('processor','N/A')}</td></tr>
        <tr><td>Architecture</td><td>{cpu.get('arch','N/A')} · {cpu.get('gen','N/A')}</td></tr>
        <tr><td>CPU Threads</td><td>{cpu.get('logical','N/A')} logical · Base {cpu.get('ghz','N/A')} · Boost {cpu.get('boost','N/A')}</td></tr>
        <tr><td>L3 Cache</td><td>{cpu.get('cache','N/A')}</td></tr>
        <tr><td>GPU (Discrete)</td><td>{data.get('gpu1_name','N/A')} · VRAM: {data.get('dedicated_mem','N/A')} · Total: {data.get('display_mem','N/A')}</td></tr>
        <tr><td>GPU (Integrated)</td><td>{gpu2_name} · {gpu2_mem}</td></tr>
        <tr><td>RAM</td><td>{data.get('ram_gb','N/A')} {ram_ddr_label} · Available: {data.get('available_mem','N/A')}</td></tr>
        <tr><td>Display</td><td>{data.get('res_w','?')}×{data.get('res_h','?')} · {data.get('refresh','?')}Hz · {data.get('monitor_id','N/A')}</td></tr>
        <tr><td>Operating System</td><td>{data.get('os','N/A')[:80]}</td></tr>
        <tr><td>DirectX</td><td>{data.get('directx','N/A')} · WDDM {data.get('wddm','N/A')}</td></tr>
        <tr><td>BIOS</td><td>{data.get('bios','N/A')}</td></tr>
        <tr><td>Miracast</td><td>{data.get('miracast','N/A')}</td></tr>
        <tr><td>MUX Target</td><td>{data.get('mux_target','N/A')}</td></tr>
      </tbody>
    </table>
  </div>

  <div class="footer">
    <div class="footer-logo">{data.get('model','System Profile')} · Performance</div>
    <div class="footer-time">FPS estimates are approximate · Generated {ts}</div>
  </div>
</div>
</div>

<!-- ══════ SYSTEM DETAILS ══════ -->
<div class="page" id="page-system">
<div class="wrap">
  <div class="page-header">
    <div class="page-header-icon">⚙️</div>
    <div class="page-header-text">
      <h2>System Details</h2>
      <p>OS, BIOS, detailed CPU info, driver data, all raw DxDiag fields.</p>
    </div>
  </div>

  <div class="divider"><div class="divider-line"></div><div class="divider-label">CPU Deep Dive</div><div class="divider-line"></div></div>
  <div class="cpu-block">
    <div class="cpu-header">
      <span class="cpu-brand-badge">{cpu.get('brand','CPU')}</span>
      <div class="cpu-name-big">{cpu.get('model',data.get('processor','N/A')[:30])}</div>
    </div>
    <div class="cpu-grid">
      <div class="cpu-stat">
        <div class="cpu-stat-label">Logical CPUs</div>
        <div class="cpu-stat-val">{cpu.get('logical','N/A')}</div>
        <div class="cpu-stat-sub">Threads (w/ HyperThreading)</div>
      </div>
      <div class="cpu-stat">
        <div class="cpu-stat-label">Base Clock</div>
        <div class="cpu-stat-val">{cpu.get('ghz','N/A')}</div>
        <div class="cpu-stat-sub">Reported by DxDiag</div>
      </div>
      <div class="cpu-stat">
        <div class="cpu-stat-label">Boost Clock</div>
        <div class="cpu-stat-val">{cpu.get('boost','N/A')}</div>
        <div class="cpu-stat-sub">Max single-core turbo</div>
      </div>
      <div class="cpu-stat">
        <div class="cpu-stat-label">L3 Cache</div>
        <div class="cpu-stat-val">{cpu.get('cache','N/A')}</div>
        <div class="cpu-stat-sub">Shared last-level cache</div>
      </div>
      <div class="cpu-stat">
        <div class="cpu-stat-label">Generation</div>
        <div class="cpu-stat-val" style="font-size:13px">{cpu.get('gen','N/A')}</div>
        <div class="cpu-stat-sub">{cpu.get('arch','N/A')}</div>
      </div>
      <div class="cpu-stat">
        <div class="cpu-stat-label">TDP</div>
        <div class="cpu-stat-val">{cpu_tdp}</div>
        <div class="cpu-stat-sub">Base power envelope</div>
      </div>
      <div class="cpu-stat">
        <div class="cpu-stat-label">DirectX FL</div>
        <div class="cpu-stat-val" style="font-size:13px">{data.get('directx','N/A')}</div>
        <div class="cpu-stat-sub">WDDM {data.get('wddm','N/A')}</div>
      </div>
      <div class="cpu-stat">
        <div class="cpu-stat-label">Platform</div>
        <div class="cpu-stat-val" style="font-size:12px">{cpu.get('brand','N/A')}</div>
        <div class="cpu-stat-sub">H-series Mobile</div>
      </div>
    </div>
  </div>

  <div class="divider"><div class="divider-line"></div><div class="divider-label">OS &amp; System</div><div class="divider-line"></div></div>
  <div class="sys-grid">
    <div class="sys-item"><div class="sys-k">Machine Name</div><div class="sys-v">{data.get('machine_name','N/A')}</div></div>
    <div class="sys-item"><div class="sys-k">Manufacturer</div><div class="sys-v">{data.get('manufacturer','N/A')}</div></div>
    <div class="sys-item"><div class="sys-k">Model</div><div class="sys-v">{data.get('model','N/A')}</div></div>
    <div class="sys-item"><div class="sys-k">OS</div><div class="sys-v">{data.get('os','N/A')[:40]}</div></div>
    <div class="sys-item"><div class="sys-k">BIOS</div><div class="sys-v">{data.get('bios','N/A')}</div></div>
    <div class="sys-item"><div class="sys-k">Processor</div><div class="sys-v">{data.get('processor','N/A')[:40]}</div></div>
    <div class="sys-item"><div class="sys-k">Memory</div><div class="sys-v">{data.get('memory','N/A')} · {ram_ddr_label}</div></div>
    <div class="sys-item"><div class="sys-k">Page File</div><div class="sys-v">{data.get('page_file','N/A')[:35]}</div></div>
    <div class="sys-item"><div class="sys-k">DirectX</div><div class="sys-v">{data.get('directx','N/A')}</div></div>
    <div class="sys-item"><div class="sys-k">DPI Setting</div><div class="sys-v">{data.get('dpi','N/A')}</div></div>
    <div class="sys-item"><div class="sys-k">Miracast</div><div class="sys-v">{data.get('miracast','N/A')[:30]}</div></div>
    <div class="sys-item"><div class="sys-k">Machine ID</div><div class="sys-v">{data.get('machine_id','N/A')[:30]}</div></div>
    <div class="sys-item"><div class="sys-k">MUX Support</div><div class="sys-v">{data.get('mux_support','N/A')[:30]}</div></div>
    <div class="sys-item"><div class="sys-k">MUX Target GPU</div><div class="sys-v">{data.get('mux_target','N/A')}</div></div>
    <div class="sys-item"><div class="sys-k">DxDiag Version</div><div class="sys-v">{data.get('dxdiag_ver','N/A')[:30]}</div></div>
  </div>

  <div class="divider"><div class="divider-line"></div><div class="divider-label">Full Raw Data</div><div class="divider-line"></div></div>
  <div class="card" style="padding:0;overflow:auto">
    <table class="build-table">
      <thead><tr><th>Field</th><th>Value</th></tr></thead>
      <tbody>
        {table_rows}
      </tbody>
    </table>
  </div>

  <div class="footer">
    <div class="footer-logo">{data.get('model','System Profile')} · System</div>
    <div class="footer-time">Generated {ts} · DxDiag Extractor v5</div>
  </div>
</div>
</div>

<script>
function switchTab(name){{
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.mob-tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('page-'+name).classList.add('active');
  const tabs=['overview','gpu','ports','performance','system'];
  const idx = tabs.indexOf(name);
  document.querySelectorAll('.nav-tab')[idx].classList.add('active');
  document.querySelectorAll('.mob-tab')[idx].classList.add('active');
  window.scrollTo({{top:0,behavior:'smooth'}});
}}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────
#  THE WEB APP  (served locally)
# ─────────────────────────────────────────────

UPLOAD_DIR = pathlib.Path.home() / "DxDiag_Outputs"
UPLOAD_DIR.mkdir(exist_ok=True)

APP_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DxDiag Extractor v5</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;600;800;900&family=Share+Tech+Mono&family=Syne:wght@400;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--green:#00ff88;--cyan:#00e5ff;--purple:#7c3aed;--amber:#f59e0b;--dark:#060810;--card:#0d1117;--card2:#111827;--border:rgba(0,255,136,0.15);--text:#e2e8f0;--muted:#64748b;}
html,body{height:100%;font-family:'Syne',sans-serif;background:var(--dark);color:var(--text)}
body{overflow-x:hidden}
body::before{content:'';position:fixed;inset:0;background:radial-gradient(ellipse 60% 45% at 50% 0%,rgba(0,255,136,0.07) 0%,transparent 60%),radial-gradient(ellipse 40% 30% at 85% 80%,rgba(0,229,255,0.05) 0%,transparent 50%),radial-gradient(ellipse 35% 25% at 10% 75%,rgba(124,58,237,0.05) 0%,transparent 50%);pointer-events:none;z-index:0;}
body::after{content:'';position:fixed;inset:0;background-image:linear-gradient(rgba(0,255,136,0.02) 1px,transparent 1px),linear-gradient(90deg,rgba(0,255,136,0.02) 1px,transparent 1px);background-size:64px 64px;pointer-events:none;z-index:0;}
.navbar{position:sticky;top:0;z-index:100;background:rgba(6,8,16,0.93);backdrop-filter:blur(20px);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;padding:0 36px;height:62px;}
.nav-logo{font-family:'Orbitron',sans-serif;font-size:14px;font-weight:900;letter-spacing:2px;color:#fff}
.nav-logo span{color:var(--green)}
.nav-status{display:flex;align-items:center;gap:8px;font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--muted);letter-spacing:1.5px;}
.dot{width:7px;height:7px;border-radius:50%;background:var(--green);animation:pulse 2s ease infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(0,255,136,0.4)}50%{box-shadow:0 0 0 6px rgba(0,255,136,0)}}
.hero{position:relative;z-index:1;text-align:center;padding:56px 24px 32px;}
.hero-eyebrow{display:inline-flex;align-items:center;gap:8px;background:rgba(0,255,136,0.07);border:1px solid var(--border);border-radius:999px;padding:5px 16px;font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--green);letter-spacing:2.5px;text-transform:uppercase;margin-bottom:22px;}
.hero-eyebrow::before{content:'◆';font-size:7px}
.hero-title{font-family:'Orbitron',sans-serif;font-size:clamp(32px,6vw,60px);font-weight:900;line-height:1.05;letter-spacing:-2px;color:#fff;margin-bottom:14px;}
.hero-title .accent{background:linear-gradient(135deg,var(--green),var(--cyan));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;}
.hero-sub{font-size:13px;color:var(--muted);max-width:560px;margin:0 auto 36px;font-family:'Share Tech Mono',monospace;line-height:1.9;}
.feat-row{display:flex;justify-content:center;gap:12px;flex-wrap:wrap;margin-bottom:32px;}
.feat-chip{background:rgba(0,229,255,0.06);border:1px solid rgba(0,229,255,0.2);color:var(--cyan);font-family:'Share Tech Mono',monospace;font-size:9px;padding:4px 12px;border-radius:4px;letter-spacing:1.5px;}
.main{position:relative;z-index:1;max-width:1100px;margin:0 auto;padding:0 28px 80px;display:grid;grid-template-columns:1fr 1fr;gap:24px}
@media(max-width:800px){.main{grid-template-columns:1fr}}
.panel{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:28px;position:relative;overflow:hidden}
.panel::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--green),transparent);opacity:.6}
.panel-title{font-family:'Orbitron',sans-serif;font-size:12px;font-weight:700;color:var(--green);letter-spacing:3px;text-transform:uppercase;margin-bottom:20px;display:flex;align-items:center;gap:10px}
.panel-title::before{content:'';display:inline-block;width:6px;height:6px;background:var(--green);border-radius:50%}
.drop-zone{border:2px dashed rgba(0,255,136,0.25);border-radius:12px;padding:36px 20px;text-align:center;cursor:pointer;transition:all .3s;background:rgba(0,255,136,0.025);position:relative;}
.drop-zone.drag-over{border-color:var(--green);background:rgba(0,255,136,0.06);box-shadow:0 0 30px rgba(0,255,136,0.1);}
.drop-zone:hover{border-color:rgba(0,255,136,0.45);background:rgba(0,255,136,0.04)}
.drop-icon{font-size:40px;margin-bottom:12px;display:block;animation:bounce 2s ease-in-out infinite}
@keyframes bounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-6px)}}
.drop-title{font-family:'Orbitron',sans-serif;font-size:13px;font-weight:700;color:#fff;margin-bottom:6px}
.drop-sub{font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--muted);margin-bottom:16px;line-height:1.6}
.drop-types{display:flex;gap:8px;justify-content:center;margin-bottom:18px}
.ext-tag{background:rgba(0,229,255,0.08);border:1px solid rgba(0,229,255,0.2);color:var(--cyan);font-family:'Share Tech Mono',monospace;font-size:10px;padding:3px 10px;border-radius:4px;letter-spacing:1px;}
.file-input{display:none}
.browse-btn{display:inline-flex;align-items:center;gap:8px;background:rgba(0,255,136,0.1);border:1px solid rgba(0,255,136,0.3);color:var(--green);font-family:'Share Tech Mono',monospace;font-size:11px;letter-spacing:1.5px;padding:9px 20px;border-radius:8px;cursor:pointer;transition:all .2s;text-transform:uppercase;}
.browse-btn:hover{background:rgba(0,255,136,0.18);border-color:rgba(0,255,136,0.6);box-shadow:0 0 20px rgba(0,255,136,0.1)}
.file-info{display:none;margin-top:16px;background:rgba(0,255,136,0.06);border:1px solid rgba(0,255,136,0.2);border-radius:8px;padding:10px 14px;font-family:'Share Tech Mono',monospace;font-size:11px;align-items:center;gap:10px;}
.file-info.show{display:flex}
.file-icon{font-size:20px;flex-shrink:0}
.file-name{color:var(--green);flex:1;word-break:break-all}
.file-size{color:var(--muted);flex-shrink:0}
.clear-file-btn{flex-shrink:0;background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);color:#f87171;font-size:13px;font-weight:700;width:24px;height:24px;border-radius:5px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .2s;line-height:1;padding:0;}
.clear-file-btn:hover{background:rgba(239,68,68,0.22);border-color:rgba(239,68,68,0.6)}
.generate-btn{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;margin-top:20px;background:linear-gradient(135deg,rgba(0,255,136,0.15),rgba(0,229,255,0.1));border:1px solid rgba(0,255,136,0.4);color:#fff;font-family:'Orbitron',sans-serif;font-size:12px;font-weight:700;letter-spacing:2px;padding:14px 24px;border-radius:10px;cursor:pointer;transition:all .25s;text-transform:uppercase;}
.generate-btn:hover:not(:disabled){background:linear-gradient(135deg,rgba(0,255,136,0.25),rgba(0,229,255,0.15));box-shadow:0 0 30px rgba(0,255,136,0.15);transform:translateY(-2px);}
.generate-btn:disabled{opacity:.4;cursor:not-allowed;transform:none}
.progress-wrap{margin-top:16px;display:none}
.progress-wrap.show{display:block}
.progress-label{font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--muted);margin-bottom:6px;letter-spacing:1px}
.progress-bar{height:4px;background:rgba(255,255,255,0.06);border-radius:999px;overflow:hidden}
.progress-fill{height:100%;background:linear-gradient(90deg,var(--green),var(--cyan));border-radius:999px;width:0%;transition:width .4s ease}
#notification{position:fixed;bottom:30px;right:30px;z-index:999;background:var(--card);border-radius:14px;border:1px solid var(--border);padding:18px 22px;min-width:300px;transform:translateY(100px);opacity:0;transition:all .4s cubic-bezier(0.34,1.56,0.64,1);box-shadow:0 20px 60px rgba(0,0,0,0.5);}
#notification.show{transform:translateY(0);opacity:1}
#notification.success{border-color:rgba(0,255,136,0.4)}
#notification.error{border-color:rgba(239,68,68,0.4)}
.notif-top{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.notif-icon{font-size:22px}
.notif-title{font-family:'Orbitron',sans-serif;font-size:12px;font-weight:700;color:#fff}
.notif-msg{font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--muted);line-height:1.5}
.notif-bar{height:3px;background:var(--green);border-radius:999px;margin-top:12px;animation:shrink 4s linear forwards}
@keyframes shrink{from{width:100%}to{width:0%}}
.step-list{display:flex;flex-direction:column;gap:14px}
.step{display:flex;align-items:flex-start;gap:14px}
.step-num{width:28px;height:28px;flex-shrink:0;background:rgba(0,255,136,0.1);border:1px solid rgba(0,255,136,0.25);border-radius:6px;display:flex;align-items:center;justify-content:center;font-family:'Orbitron',sans-serif;font-size:11px;font-weight:800;color:var(--green);}
.step-title{font-size:13px;font-weight:600;color:#fff;margin-bottom:3px}
.step-desc{font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--muted);line-height:1.6}
.step-code{display:inline-block;background:rgba(0,0,0,0.4);border:1px solid rgba(255,255,255,0.08);border-radius:4px;padding:3px 8px;font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--cyan);margin-top:4px;user-select:all;cursor:copy;}
.divider-sm{height:1px;background:var(--border);margin:6px 0}
.output-panel{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:28px;position:relative;overflow:hidden;grid-column:1/-1}
.output-panel::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--cyan),transparent);opacity:.5}
.output-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px;margin-top:16px}
.output-file{background:rgba(0,229,255,0.05);border:1px solid rgba(0,229,255,0.18);border-radius:10px;padding:12px 14px;display:flex;align-items:center;gap:10px;transition:border-color .2s;}
.output-file:hover{border-color:rgba(0,229,255,0.35)}
.of-name{font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--cyan);flex:1;word-break:break-all;min-width:0}
.of-actions{display:flex;align-items:center;gap:6px;flex-shrink:0}
.of-open{font-family:'Share Tech Mono',monospace;font-size:10px;color:var(--green);background:rgba(0,255,136,0.08);border:1px solid rgba(0,255,136,0.2);padding:4px 10px;border-radius:5px;cursor:pointer;text-decoration:none;transition:all .2s;white-space:nowrap;}
.of-open:hover{background:rgba(0,255,136,0.16);border-color:rgba(0,255,136,0.4)}
.of-delete{font-size:13px;background:rgba(239,68,68,0.07);border:1px solid rgba(239,68,68,0.2);color:#f87171;border-radius:5px;padding:4px 7px;cursor:pointer;transition:all .2s;line-height:1;}
.of-delete:hover{background:rgba(239,68,68,0.18);border-color:rgba(239,68,68,0.5)}
.of-delete:disabled{opacity:.4;cursor:not-allowed}
.empty-state{text-align:center;padding:28px;font-family:'Share Tech Mono',monospace;font-size:11px;color:var(--muted);}
.empty-state .es-icon{font-size:32px;margin-bottom:10px}
.scan-line{position:absolute;bottom:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,var(--green),transparent);animation:scan 3s linear infinite;}
@keyframes scan{0%{bottom:0;opacity:0}10%{opacity:1}90%{opacity:1}100%{bottom:100%;opacity:0}}
</style>
</head>
<body>
<nav class="navbar">
  <div class="nav-logo">DxDiag <span>Extractor</span> <span style="font-size:9px;color:var(--muted);letter-spacing:1px">v5</span></div>
  <div class="nav-status"><div class="dot"></div><span id="status-text">READY · PORT 7474</span></div>
</nav>

<section class="hero">
  <div class="hero-eyebrow">System Profile Generator v5</div>
  <h1 class="hero-title">Drop your DxDiag<br><span class="accent">Get a sick HTML</span></h1>
  <p class="hero-sub">Upload any .txt or .docx DxDiag file · GPU roles auto-detected · 5 tabs including Ports, Performance, CPU deep-dive.</p>
  <div class="feat-row">
    <span class="feat-chip">✓ GPU ROLE AUTO-DETECT</span>
    <span class="feat-chip">✓ PORTS &amp; I/O TAB</span>
    <span class="feat-chip">✓ PERFORMANCE TAB</span>
    <span class="feat-chip">✓ CPU DEEP DIVE</span>
    <span class="feat-chip">✓ DEVICE LIST</span>
  </div>
</section>

<div class="main">
  <div class="panel">
    <div class="scan-line"></div>
    <div class="panel-title">Upload File</div>
    <div class="drop-zone" id="dropZone">
      <span class="drop-icon">📂</span>
      <div class="drop-title">Drag & Drop your DxDiag file here</div>
      <div class="drop-sub">or click below to browse<br>Supports .txt and .docx formats</div>
      <div class="drop-types"><span class="ext-tag">.TXT</span><span class="ext-tag">.DOCX</span></div>
      <input type="file" id="fileInput" class="file-input" accept=".txt,.docx">
      <label for="fileInput" class="browse-btn">📁 Browse Files</label>
    </div>
    <div class="file-info" id="fileInfo">
      <span class="file-icon" id="fileIcon">📄</span>
      <span class="file-name" id="fileName">—</span>
      <span class="file-size" id="fileSize">—</span>
      <button class="clear-file-btn" onclick="clearFile()" title="Remove file">✕</button>
    </div>
    <div class="progress-wrap" id="progressWrap">
      <div class="progress-label" id="progressLabel">INITIALIZING...</div>
      <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
    </div>
    <button class="generate-btn" id="generateBtn" disabled onclick="generate()">
      <span>⚡</span>GENERATE SYSTEM PROFILE
    </button>
  </div>

  <div class="panel">
    <div class="panel-title">How to get your DxDiag</div>
    <div class="step-list">
      <div class="step"><div class="step-num">1</div><div class="step-content"><div class="step-title">Open Run Dialog</div><div class="step-desc">Press <strong>Windows + R</strong></div></div></div>
      <div class="divider-sm"></div>
      <div class="step"><div class="step-num">2</div><div class="step-content"><div class="step-title">Launch DxDiag</div><div class="step-desc">Type and press Enter:</div><span class="step-code">dxdiag</span></div></div>
      <div class="divider-sm"></div>
      <div class="step"><div class="step-num">3</div><div class="step-content"><div class="step-title">Wait for scan</div><div class="step-desc">Wait until the green progress bar at the bottom disappears</div></div></div>
      <div class="divider-sm"></div>
      <div class="step"><div class="step-num">4</div><div class="step-content"><div class="step-title">Save as Text File</div><div class="step-desc">Click <strong>"Save All Information..."</strong> → save as <strong>.txt</strong></div></div></div>
      <div class="divider-sm"></div>
      <div class="step"><div class="step-num">5</div><div class="step-content"><div class="step-title">Upload &amp; Generate</div><div class="step-desc">Drag the .txt here and hit Generate!</div></div></div>
    </div>
  </div>

  <div class="output-panel" id="outputPanel">
    <div class="panel-title">Generated Profiles</div>
    <div id="outputGrid" class="output-grid">
      <div class="empty-state">
        <div class="es-icon">🗂️</div>
        Generated HTML profiles will appear here.<br>
        Output saved to: <span style="color:var(--cyan)">~/DxDiag_Outputs/</span>
      </div>
    </div>
  </div>
</div>

<div id="notification">
  <div class="notif-top"><span class="notif-icon" id="notifIcon">✅</span><span class="notif-title" id="notifTitle">Done!</span></div>
  <div class="notif-msg" id="notifMsg"></div>
  <div class="notif-bar" id="notifBar"></div>
</div>

<script>
let selectedFile = null;
const dz = document.getElementById('dropZone');
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag-over'); });
dz.addEventListener('dragleave', () => dz.classList.remove('drag-over'));
dz.addEventListener('drop', e => { e.preventDefault(); dz.classList.remove('drag-over'); const f = e.dataTransfer.files[0]; if (f) setFile(f); });
document.getElementById('fileInput').addEventListener('change', e => { if (e.target.files[0]) setFile(e.target.files[0]); });

function setFile(file) {
  const ext = file.name.split('.').pop().toLowerCase();
  if (!['txt','docx'].includes(ext)) { showNotif('error','❌ Invalid File','Only .txt and .docx files are supported.'); return; }
  selectedFile = file;
  document.getElementById('fileIcon').textContent = ext==='docx' ? '📝' : '📄';
  document.getElementById('fileName').textContent = file.name;
  document.getElementById('fileSize').textContent = formatSize(file.size);
  document.getElementById('fileInfo').classList.add('show');
  document.getElementById('generateBtn').disabled = false;
  document.getElementById('status-text').textContent = 'FILE READY · ' + file.name.toUpperCase();
}

function clearFile() {
  selectedFile = null;
  document.getElementById('fileInfo').classList.remove('show');
  document.getElementById('generateBtn').disabled = true;
  document.getElementById('fileInput').value = '';
  document.getElementById('progressWrap').classList.remove('show');
  document.getElementById('progressFill').style.width = '0%';
  document.getElementById('status-text').textContent = 'READY · PORT 7474';
  showNotif('success', '🗑 Cleared', 'File selection removed.');
}

function formatSize(b) {
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b/1024).toFixed(1) + ' KB';
  return (b/1048576).toFixed(1) + ' MB';
}

async function generate() {
  if (!selectedFile) return;
  const btn = document.getElementById('generateBtn');
  btn.disabled = true;
  const pw = document.getElementById('progressWrap');
  const pf = document.getElementById('progressFill');
  const pl = document.getElementById('progressLabel');
  pw.classList.add('show');
  for (const [pct, label] of [
    [10,'READING FILE...'], [30,'PARSING DXDIAG DATA...'],
    [50,'DETECTING GPU ROLES...'], [65,'EXTRACTING PORTS & DEVICES...'],
    [80,'BUILDING HTML PROFILE...'], [100,'FINALIZING...']
  ]) {
    pl.textContent = label; pf.style.width = pct + '%'; await sleep(280);
  }
  const formData = new FormData();
  formData.append('file', selectedFile);
  try {
    document.getElementById('status-text').textContent = 'PROCESSING...';
    const resp = await fetch('/upload', { method: 'POST', body: formData });
    const result = await resp.json();
    if (result.success) {
      showNotif('success', '✅ Profile Generated!', result.message);
      document.getElementById('status-text').textContent = 'DONE · ' + result.output_name;
      addOutputFile(result.output_name, result.output_path);
    } else {
      showNotif('error', '❌ Error', result.error);
      document.getElementById('status-text').textContent = 'ERROR';
    }
  } catch(e) { showNotif('error', '❌ Network Error', e.message); }
  pw.classList.remove('show'); pf.style.width = '0%'; btn.disabled = false;
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function addOutputFile(name, path) {
  const grid = document.getElementById('outputGrid');
  const es = grid.querySelector('.empty-state');
  if (es) es.remove();
  const div = document.createElement('div');
  div.className = 'output-file';
  div.innerHTML = `
    <span class="of-name">📊 ${name}</span>
    <div class="of-actions">
      <a class="of-open" href="/open?path=${encodeURIComponent(path)}" target="_blank">OPEN ↗</a>
      <button class="of-delete" onclick="deleteReport(this)" data-path="${path}" title="Delete">🗑</button>
    </div>`;
  grid.prepend(div);
}

async function deleteReport(btn) {
  const path = btn.dataset.path;
  const card = btn.closest('.output-file');
  const name = card.querySelector('.of-name').textContent.replace('📊 ','');
  if (!confirm(`Delete "${name}"?\nThis permanently removes the file from disk.`)) return;
  btn.disabled = true; btn.textContent = '⏳';
  try {
    const resp = await fetch('/delete', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({path}) });
    const result = await resp.json();
    if (result.success) {
      card.style.transition = 'opacity .3s,transform .3s';
      card.style.opacity = '0'; card.style.transform = 'translateX(16px)';
      setTimeout(() => {
        card.remove();
        const grid = document.getElementById('outputGrid');
        if (!grid.querySelector('.output-file')) grid.innerHTML = '<div class="empty-state"><div class="es-icon">🗂️</div>Generated HTML profiles will appear here.<br>Output saved to: <span style="color:var(--cyan)">~/DxDiag_Outputs/</span></div>';
      }, 320);
      showNotif('success', '🗑 Deleted', result.message);
    } else { showNotif('error','❌ Failed', result.error); btn.disabled=false; btn.textContent='🗑'; }
  } catch(e) { showNotif('error','❌ Network Error',e.message); btn.disabled=false; btn.textContent='🗑'; }
}

let notifTimer;
function showNotif(type, title, msg) {
  const n = document.getElementById('notification');
  document.getElementById('notifIcon').textContent = type==='success' ? '✅' : '❌';
  document.getElementById('notifTitle').textContent = title;
  document.getElementById('notifMsg').textContent = msg;
  n.className = 'show ' + type;
  const bar = document.getElementById('notifBar');
  bar.style.animation = 'none'; void bar.offsetWidth; bar.style.animation = '';
  clearTimeout(notifTimer);
  notifTimer = setTimeout(() => { n.className = ''; }, 5000);
}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────
#  HTTP SERVER
# ─────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        
        # === Google Ads.txt Verification ===
        if parsed.path == "/ads.txt":
            ads_content = "google.com, pub-2657651974679200, DIRECT, f08c47fec0942fa0"
            self._send(200, "text/plain", ads_content.encode())
            return
        # ===================================

        if parsed.path in ("/", ""):
            self._send(200, "text/html", APP_HTML.encode())
        elif parsed.path == "/open":
            qs = urllib.parse.parse_qs(parsed.query)
            fp = qs.get("path", [""])[0]
            if fp and os.path.exists(fp):
                with open(fp, "rb") as f:
                    self._send(200, "text/html", f.read())
            else:
                self._send(404, "text/plain", b"File not found")
        else:
            self._send(404, "text/plain", b"Not found")
            
    def do_POST(self):
        if self.path == "/upload":
            try:
                content_type = self.headers.get("Content-Type", "")
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                boundary = re.search(r"boundary=(.+)", content_type)
                if not boundary:
                    self._json({"success": False, "error": "No boundary in multipart"})
                    return
                bnd = boundary.group(1).encode()
                parts = body.split(b"--" + bnd)
                file_data = None
                file_name = "upload.txt"
                for part in parts:
                    if b"filename=" in part:
                        fn_m = re.search(rb'filename="(.+?)"', part)
                        if fn_m:
                            file_name = fn_m.group(1).decode()
                        idx = part.find(b"\r\n\r\n")
                        if idx != -1:
                            file_data = part[idx+4:].rstrip(b"\r\n")
                if file_data is None:
                    self._json({"success": False, "error": "No file data received"})
                    return
                ext = pathlib.Path(file_name).suffix.lower()
                tmp = UPLOAD_DIR / f"_tmp_{int(time.time())}{ext}"
                with open(tmp, "wb") as f:
                    f.write(file_data)
                text = read_file(str(tmp))
                data = parse_dxdiag(text)
                html = generate_html(data)
                safe_model = re.sub(r"[^\w\-]", "_", data.get("model", "System"))[:30]
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                out_name = f"{safe_model}_{ts}.html"
                out_path = UPLOAD_DIR / out_name
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(html)
                tmp.unlink(missing_ok=True)
                self._json({"success": True,
                            "message": f"Profile saved to DxDiag_Outputs/{out_name}",
                            "output_name": out_name,
                            "output_path": str(out_path)})
            except Exception as e:
                self._json({"success": False, "error": str(e) + "\n" + traceback.format_exc()[:600]})

        elif self.path == "/delete":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                payload = json.loads(body.decode("utf-8"))
                fp = payload.get("path", "")
                target = pathlib.Path(fp).resolve()
                if str(target).startswith(str(UPLOAD_DIR.resolve())) and target.exists():
                    target.unlink()
                    self._json({"success": True, "message": f"Deleted {target.name}"})
                else:
                    self._json({"success": False, "error": "File not found or not permitted"})
            except Exception as e:
                self._json({"success": False, "error": str(e)})
        else:
            self._send(404, "text/plain", b"Not found")

    def _send(self, code, ct, body):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self._send(200, "application/json", body)

# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

import os
PORT = int(os.environ.get("PORT", 5000))

def main():
    print("\n" + "═"*58)
    print("  ⚡  DxDiag Extractor  v5  ·  by Claude")
    print("═"*58)
    print(f"  🌐  http://localhost:{PORT}")
    print(f"  📁  Output dir: {UPLOAD_DIR}")
    print(f"  🛑  Press Ctrl+C to stop")
    print("  🆕  Fixes: GPU role detection, Ports tab, Performance tab,")
    print("      CPU deep dive, device detection, side-by-side GPU cards")
    print("═"*58 + "\n")
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    def open_browser():
        time.sleep(0.8)
        webbrowser.open(f"http://localhost:{PORT}")
    t = threading.Thread(target=open_browser, daemon=True)
    t.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\n  👋  Server stopped. Goodbye!\n")
        server.shutdown()

if __name__ == "__main__":
    main()
