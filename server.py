#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redmi K80 Pro (miro) MCA charging real-time dashboard backend.

Reads all charging real-time sysfs nodes via ADB (root) every <interval>
seconds, keeps a short history, and serves:

    /           -> index.html (single-page dashboard, auto refresh)
    /api/data   -> JSON snapshot used by the page

Usage:
    python server.py                    # defaults: adb 192.168.5.13:5555, port 8765, 3s
    python server.py --port 9000 --interval 5 --adb-host 192.168.1.10:5555

The page polls /api/data every 3 s (or the server's configured interval).
When the ADB device is unreachable the page shows an offline/error state;
no fake data is generated.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


BASE_SYSFS = "/sys/devices/platform/soc/"
BATTERY_UEVENT = "/sys/class/power_supply/battery/uevent"
MCA_LOG_DIR = "/data/vendor/bsplog/charge/charge_logger/mca_log"
THERMAL_DUMP = "/data/vendor/thermal/thermal.dump"

# thermal-*.conf 无线监控段名前缀 -> 场景（来自设备解密配置）
THERMAL_SCENES = {
    "MONITOR-WIRELESS": "normal（日常）",
    "CHARGE-MONITOR-WIRELESS": "charge（充电中）",
    "CHG-ONLY-MONITOR-WIRELESS": "chg-only（熄屏充电）",
    "4K-MONITOR-WIRELESS": "4k（4K 录像）",
    "ARVR-MONITOR-WIRELESS": "arvr（AR/VR）",
    "CAMERA-MONITOR-WIRELESS": "camera（相机）",
    "CCLASSVIDEO-MONITOR-WIRELESS": "cclassvideo（连续视频）",
    "CGAME-MONITOR-WIRELESS": "cgame（连续游戏）",
    "CLASS0-MONITOR-WIRELESS": "class0",
    "DANMU-MONITOR-WIRELESS": "danmu（弹幕）",
    "HIGHFPS-MONITOR-WIRELESS": "highfps（高帧率）",
    "HP-GAME-MONITOR-WIRELESS": "hp-mgame（高性能游戏）",
    "HP-NORMAL-MONITOR-WIRELESS": "hp-normal（高性能常规）",
    "HUANJI-MONITOR-WIRELESS": "huanji（幻迹）",
    "MGAME-MONITOR-WIRELESS": "mgame（中度游戏）",
    "NAVIGATION-MONITOR-WIRELESS": "navigation（导航）",
    "NOLIMITS-MONITOR-WIRELESS": "nolimits（无限制）",
    "PER-CLASS0-MONITOR-WIRELESS": "per-class0（性能 Class0）",
    "PER-NORMAL-MONITOR-WIRELESS": "per-normal（性能常规）",
    "PER-VIDEO-MONITOR-WIRELESS": "per-video（性能视频）",
    "PHONE-MONITOR-WIRELESS": "phone（通话）",
    "TGAME-MONITOR-WIRELESS": "tgame（重度游戏）",
    "VIDEO-MONITOR-WIRELESS": "video（视频）",
    "VIDEOCHAT-MONITOR-WIRELESS": "videochat（视频通话）",
    "XINGTIE-MONITOR-WIRELESS": "xingtie（星穹铁道）",
    "YUANSHEN-MONITOR-WIRELESS": "yuanshen（原神）",
}

# Every live node collected from the device.  group/label are used by the
# HTML page to render "all charging real-time data" in one page.
NODES = [
    # --- 私有快充协商 (soc:mca_business_charger) ---
    dict(id="quick_charge_type", path="soc:mca_business_charger/quick_charge_type",
         label="私有快充类型", group="私有快充协商", unit="", fmt="text"),
    dict(id="real_type", path="soc:mca_business_charger/real_type",
         label="真实协议名", group="私有快充协商", unit="", fmt="text"),
    dict(id="power_max", path="soc:mca_business_charger/power_max",
         label="协商最大功率", group="私有快充协商", unit="W", fmt="num"),
    dict(id="is_eu_model", path="soc:mca_business_charger/is_eu_model",
         label="是否欧版", group="私有快充协商", unit="", fmt="text"),

    # --- 无线策略实时 (soc:mca_strategy_basic_wireless_class) ---
    dict(id="wls_debug", path="soc:mca_strategy_basic_wireless_class/wls_debug",
         label="无线实时参数 vout/vrect/iout", group="无线策略实时", unit="", fmt="wls"),
    dict(id="wls_fc_flag", path="soc:mca_strategy_basic_wireless_class/wls_fc_flag",
         label="快充成功标志", group="无线策略实时", unit="", fmt="num"),
    dict(id="wls_car_adapter", path="soc:mca_strategy_basic_wireless_class/wls_car_adapter",
         label="车载适配器标志", group="无线策略实时", unit="", fmt="num"),
    dict(id="audio_phone_sts", path="soc:mca_strategy_basic_wireless_class/audio_phone_sts",
         label="音频/通话状态", group="无线策略实时", unit="", fmt="num"),
    dict(id="low_inductance_offset", path="soc:mca_strategy_basic_wireless_class/low_inductance_offset",
         label="低感量偏移", group="无线策略实时", unit="", fmt="num"),

    # --- 限流 / 电流投票 (soc:mca_charge_interface, soc:mca_charger_thermal) ---
    dict(id="ichg_limit", path="soc:mca_charge_interface/ichg_limit",
         label="充电电流投票结果", group="电流投票与限流", unit="", fmt="ichg"),
    dict(id="charge_enable", path="soc:mca_charge_interface/charge_enable",
         label="充电使能投票", group="电流投票与限流", unit="", fmt="text"),
    dict(id="wireless_chg_curr", path="soc:mca_charger_thermal/wireless_chg_curr",
         label="无线热控限流", group="电流投票与限流", unit="mA", fmt="num"),

    # --- 电荷泵 / 电池 BTB ---
    dict(id="ibus_total", path="soc:mca_platform_cp/ibus_total",
         label="电荷泵总线电流", group="电荷泵与电池", unit="mA", fmt="num"),
    dict(id="ibus_delta", path="soc:mca_platform_cp/ibus_delta",
         label="电荷泵总线电流差", group="电荷泵与电池", unit="mA", fmt="num"),
    dict(id="btb_master_status", path="soc:mca_bmd/btb_master_status",
         label="BTB 主/从状态（单电芯双接口）", group="电荷泵与电池", unit="", fmt="text"),

    # --- 芯片 / 系统 ---
    dict(id="wireless_chip_fw", path="soc:mca_strategy_wireless_revchg_class/wireless_chip_fw",
         label="无线芯片固件", group="芯片与系统", unit="", fmt="text"),
]


def now_iso() -> str:
    local = datetime.now(timezone(timedelta(hours=8)))
    return local.strftime("%Y-%m-%d %H:%M:%S")


def run_cmd(cmd: list[str], timeout: float = 8.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace", creationflags=0)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        raise
    except subprocess.TimeoutExpired:
        return -1, "", "adb command timeout"


class AdbReader:
    """Batches all sysfs reads into two adb shell calls (root via su)."""

    def __init__(self, adb_host: str | None, serial: str | None, adb_arg: str):
        self.adb_bin = self._resolve_bin(adb_arg)
        self.adb_host = adb_host
        self.serial = serial
        self.available = False
        self.last_error = ""
        self.utc_offset_minutes = 0
        self._init()
        if self.available:
            self.utc_offset_minutes = self._get_utc_offset()

    @staticmethod
    def _resolve_bin(adb_arg: str) -> str | None:
        """auto: try C:\\adb, then PATH. Explicit --adb wins."""
        candidates = []
        if adb_arg and adb_arg != "auto":
            candidates.append(adb_arg)
        candidates += ["C:\\adb\\adb.exe", "C:\\adb\\adb", "adb"]
        for cand in candidates:
            path = cand
            if os.path.isdir(path):
                path = os.path.join(path, "adb.exe")
            if os.path.isfile(path):
                return path
            if not os.path.dirname(path):
                found = shutil.which(path)
                if found:
                    return found
        return None

    def _run(self, args: list[str], timeout: float = 10.0, with_serial: bool = True):
        if not self.adb_bin:
            return -1, "", "no adb binary"
        cmd = [self.adb_bin]
        if with_serial and self.serial:
            cmd += ["-s", self.serial]
        cmd += args
        return run_cmd(cmd, timeout=timeout)

    def _get_utc_offset(self) -> int:
        """Device UTC offset in minutes, e.g. +08:00 -> 480. 0 on failure."""
        try:
            code, out, _ = self._run(["shell", "date", "+%z"], timeout=5)
            if code == 0:
                m = re.fullmatch(r"([+-])(\d{2})(\d{2})", out.strip())
                if m:
                    sign = 1 if m.group(1) == "+" else -1
                    return sign * (int(m.group(2)) * 60 + int(m.group(3)))
        except FileNotFoundError:
            pass
        return 0

    def _init(self):
        if not self.adb_bin:
            self.last_error = "adb not found (auto: C:\\adb\\adb.exe or PATH); use --adb"
            return
        try:
            if self.adb_host:
                self._run(["connect", self.adb_host], timeout=5, with_serial=False)
            code, out, _ = self._run(["devices"], timeout=5, with_serial=False)
            if code != 0:
                self.last_error = "adb devices failed"
                return
            serials = [line.split("\t")[0] for line in out.splitlines()[1:]
                       if line.strip() and "\tdevice" in line]
            if self.serial:
                self.available = self.serial in serials
            elif serials:
                self.serial = serials[0]
                self.available = True
            if not self.available:
                self.last_error = "no ADB device in 'device' state"
        except FileNotFoundError:
            self.last_error = "adb executable not found in PATH"

    def read_batch(self) -> dict[str, dict]:
        """Returns {node_id: {raw, ok, value}} for all NODES plus battery uevent."""
        paths = [BASE_SYSFS + n["path"] for n in NODES] + [BATTERY_UEVENT]
        if not self.available:
            return {}
        script = "; ".join(f"echo '###{i}'; cat '{p}'" for i, p in enumerate(paths))
        try:
            last = None
            attempts = [
                (["shell", "su", "-c", f'"{script}"'], "su"),
                (["shell", script], "plain"),
            ]
            for cmd, label in attempts:
                code, out, err = self._run(cmd, timeout=12)
                if code == -1:
                    last = (code, out, err, label)
                    continue
                result = self._split_output(out, len(paths))
                if "battery_uevent" in result:
                    return result
                last = (code, out, err, label)
            code, out, err, label = last
            self.last_error = f"read failed ({label}): " + (err or out).strip()[:160]
            return {}
        except FileNotFoundError:
            self.available = False
            self.last_error = "adb executable not found in PATH"
            return {}

    def read_vote_logs(self, tail_bytes: int = 2097152) -> str:
        """Tail the newest mca_log, filtered to mca_vote lines."""
        if not self.available:
            return ""
        try:
            code, out, _ = self._run(
                ["shell", "su", "-c", f'"ls -t {MCA_LOG_DIR}/ | head -n 1"'], timeout=10)
            if code != 0 or not out.strip():
                return ""
            fname = out.strip().splitlines()[0]
            if not re.fullmatch(r"[A-Za-z0-9_.\-]+", fname):
                return ""
            path = f"{MCA_LOG_DIR}/{fname}"
            code, out, _ = self._run(
                ["shell", "su", "-c", f'"tail -c {tail_bytes} {path} | grep -a -E \'mca_vote\'"'],
                timeout=15)
            return out if code == 0 else ""
        except FileNotFoundError:
            return ""

    def read_session_logs(self, tail_bytes: int = 4194304, file_count: int = 3) -> str:
        """Grep session events from the newest mca_log files (chronological)."""
        if not self.available:
            return ""
        pattern = ("power_good|AUTHEN_FINISH|uuid_value|TX_ADAPTER|"
                   "FAST_CHARGE|fast chg success|set chg current|open path ibus|"
                   "smartchg_soc_limit_callback|strategy_wireless_get_qc_enable|"
                   "strategy_wireless_get_charging_info")
        try:
            code, out, _ = self._run(
                ["shell", "su", "-c", f'"ls -t {MCA_LOG_DIR}/ | head -n {file_count}"'], timeout=10)
            if code != 0 or not out.strip():
                return ""
            files = sorted(f for f in out.strip().splitlines()
                           if re.fullmatch(r"[A-Za-z0-9_.\-]+", f))[:file_count]
            if not files:
                return ""
            script = "; ".join(
                f"tail -c {tail_bytes} {MCA_LOG_DIR}/{f} | grep -a -E '{pattern}' | grep -v sysfs_show"
                for f in files)
            code, out, _ = self._run(["shell", "su", "-c", f'"{script}"'], timeout=25)
            return out if code == 0 else ""
        except FileNotFoundError:
            return ""

    def read_thermal_dump(self, tail_bytes: int = 65536) -> str:
        """Tail the newest thermal.dump lines (mi_thermald live state)."""
        if not self.available:
            return ""
        try:
            code, out, _ = self._run(
                ["shell", "su", "-c",
                 f'"tail -c {tail_bytes} {THERMAL_DUMP} | grep -a -E \'MONITOR-WIRELESS\' | tail -n 3"'],
                timeout=15)
            return out if code == 0 else ""
        except FileNotFoundError:
            return ""

    @staticmethod
    def _split_output(out: str, count: int) -> dict[str, dict]:
        blocks: dict[int, str] = {}
        cur = -1
        for line in out.splitlines():
            if line.startswith("###") and line[3:].isdigit():
                cur = int(line[3:])
                blocks.setdefault(cur, [])
            elif cur >= 0:
                blocks[cur].append(line)
        result: dict[str, dict] = {}
        for i, lines in blocks.items():
            if i == count - 1:
                result["battery_uevent"] = {"raw": "\n".join(lines), "ok": bool(lines)}
            elif i < count - 1:
                raw = "\n".join(lines).strip()
                node = NODES[i]
                result[node["id"]] = {"raw": raw, "value": raw, "ok": bool(raw)}
        return result


def parse_wls_debug(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for m in re.finditer(r"([A-Za-z_]+)\s*=\s*(-?\d+(?:\.\d+)?)", text):
        out[m.group(1)] = float(m.group(2))
    return out


def parse_uevent(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            key = k.strip()
            if key.startswith("POWER_SUPPLY_"):
                key = key[len("POWER_SUPPLY_"):]
            out[key] = v.strip()
    return out


def num(raw: str) -> float | None:
    try:
        return float(raw.strip())
    except (TypeError, ValueError):
        return None


VOTE_TIME_RE = re.compile(r"\[(\d{2}:\d{2}:\d{2}:\d{3})")
VOTE_CHANGED_RE = re.compile(r"mca_vote:\d+ (\w+): ([A-Za-z0-9_.]+),(\d+) voting on of val=(-?\d+)")
VOTE_RESULT_RE = re.compile(r"mca_vote:\d+ (\w+): effective vote is now (-?\d+) voted by ([A-Za-z0-9_.]+),(\d+)")
VOTE_HEADER_RE = re.compile(r"mca_vote:\d+ (\w+) VOTER:")
VOTE_ROW_RE = re.compile(r"(\d+)\.([A-Za-z0-9_.]+)\s+(\d+)\s+(-?\d+)")
VOTE_UNITS = {
    "term_volt": "mV",            # 终止电压阈值（电压）
    "chg_enable": "",             # 充电使能开关（0/1，无单位）
    "quick_chg_disable": "",      # 快充禁用开关（0/1，无单位）
    "wls_quick_chg_disable": "",  # 无线快充禁用开关（0/1，无单位）
}


def shift_log_time(time_str: str, offset_minutes: int) -> str:
    """Shift a HH:MM:SS:mmm kernel log timestamp by the device UTC offset."""
    m = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}):(\d{3})", time_str)
    if not m or not offset_minutes:
        return time_str
    total = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
             + offset_minutes * 60) % 86400
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}:{m.group(4)}"


