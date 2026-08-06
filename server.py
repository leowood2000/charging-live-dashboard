#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redmi K80 Pro (miro) MCA charging real-time dashboard backend (ADB 版).

数据语义与安卓版 SnapshotCollector.java 对齐：
- 快速采集（sysfs + battery + thermal + history）默认 3 秒
- 日志采集（voters / sessions / EPP / 实际下发 ICL）默认 20 秒
- 电池电流约定：充电为正、放电为负；电池功率保留正负号
- 日志读取失败保留上次成功数据，并由 logs_stale 标记
- 会话/投票/仲裁输出与安卓版同构

Usage:
    python server.py                    # defaults: adb 192.168.5.13:5555, port 8765
    python server.py --port 9000 --interval 5 --logs-interval 20 --adb-host 192.168.1.10:5555

The page polls /api/data every fast_interval seconds. When the ADB device
is unreachable the page shows an offline/error state; no fake data is generated.
"""

from __future__ import annotations

import argparse
import copy
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


# Every live node collected from the device.  group/label/unit/fmt 与安卓版
# SnapshotCollector.java 保持一致；ua_to_ma 表示原始值 µA，页面换算成 mA。
NODES = [
    # --- 私有快充协商 (soc:mca_business_charger) ---
    dict(id="quick_charge_type", path="soc:mca_business_charger/quick_charge_type",
         label="私有快充类型", group="私有快充协商", unit="", fmt="text"),
    dict(id="real_type", path="soc:mca_business_charger/real_type",
         label="驱动协议类型", group="私有快充协商", unit="", fmt="text"),
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

    # --- 有线策略实时 (soc:mca_charger_thermal) ---
    dict(id="wired_chg_curr", path="soc:mca_charger_thermal/wired_chg_curr",
         label="有线热控电流上限", group="有线策略实时", unit="mA", fmt="ua_to_ma"),
    dict(id="wired_ctrl_limit", path="soc:mca_charger_thermal/wired_ctrl_limit",
         label="有线热控等级", group="有线策略实时", unit="", fmt="num"),

    # --- 限流 / 电流投票 (soc:mca_charge_interface, soc:mca_charger_thermal) ---
    dict(id="ichg_limit", path="soc:mca_charge_interface/ichg_limit",
         label="充电电流投票结果", group="电流投票与限流", unit="", fmt="ichg"),
    dict(id="charge_enable", path="soc:mca_charge_interface/charge_enable",
         label="充电使能投票", group="电流投票与限流", unit="", fmt="text"),
    dict(id="wireless_chg_curr", path="soc:mca_charger_thermal/wireless_chg_curr",
         label="无线热控电流上限", group="电流投票与限流", unit="mA", fmt="ua_to_ma"),

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
        self.last_reconnect_at = time.monotonic()
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
            if self.adb_host:
                # 指定 --adb-host 时优先使用该设备，而不是取 devices 第一台
                self.available = self.adb_host in serials
                if self.available:
                    self.serial = self.adb_host
            elif self.serial:
                self.available = self.serial in serials
            elif serials:
                self.serial = serials[0]
                self.available = True
            if not self.available:
                self.last_error = "no ADB device in 'device' state"
        except FileNotFoundError:
            self.last_error = "adb executable not found in PATH"

    def ensure_connected(self) -> bool:
        """ADB 掉线后每 5 秒节流重试 connect + devices，支持自动恢复。"""
        now = time.monotonic()
        if self.available:
            return True
        if now - self.last_reconnect_at < 5:
            return False
        self.last_reconnect_at = now
        self._init()
        return self.available

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

    def read_vote_logs(self, tail_bytes: int = 2097152) -> tuple[bool, str]:
        """Tail the newest mca_log, filtered to mca_vote lines.

        Returns (read_ok, text)：read_ok 表示 ADB/su/文件读取成功；
        grep 无匹配（退出码 1）不算读取失败，text 可能为空。
        """
        if not self.available:
            return False, ""
        try:
            code, out, _ = self._run(
                ["shell", "su", "-c", f'"ls -t {MCA_LOG_DIR}/ | head -n 1"'], timeout=10)
            if code != 0 or not out.strip():
                return False, ""
            fname = out.strip().splitlines()[0]
            if not re.fullmatch(r"[A-Za-z0-9_.\-]+", fname):
                return False, ""
            path = f"{MCA_LOG_DIR}/{fname}"
            code, out, _ = self._run(
                ["shell", "su", "-c", f'"tail -c {tail_bytes} {path} | grep -a -E \'mca_vote\'"'],
                timeout=15)
            # grep 无匹配时退出码为 1，属于“读取成功但无内容”，不算失败
            return (code in (0, 1), out if code in (0, 1) else "")
        except FileNotFoundError:
            return False, ""

    def read_session_logs(self, tail_bytes: int = 4194304, file_count: int = 3) -> tuple[bool, str]:
        """Grep session events from the newest mca_log files (chronological).

        Returns (read_ok, text)：与 read_vote_logs 相同，grep 无匹配不算失败。
        """
        if not self.available:
            return False, ""
        pattern = ("power_good|AUTHEN_FINISH|uuid_value|TX_ADAPTER|"
                   "FAST_CHARGE|fast chg success|set chg current|open path ibus|"
                   "smartchg_soc_limit_callback|strategy_wireless_get_qc_enable|"
                   "strategy_wireless_get_charging_info")
        try:
            code, out, _ = self._run(
                ["shell", "su", "-c", f'"ls -t {MCA_LOG_DIR}/ | head -n {file_count}"'], timeout=10)
            if code != 0 or not out.strip():
                return False, ""
            # ls -t 为新到旧，解析需要旧 -> 新，保证会话时间线顺序正确
            files = [
                f.strip()
                for f in out.splitlines()
                if re.fullmatch(r"[A-Za-z0-9_.\-]+", f.strip())
            ][:file_count]
            files = list(reversed(files))
            if not files:
                return False, ""
            script = "; ".join(
                f"tail -c {tail_bytes} {MCA_LOG_DIR}/{f} | grep -a -E '{pattern}' | grep -v sysfs_show"
                for f in files)
            code, out, _ = self._run(["shell", "su", "-c", f'"{script}"'], timeout=25)
            return (code in (0, 1), out if code in (0, 1) else "")
        except FileNotFoundError:
            return False, ""

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
# 与安卓版一致：client 名称允许 @ / : 等字符，且支持 voting off
VOTE_CHANGED_RE = re.compile(
    r"mca_vote:\d+ (\w+): "
    r"([A-Za-z0-9_.@:/\-]+),(\d+) "
    r"voting (on|off) of val=(-?\d+)")
VOTE_RESULT_RE = re.compile(
    r"mca_vote:\d+ (\w+): effective vote is now (-?\d+) "
    r"voted by ([A-Za-z0-9_.@:/\-]+),(\d+)")
VOTE_HEADER_RE = re.compile(r"mca_vote:\d+ (\w+) VOTER:")
VOTE_ROW_RE = re.compile(
    r"(\d+)\.([A-Za-z0-9_.@:/\-]+)\s+(\d+)\s+(-?\d+)")

# 已从 .ko 核实为电流类投票的主题才显式标注 mA，未知主题默认空单位
VOTE_UNITS = {
    "term_volt": "mV",            # 终止电压阈值（电压）
    "chg_enable": "",             # 充电使能开关（0/1，无单位）
    "quick_chg_disable": "",      # 快充禁用开关（0/1，无单位）
    "wls_quick_chg_disable": "",  # 无线快充禁用开关（0/1，无单位）
    "wireless_buck_input": "mA",
    "buck_charge_curr": "mA",
    "wireless_bpp_in": "mA",
    "wireless_bppqc2_in": "mA",
    "wireless_bppqc3_in": "mA",
    "wireless_epp_in": "mA",
    "wireless_auth_20w": "mA",
    "wireless_auth_30w": "mA",
    "wireless_auth_50w": "mA",
    "wireless_auth_80w": "mA",
    "wireless_auth_voice_box": "mA",
    "wireless_auth_magnet_30w": "mA",
    "wireless_sw_qc_ich": "mA",
    "wireless_sw_thermal_ich": "mA",
    "wls_single_chg_cur": "mA",
    "wls_multi_chg_cur": "mA",
    "div1_single": "mA",
    "div1_multi": "mA",
    "div2_single": "mA",
    "div2_multi": "mA",
    "div4_single": "mA",
    "div4_multi": "mA",
    "thermal_flip": "mA",
    "single_chg_cur": "mA",
    "multi_chg_cur": "mA",
}

# 已从 miro 固件 .ko 反汇编核实的仲裁类型：MIN/MAX/FIRST_NONZERO/FIRST_ZERO/UNKNOWN
VOTE_POLICIES = {
    # mca_basic_wireless.ko：mca_create_votable(..., 0, ...) 全部为 MIN
    "wireless_buck_input": "MIN",
    "wireless_bpp_in": "MIN",
    "wireless_bppqc2_in": "MIN",
    "wireless_bppqc3_in": "MIN",
    "wireless_epp_in": "MIN",
    "wireless_auth_20w": "MIN",
    "wireless_auth_30w": "MIN",
    "wireless_auth_50w": "MIN",
    "wireless_auth_80w": "MIN",
    "wireless_auth_voice_box": "MIN",
    "wireless_auth_magnet_30w": "MIN",
    "wireless_sw_qc_ich": "MIN",
    "wireless_sw_thermal_ich": "MIN",
    # 项目配置（用户确认）：有线 buck 充电电流按 MIN 推算
    "buck_charge_curr": "MIN",
    # mca_quick_wireless.ko：wls_single/multi_chg_cur 为 MIN，disable 为 type2（首个非零）
    "wls_single_chg_cur": "MIN",
    "wls_multi_chg_cur": "MIN",
    "wls_quick_chg_disable": "FIRST_NONZERO",
    # mca_strategy_quickchg（反编译 C）：电流类 type0，disable type2，en type3（首个为零）
    "quick_chg_disable": "FIRST_NONZERO",
    "quick_chg_en": "FIRST_ZERO",
    "div1_single": "MIN",
    "div1_multi": "MIN",
    "div2_single": "MIN",
    "div2_multi": "MIN",
    "div4_single": "MIN",
    "div4_multi": "MIN",
    "thermal_flip": "MIN",
    "single_chg_cur": "MIN",
    "multi_chg_cur": "MIN",
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
    """Parse the latest mca_vote table per topic from mca_log output.

    changed/result 按主题分别缓存，避免日志交错时 A 主题的结果被挂到 B 主题。
    """
    blocks: dict[str, dict] = {}
    current: dict | None = None
    changes_by_topic: dict[str, dict] = {}
    results_by_topic: dict[str, dict] = {}
    for line in text.splitlines():
        m = VOTE_CHANGED_RE.search(line)
        if m:
            tm = VOTE_TIME_RE.search(line)
            changes_by_topic[m.group(1)] = {
                "topic": m.group(1), "client": m.group(2),
                "idx": int(m.group(3)), "enabled": m.group(4) == "on",
                "value": int(m.group(5)),
                "time": shift_log_time(tm.group(1), offset_minutes) if tm else "",
            }
            continue
        m = VOTE_RESULT_RE.search(line)
        if m:
            tm = VOTE_TIME_RE.search(line)
            results_by_topic[m.group(1)] = {
                "topic": m.group(1), "value": int(m.group(2)),
                "client": m.group(3), "idx": int(m.group(4)),
                "time": shift_log_time(tm.group(1), offset_minutes) if tm else "",
            }
            continue
        m = VOTE_HEADER_RE.search(line)
        if m:
            topic = m.group(1)
            tm = VOTE_TIME_RE.search(line)
            current = {
                "topic": topic,
                "time": shift_log_time(tm.group(1), offset_minutes) if tm else "",
                "unit": VOTE_UNITS.get(topic, ""),
                "policy": VOTE_POLICIES.get(topic, "UNKNOWN"),
                "changed": changes_by_topic.get(topic),
                "result": results_by_topic.get(topic),
                "rows": [],
            }
            blocks[topic] = current
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


def parse_wls_icl(text: str, offset_minutes: int = 0) -> tuple[int, str] | None:
    """Extract the latest wireless loop icl (driver-applied wireless input limit).

    Returns (value, shifted local log time) so the frontend can show the value
    together with its log timestamp and collection time.
    """
    last = None
    for line in text.splitlines():
        m = re.search(r"wireless loop: icl:(\d+)", line)
        if m:
            tm = VOTE_TIME_RE.search(line)
            log_time = shift_log_time(tm.group(1), offset_minutes) if tm else ""
            last = (int(m.group(1)), log_time)
    return last


def is_last_wireless_power_off(text: str) -> bool:
    """True if the last wireless power event is removal (power_good_off)."""
    return (
        text.rfind("wireless power_good_off")
        > text.rfind("wireless power_good_on")
    )


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
    """Group mca_log handshake/limit events into charging sessions.

    与安卓版一致：
    - 以 power_good_on 建立会话，不再靠 300 秒间隔猜测
    - power_good_off 也进入“充电板移除”事件并标记会话结束
    - 所有电流变化 / open path 事件都保留
    - 会话最多 3 个，每个会话最多 100 条事件
    """
    sessions: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        tm = VOTE_TIME_RE.search(line)
        ev = classify_session_line(line)
        if not tm or not ev:
            continue
        t = shift_log_time(tm.group(1), offset_minutes)
        kind, label, detail = ev
        if kind == "on":
            # 新的 power_good_on 到来时，把上一个未结束会话标为结束
            if cur is not None and not cur["ended"]:
                cur["ended"] = True
            cur = {
                "start": t, "ended": False, "events": [],
                "uuid": None, "tx_adapter": None, "fc_flag": None,
                "opens": 0, "smartendura": False,
                "peak_limit_ma": None, "final_limit_ma": None,
            }
            sessions.append(cur)
            cur["events"].append({"kind": kind, "time": t, "label": label, "detail": detail})
            continue
        if cur is None:
            continue
        evs = cur["events"]
        if kind == "off":
            cur["ended"] = True
            evs.append({"kind": kind, "time": t, "label": label, "detail": detail})
            # 断开后不再向已结束会话追加日志
            cur = None
            continue
        elif kind == "auth":
            evs.append({"kind": kind, "time": t, "label": label, "detail": detail})
        elif kind == "uuid":
            if not cur["uuid"]:
                cur["uuid"] = detail
            evs.append({"kind": kind, "time": t, "label": label, "detail": detail})
        elif kind == "tx":
            if not cur["tx_adapter"]:
                cur["tx_adapter"] = detail
            evs.append({"kind": kind, "time": t, "label": label, "detail": detail})
        elif kind == "fc":
            evs.append({"kind": kind, "time": t, "label": label, "detail": detail})
        elif kind == "fcflag":
            cur["fc_flag"] = detail
            evs.append({"kind": kind, "time": t, "label": label, "detail": detail})
        elif kind == "ichg":
            evs.append({"kind": kind, "time": t, "label": label, "detail": detail})
            v = int(detail)
            if cur["peak_limit_ma"] is None or v > cur["peak_limit_ma"]:
                cur["peak_limit_ma"] = v
            cur["final_limit_ma"] = v
        elif kind == "open":
            cur["opens"] += 1
            evs.append({"kind": kind, "time": t, "label": label, "detail": detail})
        elif kind == "smart":
            cur["smartendura"] = True
            evs.append({"kind": kind, "time": t, "label": label, "detail": detail})

    while len(sessions) > 3:
        sessions.pop(0)
    for s in sessions:
        while len(s["events"]) > 100:
            s["events"].pop(0)
    return sessions


class Sampler:
    """双周期采集：快采集 3s（sysfs/battery/thermal/history），日志采集 20s。

    日志（voters/sessions/EPP/实际 ICL）读取失败时保留上次成功数据，
    只把 logs_stale 置 True，页面据此提示“显示上次成功数据”。
    """

    def __init__(self, adb: AdbReader, fast_interval: float, logs_interval: float):
        self.adb = adb
        self.fast_interval = max(1.0, float(fast_interval))
        self.logs_interval = max(5.0, float(logs_interval))
        self.logs_stale = False
        self.logs_updated_at = time.time() * 1000
        # 使用独立 stop_event，避免覆盖 threading.Thread 内部的 _stop()
        self.stop_event = threading.Event()
        self.history: deque[dict] = deque(maxlen=180)
        self.lock = threading.Lock()
        self.snapshot: dict = self._build_error_snapshot("initializing")
        # 日志缓存：读取失败保留上次成功数据
        self.last_voters: dict = {}
        self.last_sessions: list = []
        self.last_epp: str | None = None
        self.last_wls_icl: int | None = None
        self.last_wls_icl_at: int | None = None
        self.last_wls_icl_log_time: str | None = None
        self.last_wls_icl_key: tuple[int, str] | None = None

    def start(self) -> None:
        threading.Thread(target=self.run_fast, name="sampler-fast", daemon=True).start()
        threading.Thread(target=self.run_logs, name="sampler-logs", daemon=True).start()

    def run_fast(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.collect_fast()
            except Exception as exc:  # keep the page alive on any sampling error
                self._publish(self._build_error_snapshot(str(exc)))
            self.stop_event.wait(self.fast_interval)

    def run_logs(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.collect_logs()
            except Exception as exc:  # 日志失败不打断整体循环
                print(f"[warn] collect_logs failed: {exc}")
            self.stop_event.wait(self.logs_interval)

    def stop(self) -> None:
        self.stop_event.set()

    def get(self) -> dict:
        with self.lock:
            return self.snapshot

    def collect_fast(self) -> None:
        if not self.adb.ensure_connected():
            self._publish(self._build_error_snapshot(
                self.adb.last_error or "ADB device unavailable"))
            return
        batch = self.adb.read_batch()
        if not batch:
            self._publish(self._build_error_snapshot(
                self.adb.last_error or "ADB read failed"))
            return

        raw = self._normalize_live(batch)
        parsed = self._build(raw)
        parsed["mode"] = "live"
        parsed["connected"] = True
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
        self._publish(parsed)

    def collect_logs(self) -> None:
        if not self.adb.ensure_connected():
            return
        vote_read_ok, vote_log = self.adb.read_vote_logs()
        session_read_ok, session_log = self.adb.read_session_logs()
        # 读取成功但内容为空（如 grep 无匹配）不算失败，仅保留旧数据
        if vote_read_ok and vote_log.strip():
            parsed_voters = parse_vote_blocks(vote_log, self.adb.utc_offset_minutes)
            if parsed_voters:
                self.last_voters = parsed_voters

        if session_read_ok and session_log.strip():
            parsed_sessions = parse_sessions(session_log, self.adb.utc_offset_minutes)
            if parsed_sessions:
                self.last_sessions = parsed_sessions
            epp = parse_epp_status(session_log)
            if epp is not None:
                self.last_epp = epp
            if is_last_wireless_power_off(session_log):
                # 无线已断开：清掉旧 ICL，避免上一个会话的值继续显示
                self.last_wls_icl = None
                self.last_wls_icl_at = None
                self.last_wls_icl_log_time = None
                self.last_wls_icl_key = None
                self.last_epp = None
            else:
                icl = parse_wls_icl(session_log, self.adb.utc_offset_minutes)
                if icl is not None:
                    value, log_time = icl
                    new_key = (value, log_time)
                    # 同一日志行重复扫描时不刷新“几秒前”，避免旧值伪装成新值
                    if new_key != self.last_wls_icl_key:
                        self.last_wls_icl_key = new_key
                        self.last_wls_icl = value
                        self.last_wls_icl_log_time = log_time
                        self.last_wls_icl_at = int(time.time() * 1000)

        self.logs_stale = not vote_read_ok or not session_read_ok
        self.logs_updated_at = time.time() * 1000
        # 复制、合并日志缓存、发布在同一个锁内完成，避免旧快照覆盖新快照
        self._publish_logs_only()

    def _publish(self, core: dict) -> None:
        self._merge_cached_logs(core)
        with self.lock:
            self.snapshot = core

    def _publish_logs_only(self) -> None:
        """日志线程专用：锁内复制当前快照并合并日志缓存后发布。"""
        with self.lock:
            core = copy.deepcopy(self.snapshot)
            self._merge_cached_logs(core)
            self.snapshot = core

    def _merge_cached_logs(self, core: dict) -> None:
        core["voters"] = copy.deepcopy(self.last_voters)
        core["sessions"] = copy.deepcopy(self.last_sessions)
        if self.last_epp is not None:
            self._append_epp_node(core, self.last_epp)
        else:
            self._remove_epp_node(core)
        if self.last_wls_icl is not None:
            buck = core.get("voters", {}).get("wireless_buck_input")
            if buck is not None:
                buck["icl"] = self.last_wls_icl
                buck["icl_time"] = self.last_wls_icl_log_time or ""
                buck["icl_at"] = self.last_wls_icl_at or 0
        meta = core.setdefault("meta", {})
        meta.update({
            "interval": self.fast_interval,
            "fast_interval": self.fast_interval,
            "logs_interval": self.logs_interval,
            "logs_updated_at": int(self.logs_updated_at),
            "logs_stale": self.logs_stale,
            "adb": getattr(self.adb, "serial", None) or "",
            "source": "adb",
            "device": getattr(self.adb, "serial", None) or "",
            "schema_version": 2,
        })

    @staticmethod
    def _append_epp_node(parsed: dict, epp: str) -> None:
        nodes = parsed.get("nodes", [])
        for n in nodes:
            if n.get("id") == "epp":
                n["value"] = epp
                n["ok"] = True
                return
        nodes.append({
            "id": "epp", "label": "EPP 协商状态", "group": "无线策略实时",
            "unit": "", "fmt": "epp", "value": epp, "ok": True,
        })

    @staticmethod
    def _remove_epp_node(parsed: dict) -> None:
        parsed["nodes"] = [n for n in parsed.get("nodes", []) if n.get("id") != "epp"]

    def _build_error_snapshot(self, msg: str) -> dict:
        return {
            "ts": time.time(), "iso": now_iso(), "mode": "offline",
            "connected": False, "error": msg, "nodes": [],
            "battery": {}, "derived": {}, "history": [], "voters": {},
            "sessions": [], "thermal": {},
            "meta": {
                "interval": self.fast_interval,
                "fast_interval": self.fast_interval,
                "logs_interval": self.logs_interval,
                "logs_updated_at": int(self.logs_updated_at),
                "logs_stale": self.logs_stale,
                "adb": getattr(self.adb, "serial", None) or "",
                "source": "adb",
                "device": getattr(self.adb, "serial", None) or "",
                "schema_version": 2,
            },
        }

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

        # 统一符号：充电为正、放电为负（AOSP 约定，不依赖厂商原始符号）
        batt_status = battery.get("status", {}).get("value", "")
        batt_status_norm = batt_status.strip().lower().replace("_", " ")
        raw_current_ma = num(battery.get("current_now", {}).get("value", ""))
        if raw_current_ma is not None:
            raw_current_ma /= 1000.0          # uA -> mA
        if raw_current_ma is None:
            batt_cur_ma = None
        elif batt_status_norm == "charging":
            batt_cur_ma = abs(raw_current_ma)
        elif batt_status_norm in ("discharging", "not charging"):
            batt_cur_ma = -abs(raw_current_ma)
        else:
            batt_cur_ma = raw_current_ma

        batt_vol_mv = num(battery.get("voltage_now", {}).get("value", ""))
        if batt_vol_mv is not None:
            batt_vol_mv = batt_vol_mv / 1000.0          # uV -> mV
        temp_raw = num(battery.get("temp", {}).get("value", ""))
        temp_c = temp_raw / 10.0 if temp_raw is not None else None
        capacity = num(battery.get("capacity", {}).get("value", ""))

        vout = wls.get("vout")
        iout = wls.get("iout")
        input_power = (vout * iout / 1e6) if vout is not None and iout is not None else None
        # 电池功率保留正负号（充电为正、放电为负）
        battery_power = (
            batt_cur_ma * batt_vol_mv / 1e6
            if batt_cur_ma is not None and batt_vol_mv is not None
            else None
        )

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

        # real_type 状态化：Unknown 在放电/未充电时是正常的，不当作采集失败
        for n in node_list:
            if n["id"] != "real_type":
                continue
            v = n.get("value", "")
            if v.lower() in ("unknown", ""):
                if batt_status_norm in ("discharging", "not charging"):
                    n["value"] = "未连接（放电中）"
                    n["ok"] = True
                elif batt_status_norm == "charging":
                    n["value"] = "未识别（充电中）"
                    n["ok"] = True
            break

        return {
            "ts": ts,
            "iso": now_iso(),
            "nodes": node_list,
            "battery": battery,
            "derived": derived,
            "meta": {
                "interval": self.fast_interval,
                "fast_interval": self.fast_interval,
                "logs_interval": self.logs_interval,
                "logs_updated_at": int(self.logs_updated_at),
                "logs_stale": self.logs_stale,
                "adb": getattr(self.adb, "serial", None) or "",
                "source": "adb",
                "device": getattr(self.adb, "serial", None) or "",
                "schema_version": 2,
            },
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
                        help="fast sample/refresh interval in seconds (default 3)")
    parser.add_argument("--logs-interval", type=float, default=20.0,
                        help="vote/session log interval in seconds (default 20)")
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
    sampler = Sampler(adb, args.interval, args.logs_interval)
    sampler.start()

    server = DashboardServer(("127.0.0.1", args.port), sampler, index_html)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"[ok] dashboard running: {url}  "
          f"(fast={args.interval}s, logs={args.logs_interval}s, "
          f"adb={adb.adb_bin or 'not found'})")
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