def parse_vote_blocks(text: str, offset_minutes: int = 0) -> dict:
    """Parse the latest mca_vote table per topic from mca_log output."""
    blocks: dict[str, dict] = {}
    current: dict | None = None
    pending_changed: dict | None = None
    pending_result: dict | None = None
    for line in text.splitlines():
        m = VOTE_CHANGED_RE.search(line)
        if m:
            pending_changed = {
                "topic": m.group(1), "client": m.group(2),
                "idx": int(m.group(3)), "value": int(m.group(4)),
            }
            continue
        m = VOTE_RESULT_RE.search(line)
        if m:
            pending_result = {
                "topic": m.group(1), "value": int(m.group(2)),
                "client": m.group(3), "idx": int(m.group(4)),
            }
            continue
        m = VOTE_HEADER_RE.search(line)
        if m:
            topic = m.group(1)
            tm = VOTE_TIME_RE.search(line)
            current = {
                "topic": topic,
                "time": shift_log_time(tm.group(1), offset_minutes) if tm else "",
                "unit": VOTE_UNITS.get(topic, "mA"),
                "changed": pending_changed if pending_changed and pending_changed["topic"] == topic else None,
                "result": pending_result if pending_result and pending_result["topic"] == topic else None,
                "rows": [],
            }
            blocks[topic] = current
            pending_changed = None
            pending_result = None
            continue
        if current is not None:
            m = VOTE_ROW_RE.search(line)
            if m:
                current["rows"].append({
                    "idx": int(m.group(1)), "client": m.group(2),
                    "enable": int(m.group(3)), "value": int(m.group(4)),
                })
    return blocks


def _log_seconds(time_str: str) -> int:
    m = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}):(\d{3})", time_str)
    if not m:
        return 0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def parse_epp_status(text: str) -> str | None:
    """Extract the latest EPP negotiation flag (epp:0/1) from mca_log lines."""
    last = None
    for line in text.splitlines():
        m = re.search(r"\bepp:(\d)", line)
        if m:
            last = m.group(1)
    return last


def parse_wls_icl(text: str) -> int | None:
    """Extract the latest wireless loop icl (driver-applied wireless input limit)."""
    last = None
    for line in text.splitlines():
        m = re.search(r"wireless loop: icl:(\d+)", line)
        if m:
            last = int(m.group(1))
    return last


THERMAL_WIRELESS_RE = re.compile(
    r"\[([A-Z0-9\-]*MONITOR-WIRELESS)\]\[VIRTUAL-SENSOR-FORMULA (\d+)\]")
THERMAL_TARGET_RE = re.compile(r"\[wireless_charge (\d+)\]")


def parse_thermal_dump(text: str) -> dict:
    """Parse mi_thermald live state: scene, virtual temp, wireless target."""
    result: dict = {"scene": None, "virtual_temp": None, "target": None}
    for line in text.splitlines():
        m = THERMAL_WIRELESS_RE.search(line)
        if not m:
            continue
        seg = m.group(1)
        result["scene"] = THERMAL_SCENES.get(seg, seg)
        result["virtual_temp"] = int(m.group(2)) / 1000.0
        t = THERMAL_TARGET_RE.search(line)
        if t:
            result["target"] = int(t.group(1))
    return result


def classify_session_line(line: str):
    """Map an mca_log line to (kind, label, detail) or None."""
    if "wireless power_good_off" in line:
        return ("off", "充电板移除", "")
    if "wireless power_good_on" in line:
        return ("on", "充电板接入", "")
    if "RX_INT_AUTHEN_FINISH" in line:
        return ("auth", "私有协议认证完成", "")
    m = re.search(r"uuid_value is (\S+)", line)
    if m:
        return ("uuid", "认证 UUID", m.group(1))
    m = re.search(r"POWER_SUPPLY_TX_ADAPTER=(\d+)", line)
    if m:
        return ("tx", "发射端识别", f"TX_ADAPTER={m.group(1)}")
    if "RX_INT_FAST_CHARGE" in line:
        return ("fc", "快充协商成功", "")
    m = re.search(r"fast chg success: (\d+)", line)
    if m:
        return ("fcflag", "快充成功标志", m.group(1))
    m = re.search(r"set chg current (\d+)", line)
    if m:
        return ("ichg", "设置充电电流", m.group(1))
    m = re.search(r"open path ibus (\d+)", line)
    if m:
        return ("open", "打开快充路径", m.group(1))
    if "smartchg_soc_limit_callback" in line and "effective_result: 1" in line:
        return ("smart", "SmartEndura 介入", "")
    return None


def parse_sessions(text: str, offset_minutes: int = 0) -> list:
    """Group mca_log handshake/limit events into charging sessions (newest last)."""
    sessions: list[dict] = []
    cur: dict | None = None
    last_ts: str | None = None
    for line in text.splitlines():
        tm = VOTE_TIME_RE.search(line)
        ev = classify_session_line(line)
        if not tm or not ev:
            continue
        t = shift_log_time(tm.group(1), offset_minutes)
        kind, label, detail = ev
        if kind == "on":
            if cur and cur["events"] and not cur["ended"]:
                cur["ended"] = True
            cur = {
                "start": t, "ended": False, "events": [],
                "uuid": None, "tx_adapter": None, "fc_flag": None,
                "limits": [], "opens": 0, "smartendura": False,
            }
            sessions.append(cur)
        elif kind == "off":
            if cur:
                cur["ended"] = True
            cur = None
            last_ts = t
            continue
        else:
            gap = 0
            if last_ts:
                gap = _log_seconds(t) - _log_seconds(last_ts)
            if cur is None or gap > 300:
                cur = {
                    "start": t, "ended": False, "events": [],
                    "uuid": None, "tx_adapter": None, "fc_flag": None,
                    "limits": [], "opens": 0, "smartendura": False,
                }
                sessions.append(cur)
        last_ts = t
        if kind == "uuid" and not cur["uuid"]:
            cur["uuid"] = detail
        elif kind == "tx" and not cur["tx_adapter"]:
            cur["tx_adapter"] = detail
        elif kind == "fcflag" and not cur["fc_flag"]:
            cur["fc_flag"] = detail
        elif kind == "ichg":
            cur["limits"].append(int(detail))
        elif kind == "open":
            cur["opens"] += 1
        elif kind == "smart":
            cur["smartendura"] = True

        if kind in ("ichg", "open"):
            exists = any(e["kind"] == kind for e in cur["events"])
            if not exists:
                cur["events"].append({"kind": kind, "time": t, "label": label, "detail": detail})
        else:
            evs = cur["events"]
            if not (kind == "smart" and evs and evs[-1]["kind"] == "smart"):
                evs.append({"kind": kind, "time": t, "label": label, "detail": detail})

    for s in sessions[-3:]:
        s["peak_limit_ma"] = max(s["limits"]) if s["limits"] else None
        s["final_limit_ma"] = s["limits"][-1] if s["limits"] else None
        s.pop("limits", None)
        s["events"] = s["events"][-12:]
    return sessions[-3:]


class Sampler(threading.Thread):
    def __init__(self, adb: AdbReader, interval: float):
        super().__init__(daemon=True)
        self.adb = adb
        self.interval = max(1.0, float(interval))
        self.history: deque[dict] = deque(maxlen=180)
        self.lock = threading.Lock()
        self.snapshot: dict = self._build_error_snapshot("initializing")
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                self.snapshot = self.collect()
            except Exception as exc:  # keep the page alive on any sampling error
                self.snapshot = self._build_error_snapshot(str(exc))
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()

    def get(self) -> dict:
        with self.lock:
            return self.snapshot

    def _build_error_snapshot(self, msg: str) -> dict:
        return {
            "ts": time.time(), "iso": now_iso(), "mode": "offline",
            "connected": False, "error": msg, "nodes": [],
            "battery": {}, "derived": {}, "history": [], "voters": {}, "sessions": [],
            "thermal": {},
            "meta": {"interval": self.interval},
        }

    def collect(self) -> dict:
        if not self.adb.available:
            return self._build_error_snapshot(self.adb.last_error or "ADB device unavailable")
        batch = self.adb.read_batch()
        if not batch:
            return self._build_error_snapshot(self.adb.last_error or "ADB read failed")

        raw = self._normalize_live(batch)
        parsed = self._build(raw)
        parsed["mode"] = "live"
        parsed["connected"] = True
        parsed["voters"] = parse_vote_blocks(
            self.adb.read_vote_logs(), self.adb.utc_offset_minutes)
        session_log = self.adb.read_session_logs()
        parsed["sessions"] = parse_sessions(session_log, self.adb.utc_offset_minutes)
        epp = parse_epp_status(session_log)
        if epp is not None:
            parsed["nodes"].append({
                "id": "epp", "label": "EPP 协商状态", "group": "无线策略实时",
                "unit": "", "fmt": "epp", "value": epp, "ok": True,
            })
        icl = parse_wls_icl(session_log)
        if icl is not None and "wireless_buck_input" in parsed["voters"]:
            parsed["voters"]["wireless_buck_input"]["icl"] = icl
        parsed["thermal"] = parse_thermal_dump(self.adb.read_thermal_dump())

        with self.lock:
            sample = {
                "t": parsed["ts"],
                "vout": parsed["derived"].get("vout"),
                "vrect": parsed["derived"].get("vrect"),
                "iout": parsed["derived"].get("iout"),
                "input_power_w": parsed["derived"].get("input_power_w"),
                "battery_power_w": parsed["derived"].get("battery_power_w"),
                "batt_current_ma": parsed["derived"].get("batt_current_ma"),
                "batt_voltage_mv": parsed["derived"].get("batt_voltage_mv"),
                "capacity": parsed["derived"].get("capacity"),
                "temp_c": parsed["derived"].get("temp_c"),
            }
            self.history.append(sample)
            parsed["history"] = list(self.history)
        return parsed

    @staticmethod
    def _normalize_live(batch: dict[str, dict]) -> dict:
        nodes: dict[str, dict] = {}
        battery: dict[str, dict] = {}
        for n in NODES:
            nodes[n["id"]] = batch.get(n["id"], {"raw": "", "value": "", "ok": False})
        if "battery_uevent" in batch:
            parsed = parse_uevent(batch["battery_uevent"]["raw"])
            for k in ("CURRENT_NOW", "VOLTAGE_NOW", "CAPACITY",
                      "TEMP", "STATUS", "HEALTH", "CYCLE_COUNT", "CHARGE_FULL",
                      "TECHNOLOGY", "CHARGE_COUNTER", "VOLTAGE_MAX_DESIGN",
                      "INPUT_CURRENT_LIMIT", "TIME_TO_FULL_NOW",
                      "MODEL_NAME", "PRESENT", "CAPACITY_LEVEL"):
                v = parsed.get(k, "")
                battery[k.lower()] = {"raw": v, "value": v, "ok": bool(v)}
        return {"nodes": nodes, "battery": battery}

    def _build(self, raw: dict) -> dict:
        nodes = raw["nodes"]
        battery = raw["battery"]
        ts = time.time()
        wls = parse_wls_debug(nodes.get("wls_debug", {}).get("value", ""))

        batt_cur_ma = num(battery.get("current_now", {}).get("value", ""))
        batt_vol_mv = num(battery.get("voltage_now", {}).get("value", ""))
        if batt_cur_ma is not None:
            batt_cur_ma = batt_cur_ma / 1000.0          # uA -> mA
        if batt_vol_mv is not None:
            batt_vol_mv = batt_vol_mv / 1000.0          # uV -> mV
        temp_raw = num(battery.get("temp", {}).get("value", ""))
        temp_c = temp_raw / 10.0 if temp_raw is not None else None
        capacity = num(battery.get("capacity", {}).get("value", ""))

        vout = wls.get("vout")
        iout = wls.get("iout")
        input_power = (vout * iout / 1e6) if vout is not None and iout is not None else None
        battery_power = (abs(batt_cur_ma) * batt_vol_mv / 1e6) if batt_cur_ma is not None and batt_vol_mv is not None else None

        derived = {
            "vout": vout, "vrect": wls.get("vrect"), "iout": iout,
            "input_power_w": round(input_power, 2) if input_power is not None else None,
            "battery_power_w": round(battery_power, 2) if battery_power is not None else None,
            "batt_current_ma": round(batt_cur_ma, 1) if batt_cur_ma is not None else None,
            "batt_voltage_mv": round(batt_vol_mv, 1) if batt_vol_mv is not None else None,
            "capacity": capacity, "temp_c": temp_c,
            "tx_adapter": wls.get("tx_adapter"),
        }

        node_list = []
        for n in NODES:
            item = nodes.get(n["id"], {"raw": "", "ok": False})
            node_list.append({
                "id": n["id"], "label": n["label"], "group": n["group"],
                "unit": n["unit"], "fmt": n["fmt"], "value": item.get("value", ""),
                "ok": item.get("ok", False),
            })

        return {
            "ts": ts,
            "iso": now_iso(),
            "nodes": node_list,
            "battery": battery,
            "derived": derived,
            "meta": {"interval": self.interval, "adb": getattr(self.adb, "serial", None) or ""},
        }


class Handler(BaseHTTPRequestHandler):
    server: "DashboardServer"

    def do_GET(self):
        if self.path == "/api/data":
            data = self.server.sampler.get()
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if self.path in ("/", "/index.html"):
            index = self.server.index_html
            if index is None:
                self.send_error(404, "index.html not found next to server.py")
                return
            body = index.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} - {fmt % args}")


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, sampler: Sampler, index_html: str | None):
        self.sampler = sampler
        self.index_html = index_html
        super().__init__(addr, Handler)


def main():
    parser = argparse.ArgumentParser(description="MCA charging real-time dashboard")
    parser.add_argument("--adb-host", default="",
                        help="ADB over Wi-Fi address host:port；留空则自动使用 adb devices 中已连接的设备")
    parser.add_argument("--serial", default=None, help="adb -s serial override")
    parser.add_argument("--adb", default="auto",
                        help="adb executable or folder (default: auto-detect C:\\adb or PATH)")
    parser.add_argument("--interval", type=float, default=3.0,
                        help="sample/refresh interval in seconds (default 3)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default 8765)")
    parser.add_argument("--open", action="store_true", help="open the page in the default browser")
    args = parser.parse_args()

    base = os.path.dirname(os.path.abspath(__file__))
    index_path = os.path.join(base, "index.html")
    index_html = None
    if os.path.isfile(index_path):
        with open(index_path, "r", encoding="utf-8") as fh:
            index_html = fh.read()

    adb = AdbReader(args.adb_host, args.serial, args.adb)
    if not adb.available:
        print(f"[warn] ADB device unavailable ({adb.last_error}); page will show offline state.")
    sampler = Sampler(adb, args.interval)
    sampler.start()

    server = DashboardServer(("127.0.0.1", args.port), sampler, index_html)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"[ok] dashboard running: {url}  (interval={args.interval}s, adb={adb.adb_bin or 'not found'})")
    if args.open:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sampler.stop()
        server.server_close()


if __name__ == "__main__":
    main()
