#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redmi K80 Pro (miro) MCA charging real-time dashboard backend (ADB 版).

数据语义与安卓版 SnapshotCollector.java 对齐：
- 快速采集自适应：充电 3 秒、未充电 12 秒、无页面访问 45 秒
- 日志采集（voters / sessions / EPP / 实际下发 ICL）默认 10 秒
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
USB_UEVENT = "/sys/class/power_supply/usb/uevent"
MCA_LOG_DIR = "/data/vendor/bsplog/charge/charge_logger/mca_log"
THERMAL_DUMP = "/data/vendor/thermal/thermal.dump"


def _detect_version() -> str:
    """Git short hash（启动时的版本标识），失败时返回 dev。"""
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        out = subprocess.run(
            ["git", "-C", base, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "dev"


VERSION = _detect_version()

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
        paths = [BASE_SYSFS + n["path"] for n in NODES] + [BATTERY_UEVENT, USB_UEVENT]
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
            self.last_log_fname = fname
            path = f"{MCA_LOG_DIR}/{fname}"
            code, out, _ = self._run(
                ["shell", "su", "-c", f'"tail -c {tail_bytes} {path} | grep -a -F \'mca_vote\'"'],
                timeout=15)
            # grep 无匹配时退出码为 1，属于“读取成功但无内容”，不算失败
            return (code in (0, 1), out if code in (0, 1) else "")
        except FileNotFoundError:
            return False, ""

    def read_session_logs(self, tail_bytes: int = 4194304, file_count: int = 2) -> tuple[bool, str]:
        """Grep session events from at most two newest mca_log files (chronological).

        Returns (read_ok, text)：与 read_vote_logs 相同，grep 无匹配不算失败。
        两个文件仅用于覆盖日志轮转边界；parse_sessions 最终只保留最新会话。
        """
        if not self.available:
            return False, ""
        grep_args = (
            "-e 'power_good' -e 'AUTHEN_FINISH' -e 'uuid_value' "
            "-e 'TX_ADAPTER' -e 'FAST_CHARGE' -e 'fast chg success' "
            "-e 'set chg current' -e 'open path ibus' "
            "-e 'smartchg_soc_limit_callback' "
            "-e 'strategy_wireless_get_qc_enable' "
            "-e 'strategy_wireless_get_charging_info' "
            "-e 'BPP drawload' -e 'rx_iout_limit' -e 'epp plus' "
            "-e 'EPP+' -e 'send_vout_range_request' -e 'set adapter voltage'"
        )
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
            self.last_log_fname = files[-1]
            script = "; ".join(
                f"tail -c {tail_bytes} {MCA_LOG_DIR}/{f} | grep -a -F {grep_args} "
                "| grep -v -F 'sysfs_show'"
                for f in files)
            code, out, _ = self._run(["shell", "su", "-c", f'"{script}"'], timeout=25)
            return (code in (0, 1), out if code in (0, 1) else "")
        except FileNotFoundError:
            return False, ""

    def read_power_path_logs(self, tail_bytes: int = 1048576, tail_lines: int = 200) -> tuple[bool, str]:
        """Grep power-path signals from the newest log only, bounded on device.

        只读最新 1 个文件 1MB，并在手机端 grep + tail -n 200 后再返回，
        避免高频 quickchg/buckchg 行撑爆 Wi-Fi ADB。
        """
        if not self.available:
            return False, ""
        # 全部是字面量；通用 mca_quick_charge_select_max_ibat 已覆盖原先两个 .* 分支。
        grep_args = (
            "-e 'power_good' -e 'usb online' -e 'real_type changed' "
            "-e 'sc8581_set_operation_mode' "
            "-e 'mca_quick_charge_update_work_mode_para' "
            "-e 'strategy_quickchg_map_ibus_to_fsw' -e 'cur_work_cp' "
            "-e 'strategy_buckchg_charge_limit' "
            "-e 'strategy_buckchg_update_charge_status' "
            "-e 'mca_quick_charge_regulation' "
            "-e 'mca_wireless_quick_charge_select_cur_work_mode' "
            "-e 'mca_wireless_quick_charge_select_max_ibat' "
            "-e 'target_limit_fcc_ma' "
            "-e 'mca_quick_charge_select_max_ibat'"
        )
        try:
            code, out, _ = self._run(
                ["shell", "su", "-c", f'"ls -t {MCA_LOG_DIR}/ | head -n 1"'], timeout=10)
            if code != 0 or not out.strip():
                return False, ""
            fname = out.strip().splitlines()[0].strip()
            if not re.fullmatch(r"[A-Za-z0-9_.\-]+", fname):
                return False, ""
            self.last_log_fname = fname
            path = f"{MCA_LOG_DIR}/{fname}"
            script = (f"tail -c {tail_bytes} {path} | grep -a -F {grep_args} "
                      f"| tail -n {tail_lines}")
            code, out, _ = self._run(["shell", "su", "-c", f'"{script}"'], timeout=15)
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
                 f'"tail -c {tail_bytes} {THERMAL_DUMP} | grep -a -F \'MONITOR-WIRELESS\' | tail -n 3"'],
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
                result["usb_uevent"] = {"raw": "\n".join(lines), "ok": bool(lines)}
            elif i == count - 2:
                result["battery_uevent"] = {"raw": "\n".join(lines), "ok": bool(lines)}
            elif i < count - 2:
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


def num(raw: object) -> float | None:
    try:
        if isinstance(raw, (int, float)):
            return float(raw)
        return float(str(raw).strip())
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
    "buck_input": "mA",
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
    # 项目假设（用户确认，未从 .ko 独立核实）：有线 buck 充电电流按 MIN 推算，
    # 无 effective 行时只允许“参考推算”，不进入总仲裁 fallback
    "buck_charge_curr": "MIN_ASSUMED",
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

# 日志窗口滚动时按 topic 增量保留；真正的输入边界到来时再清理对应域。
WIRELESS_VOTER_TOPICS = {
    "wireless_buck_input", "wireless_bpp_in", "wireless_bppqc2_in",
    "wireless_bppqc3_in", "wireless_epp_in", "wireless_auth_20w",
    "wireless_auth_30w", "wireless_auth_50w", "wireless_auth_80w",
    "wireless_auth_voice_box", "wireless_auth_magnet_30w",
    "wireless_sw_qc_ich", "wireless_sw_thermal_ich",
    "wls_single_chg_cur", "wls_multi_chg_cur", "wls_quick_chg_disable",
}
WIRED_VOTER_TOPICS = {
    "buck_input", "div1_single", "div1_multi", "div2_single", "div2_multi",
    "div4_single", "div4_multi", "single_chg_cur", "multi_chg_cur",
    "quick_chg_disable", "quick_chg_en",
}


def resolve_input_source(usb_online: bool | None, usb_vbus_mv: float | None,
                         wireless_signal: bool) -> str:
    """Resolve physical input before assigning shared CP telemetry."""
    if usb_online is True:
        return "wired"
    if usb_online is False:
        return "wireless" if wireless_signal else "none"
    if wireless_signal:
        return "wireless"
    return "wired" if usb_vbus_mv is not None and usb_vbus_mv > 1000.0 else "none"


def cp_ibus_owner(input_source: str, cp_ibus_ma: float | None) -> str:
    if cp_ibus_ma is None or input_source not in ("wired", "wireless"):
        return "none"
    return input_source


def resolve_wireless_connected(latched: bool | None, input_source: str,
                               vout_mv: float | None) -> bool:
    if latched is not None:
        return bool(latched)
    return input_source == "wireless" or (vout_mv is not None and vout_mv > 1000.0)


def resolve_wired_input_source(wired_state: str, cp_ibus_ma: float | None,
                               usb_ibus_ma: float | None, usb_online: bool,
                               charging_enabled: bool | None,
                               battery_current_ma: float | None) -> str | None:
    cp_valid = cp_ibus_ma is not None
    stopped = charging_enabled is False and battery_current_ma is not None \
        and abs(battery_current_ma) <= 300.0
    cp_idle = stopped and cp_valid and abs(cp_ibus_ma) <= 50.0
    if wired_state == "cp" and cp_valid and not cp_idle:
        return "cp_ibus_total"
    if usb_online and usb_ibus_ma is not None:
        return "usb_uevent"
    return "cp_ibus_total" if wired_state == "cp" and cp_valid else None


def shift_log_time(time_str: str, offset_minutes: int) -> str:
    """Shift a HH:MM:SS:mmm kernel log timestamp by the device UTC offset."""
    m = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}):(\d{3})", time_str)
    if not m or not offset_minutes:
        return time_str
    total = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
             + offset_minutes * 60) % 86400
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}:{m.group(4)}"


LOG_FILE_RE = re.compile(r"mca_log_(\d{2})(\d{2})_(\d{2})(\d{2})\.log")


def abs_log_ms(fname: str, time_str: str) -> int:
    """日志行时间（已 shift 为本地）→ 归一化绝对毫秒：文件名日期 + 行内时刻，跨文件单调。

    用于比较 ICL / effective 与最近一次 work_mode 变化的先后，
    避免跨 00:00 或跨多个日志文件时字符串时间比较翻转。
    """
    base = datetime.now().date()
    m = LOG_FILE_RE.search(fname or "")
    if m:
        base = datetime(datetime.now().year, int(m.group(1)), int(m.group(2))).date()
    base_ms = int(datetime.combine(base, datetime.min.time()).timestamp() * 1000)
    p = time_str.split(":")
    if len(p) < 4:
        return base_ms
    try:
        secs = int(p[0]) * 3600 + int(p[1]) * 60 + int(p[2])
        return base_ms + secs * 1000 + int(p[3])
    except ValueError:
        return base_ms


def parse_vote_blocks(text: str, offset_minutes: int = 0, fname: str = "") -> dict:
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
            t = shift_log_time(tm.group(1), offset_minutes) if tm else ""
            results_by_topic[m.group(1)] = {
                "topic": m.group(1), "value": int(m.group(2)),
                "client": m.group(3), "idx": int(m.group(4)),
                "time": t,
                "r_ms": abs_log_ms(fname, t) if t else 0,
            }
            continue
        m = VOTE_HEADER_RE.search(line)
        if m:
            topic = m.group(1)
            tm = VOTE_TIME_RE.search(line)
            current = {
                "topic": topic,
                "time": shift_log_time(tm.group(1), offset_minutes) if tm else "",
                "at": abs_log_ms(fname, shift_log_time(tm.group(1), offset_minutes)) if tm else int(time.time() * 1000),
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


def merge_vote_topics(previous: dict | None, incoming: dict | None) -> dict:
    """Merge the topics present in this log window without deleting omitted topics."""
    merged = copy.deepcopy(previous or {})
    for topic, nxt in (incoming or {}).items():
        old = merged.get(topic)
        if not isinstance(old, dict):
            merged[topic] = copy.deepcopy(nxt)
            continue
        block = copy.deepcopy(old)
        for field in ("topic", "time", "unit", "policy", "rows", "at"):
            if field in nxt:
                block[field] = copy.deepcopy(nxt[field])
        for field in ("changed", "result"):
            if field in nxt and nxt[field] is not None:
                block[field] = copy.deepcopy(nxt[field])
        merged[topic] = block
    return merged


def clear_vote_topics(previous: dict | None, topics: set[str]) -> dict:
    """Clear only the topics owned by a disconnected input domain."""
    return {k: copy.deepcopy(v) for k, v in (previous or {}).items() if k not in topics}


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


def parse_wls_icl(text: str, offset_minutes: int = 0, fname: str = "") -> tuple[int, int, str, int] | None:
    """Extract the latest wireless loop icl + chg_en (driver-applied state).

    Returns (icl, chg_en, shifted local log time, normalized log ms) so the
    frontend can show the value together with its timestamp and decide whether
    it belongs to the current work_mode stage.
    """
    last = None
    for line in text.splitlines():
        m = re.search(r"wireless loop: icl:(\d+), buck_fcc:\d+, chg_en:(\d+)", line)
        if m:
            tm = VOTE_TIME_RE.search(line)
            log_time = shift_log_time(tm.group(1), offset_minutes) if tm else ""
            ms = abs_log_ms(fname, log_time) if log_time else 0
            last = (int(m.group(1)), int(m.group(2)), log_time, ms)
    return last


def parse_quick_cur_max(text: str) -> int | None:
    """Latest quick wireless battery current target cur_max:[Final] (mA)."""
    last = None
    for m in re.finditer(r"cur_max:\[Final\]: (\d+)", text):
        last = int(m.group(1))
    return last


def parse_buck_fcc(text: str) -> int | None:
    """Latest wireless loop buck_fcc (battery-side FCC cap, mA)."""
    last = None
    for m in re.finditer(r"wireless loop: icl:\d+, buck_fcc:(\d+)", text):
        last = int(m.group(1))
    return last


def parse_cp_mode(text: str) -> int | None:
    """Latest sc8581 charge-pump operation mode (0=off, >0=CP 路径生效)."""
    last = None
    for m in re.finditer(r"set operation mode (\d+)", text):
        last = int(m.group(1))
    return last


def parse_cp_work_mode(text: str) -> int | None:
    """Latest quick wireless charge-pump division ratio work_mode (1/2/4 → 1:1/2:1/4:1)."""
    last = None
    for m in re.finditer(r"select_cur_work_mode:.*work_mode=(\d+)", text):
        last = int(m.group(1))
    return last


def parse_quick_cur_decision(text: str, offset_minutes: int = 0) -> dict | None:
    """Latest select_max_ibat decision: inputs (445) + cur_max:[Final] (446)."""
    last_inputs: dict | None = None
    result: dict | None = None
    for line in text.splitlines():
        m = re.search(
            r"select_max_ibat:445 \[channel_cur:(\d+)\], \[temp_max_cur:(\d+)\], "
            r"\[tx_adapter_max:(\d+)\], \[sw_qc_ichg:(\d+)\],\[sw_thermal_ichg:(\d+)\]",
            line)
        if m:
            tm = VOTE_TIME_RE.search(line)
            last_inputs = {
                "channel_cur": int(m.group(1)),
                "temp_max_cur": int(m.group(2)),
                "tx_adapter_max": int(m.group(3)),
                "sw_qc_ichg": int(m.group(4)),
                "sw_thermal_ichg": int(m.group(5)),
                "log_time": shift_log_time(tm.group(1), offset_minutes) if tm else "",
                "at": int(time.time() * 1000),
            }
            continue
        m2 = re.search(r"select_max_ibat:446 cur_max:\[Final\]: (\d+)", line)
        if m2 and last_inputs is not None:
            result = dict(last_inputs)
            result["final"] = int(m2.group(1))
    return result


WIRED_STAGE_CUR_MAX_RE = re.compile(
    r"mca_quick_charge_select_max_ibat:.*cur_stage (\d+) cur_max (\d+) "
    r"delta_cur (\d+) cur_work_cp")
WIRED_FINAL_CUR_MAX_RE = re.compile(
    r"mca_quick_charge_select_max_ibat:.*cur_max (\d+) secure_cur (\d+) "
    r"channel_cur (\d+) thermal_cur (\d+)")
WIRED_QC_TARGET_RE = re.compile(
    r"target_limit_fcc_ma:\s*(\d+)\s*,\s*target_limit_ibus_ma:\s*(\d+)")


def parse_session_cp_state(text: str, offset_minutes: int = 0, fname: str = ""):
    """无线/有线 CP 状态彻底解耦：
    - power_good 只重置无线 track；usb online / real_type changed 只重置有线 track
    - SC8581 operation mode 只有在对应 quickchg 上下文出现后才写入对应 track
    """
    # 无线 track（power_good 边界）
    w_cp_mode: int | None = None
    w_cp_work_mode: int | None = None
    w_cp_work_mode_ms: int | None = None
    w_decision: dict | None = None
    w_inputs: dict | None = None
    w_boundary = False
    w_ctx = False
    # 有线 track（usb online / real_type changed 边界）
    d_cp_mode: int | None = None
    d_cp_mode_seq = -1
    d_cp_ratio: int | None = None
    d_cur_cp = False
    d_cur_cp_seq = -1
    d_buck = False
    d_boundary = False
    d_ctx = False
    d_cur_max: dict | None = None
    d_stage_cur_max: dict | None = None
    d_qc_target: dict | None = None
    seq = 0
    for line in text.splitlines():
        seq += 1
        if "power_good_on" in line or "power_good_off" in line:
            w_boundary = True
            w_cp_mode = None
            w_cp_work_mode = None
            w_cp_work_mode_ms = None
            w_decision = None
            w_inputs = None
            w_ctx = False
            continue
        if ("usb online: 0" in line or "usb online: 1" in line
                or "real_type changed:" in line):
            d_boundary = True
            d_cp_mode = None
            d_cp_mode_seq = -1
            d_cp_ratio = None
            d_cur_cp = False
            d_cur_cp_seq = -1
            d_buck = False
            d_ctx = False
            d_cur_max = None
            d_stage_cur_max = None
            d_qc_target = None
            continue
        # 上下文：无线 quickchg 与有线 quickchg 互斥
        if "mca_wireless_quick_charge_" in line:
            w_ctx = True
            d_ctx = False
        if ("mca_quick_charge_" in line or "[mca_quick_charge]" in line
                or "strategy_quickchg_" in line):
            d_ctx = True
            w_ctx = False
        # 有线 Buck 证据：buckchg 策略活动（无 CP 证据时据此判 Buck）
        if "mca_strategy_buckchg" in line or "strategy_buckchg" in line:
            d_buck = True
        m = re.search(r"set operation mode (\d+)", line)
        if m:
            n = int(m.group(1))
            if w_ctx:
                w_cp_mode = n
            if d_ctx:
                d_cp_mode = n
                d_cp_mode_seq = seq
            continue
        m = re.search(r"mca_wireless_quick_charge_select_cur_work_mode:.*work_mode=(\d+)", line)
        if m:
            w_cp_work_mode = int(m.group(1))
            tm = VOTE_TIME_RE.search(line)
            raw = tm.group(1) if tm else ""
            w_cp_work_mode_ms = (
                abs_log_ms(fname, shift_log_time(raw, offset_minutes)) if raw else None)
            continue
        m = re.search(r"update_work_mode_para:.*work_mode: (\d+)", line)
        if m:
            d_cp_ratio = int(m.group(1))
            continue
        m = re.search(r"map_ibus_to_fsw:.*ratio: (\d+)", line)
        if m:
            d_cp_ratio = int(m.group(1))
            continue
        mf = WIRED_FINAL_CUR_MAX_RE.search(line)
        if mf and d_ctx:
            tm = VOTE_TIME_RE.search(line)
            d_cur_max = {
                "cur_max": int(mf.group(1)),
                "secure_cur": int(mf.group(2)),
                "channel_cur": int(mf.group(3)),
                "thermal_cur": int(mf.group(4)),
                "log_time": shift_log_time(tm.group(1), offset_minutes) if tm else "",
                "at": int(time.time() * 1000),
            }
            continue
        ms = WIRED_STAGE_CUR_MAX_RE.search(line)
        if ms and d_ctx:
            tm = VOTE_TIME_RE.search(line)
            d_stage_cur_max = {
                "stage": int(ms.group(1)),
                "cur_max": int(ms.group(2)),
                "delta": int(ms.group(3)),
                "log_time": shift_log_time(tm.group(1), offset_minutes) if tm else "",
                "at": int(time.time() * 1000),
            }
            d_cur_cp = True
            d_cur_cp_seq = seq
            continue
        mq = WIRED_QC_TARGET_RE.search(line)
        if mq and d_ctx:
            tm = VOTE_TIME_RE.search(line)
            d_qc_target = {
                "fcc": int(mq.group(1)),
                "ibus": int(mq.group(2)),
                "source": "mca_qc_get_vbus_change_trend",
                "log_time": shift_log_time(tm.group(1), offset_minutes) if tm else "",
                "at": int(time.time() * 1000),
            }
            d_cur_cp = True
            d_cur_cp_seq = seq
            continue
        if "mca_quick_charge_select_max_ibat:" in line and "cur_work_cp" in line:
            d_cur_cp = True
            d_cur_cp_seq = seq
            continue
        if "mca_wireless_quick_charge_select_max_ibat:" not in line:
            continue
        m = re.search(
            r"select_max_ibat:445 \[channel_cur:(\d+)\], \[temp_max_cur:(\d+)\], "
            r"\[tx_adapter_max:(\d+)\], \[sw_qc_ichg:(\d+)\],\[sw_thermal_ichg:(\d+)\]",
            line)
        if m:
            tm = VOTE_TIME_RE.search(line)
            w_inputs = {
                "channel_cur": int(m.group(1)),
                "temp_max_cur": int(m.group(2)),
                "tx_adapter_max": int(m.group(3)),
                "sw_qc_ichg": int(m.group(4)),
                "sw_thermal_ichg": int(m.group(5)),
                "log_time": shift_log_time(tm.group(1), offset_minutes) if tm else "",
                "at": int(time.time() * 1000),
            }
            continue
        m2 = re.search(r"select_max_ibat:446 cur_max:\[Final\]: (\d+)", line)
        if m2 and w_inputs is not None:
            w_decision = dict(w_inputs)
            w_decision["final"] = int(m2.group(1))
    # 有线最终状态（时间顺序 + CP 证据优先）
    if d_cp_mode is not None:
        if d_cp_mode > 0:
            d_state = "cp"
        elif d_cur_cp and d_cur_cp_seq > d_cp_mode_seq:
            d_state = "cp"   # mode=0 之后又出现 cur_work_cp → CP 重新激活
        else:
            d_state = "buck"
    elif d_cur_cp:
        d_state = "cp"
    elif d_buck:
        d_state = "buck"
    else:
        d_state = "unknown"
    return {
        "wireless": (w_cp_mode, w_cp_work_mode, w_cp_work_mode_ms, w_decision, w_boundary),
        "wired": (d_state, d_cp_ratio, d_cur_cp, d_buck, d_boundary,
                  d_cur_max, d_stage_cur_max, d_qc_target),
    }


WIRED_BUCK_TELEMETRY_RE = re.compile(
    r"strategy_buckchg_update_charge_status:1463 pmic_chg_status: (\d+), "
    r"chg_status: (\d+), chg_en: \[(\d+)\]\[(\w+)\], chg_type: (\d+), "
    r"vbat: (\d+), vbus: (\d+), ibus: (\d+)")

WIRED_CP_TELEMETRY_RE = re.compile(
    r"mca_quick_charge_regulation:1942 cur_stage\[(\d+)\]: "
    r"adp_volt: (\d+)/(\d+), "
    r"ibat: ([\d\-]+)/([\d\-]+)/([\d\-]+)/([\d\-]+), "
    r"vbat: (\d+)/(\d+), ibus: (\d+),")

# 日志采集周期约 10s：阈值取“略宽于采集周期”，避免每次都误标陈旧。
# regulation 正常约 0.7s 一条，buckchg status 约 10s 一条。
WIRED_TELEMETRY_STALE_MS = {
    "quick_charge_regulation": 12000,
    "buckchg_telemetry": 25000,
}


def _latest_line_match(text: str, pattern: re.Pattern):
    """返回文本中最后一次匹配 (match, time_match)。"""
    m = None
    tm = None
    for line in text.splitlines():
        mm = pattern.search(line)
        if mm:
            m = mm
            tm = VOTE_TIME_RE.search(line)
    return m, tm


def parse_wired_buck_telemetry(text: str, offset_minutes: int = 0) -> dict | None:
    """最新一条 buckchg 状态行：vbus/ibus 为 µV/µA，返回 mV/mA。"""
    m, tm = _latest_line_match(text, WIRED_BUCK_TELEMETRY_RE)
    if m is None:
        return None
    return {
        "vbus_mv": int(m.group(7)) / 1000.0,
        "ibus_ma": int(m.group(8)) / 1000.0,
        "chg_en": int(m.group(3)),
        "chg_en_client": m.group(4),
        "chg_type": int(m.group(5)),
        "source": "buckchg_telemetry",
        "log_time": shift_log_time(tm.group(1), offset_minutes) if tm else "",
        "at": int(time.time() * 1000),
    }


def parse_wired_cp_telemetry(text: str, offset_minutes: int = 0) -> dict | None:
    """最新一条有线 quick charge regulation 行。

    adp_volt 第一值为请求值、第二值为实测值（mV）；ibus 单位为 mA。
    """
    m, tm = _latest_line_match(text, WIRED_CP_TELEMETRY_RE)
    if m is None:
        return None
    return {
        "vbus_mv": int(m.group(3)),
        "ibus_ma": int(m.group(10)),
        "chg_en": 1,
        "chg_en_client": "quick_charge",
        "chg_type": None,
        "source": "quick_charge_regulation",
        "log_time": shift_log_time(tm.group(1), offset_minutes) if tm else "",
        "at": int(time.time() * 1000),
    }


def is_wired_disconnected(text: str) -> bool:
    """最新一条有线会话边界是否为断开（usb online: 0 / real_type => 0）。"""
    last = ""
    for line in text.splitlines():
        if "usb online:" in line or "real_type changed:" in line:
            last = line
    if not last:
        return False
    if "usb online: 0" in last:
        return True
    return bool(re.search(r"real_type changed: \d+ => 0", last))


def split_after_last_wired_boundary(text: str) -> str:
    """只保留最后一次 usb online / real_type changed 边界之后的日志段。"""
    lines = text.splitlines()
    last = -1
    for i, line in enumerate(lines):
        if "usb online:" in line or "real_type changed:" in line:
            last = i
    return "\n".join(lines[last + 1:]) if last >= 0 else text


def has_wired_boundary(text: str) -> bool:
    return any("usb online:" in line or "real_type changed:" in line
               for line in text.splitlines())


def split_after_last_wireless_attach(text: str) -> str:
    """只保留最后一次 wireless power_good_on 之后的日志段。"""
    lines = text.splitlines()
    last = -1
    for i, line in enumerate(lines):
        if "power_good_on" in line:
            last = i
    return "\n".join(lines[last + 1:]) if last >= 0 else text


def last_wireless_attach_ms(text: str, offset_minutes: int = 0,
                            fname: str = "") -> int | None:
    """最后一条 wireless power_good_on 的归一化日志毫秒（会话边界 key）。"""
    last = None
    for line in text.splitlines():
        if "power_good_on" not in line:
            continue
        tm = VOTE_TIME_RE.search(line)
        if tm:
            last = abs_log_ms(fname, shift_log_time(tm.group(1), offset_minutes))
    return last


def parse_wireless_mode(text: str, offset_minutes: int = 0) -> dict:
    """解析当前无线会话的控制模式与 RX 输出电流上限。

    同一 power_good_on 会话内按日志顺序扫描，最后证据覆盖前证据：
    - BPP drawload → bpp_drawload（仅模式标识，不做 ICL/iout 一致性判定）
    - epp plus / EPP+ / rx_iout_limit / can quick charge! / vout range request
      → epp_qc
    """
    mode = "unknown"
    rx_iout_limit: int | None = None
    rx_iout_limit_time = ""
    qc_enabled = False
    for line in text.splitlines():
        if "BPP drawload" in line:
            mode = "bpp_drawload"
        if ("epp plus" in line or "EPP+" in line
                or "send_vout_range_request" in line
                or "set adapter voltage" in line
                or "rx_iout_limit" in line
                or "can quick charge!" in line):
            mode = "epp_qc"
        # 函数名 strategy_class_wireless_op_get_rx_iout_limit:421 里的行号
        # 也会匹配，必须取该行最后一次匹配（真正的 rx_iout_limit: 3800）
        for mm in re.finditer(r"rx_iout_limit:\s*(\d+)", line):
            rx_iout_limit = int(mm.group(1))
            tm = VOTE_TIME_RE.search(line)
            if tm:
                rx_iout_limit_time = shift_log_time(tm.group(1), offset_minutes)
        if "can quick charge!" in line:
            qc_enabled = True
    return {"mode": mode, "rx_iout_limit": rx_iout_limit,
            "rx_iout_limit_time": rx_iout_limit_time,
            "qc_enabled": qc_enabled}


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
    if "usb online: 0" in line:
        return ("wired_off", "有线充电移除", "")
    if "usb online: 1" in line:
        return ("wired_on", "有线充电接入", "")
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
    - 只保留最新 1 个会话，每个会话最多 100 条事件
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
        if kind in ("on", "wired_on"):
            # 新的 power_good_on 到来时，把上一个未结束会话标为结束
            if cur is not None and not cur["ended"]:
                cur["ended"] = True
            cur = {
                "start": t, "ended": False, "events": [],
                "source": "wired" if kind == "wired_on" else "wireless",
                "uuid": None, "tx_adapter": None, "fc_flag": None,
                "opens": 0, "smartendura": False,
                "peak_limit_ma": None, "final_limit_ma": None,
            }
            sessions.append(cur)
            if kind == "on":
                cur["events"].append({"kind": kind, "time": t, "label": label, "detail": detail})
            continue
        if cur is None:
            continue
        evs = cur["events"]
        if kind in ("off", "wired_off"):
            cur["ended"] = True
            if kind == "off":
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
            v = int(detail)
            if len(sessions) == 1 and evs and evs[-1].get("kind") == "ichg":
                prev = evs[-1]
                if "→" in str(prev.get("detail", "")):
                    mprev = re.search(r"^(\d+)", str(prev.get("detail", "")))
                    first = int(mprev.group(1)) if mprev else v
                    count_match = re.search(r"· (\d+)次", str(prev.get("detail", "")))
                    count = int(count_match.group(1)) + 1 if count_match else 2
                    prev["time"] = t
                    prev["detail"] = f"{first}→{v}mA · {count}次"
                else:
                    try:
                        first = int(str(prev.get("detail", "")).split()[0])
                    except (TypeError, ValueError):
                        first = v
                    evs.append({"kind": kind, "time": t, "label": label,
                                "detail": f"{first}→{v}mA · 2次"})
            else:
                evs.append({"kind": kind, "time": t, "label": label, "detail": detail})
            if cur["peak_limit_ma"] is None or v > cur["peak_limit_ma"]:
                cur["peak_limit_ma"] = v
            cur["final_limit_ma"] = v
        elif kind == "open":
            cur["opens"] += 1
            evs.append({"kind": kind, "time": t, "label": label, "detail": detail})
        elif kind == "smart":
            cur["smartendura"] = True
            evs.append({"kind": kind, "time": t, "label": label, "detail": detail})

    while len(sessions) > 1:
        sessions.pop(0)
    for s in sessions:
        while len(s["events"]) > 100:
            s["events"].pop(0)
    return sessions


class Sampler:
    """双周期采集：快采集 3s；日志连接时按配置周期、完全断开时 60s。

    日志（voters/sessions/EPP/实际 ICL）读取失败时保留上次成功数据，
    只把 logs_stale 置 True，页面据此提示“显示上次成功数据”。
    """

    def __init__(self, adb: AdbReader, fast_interval: float, logs_interval: float,
                 idle_interval: float = 12.0, no_viewer_interval: float = 45.0,
                 no_viewer_after: float = 30.0):
        self.adb = adb
        self.fast_interval = max(1.0, float(fast_interval))
        self.idle_interval = max(self.fast_interval, float(idle_interval))
        self.no_viewer_interval = max(self.idle_interval, float(no_viewer_interval))
        self.no_viewer_after = max(10.0, float(no_viewer_after))
        self.last_viewer_at = 0.0
        self.logs_interval = max(5.0, float(logs_interval))
        self.logs_active = True
        self.vote_logs_stale = False
        self.session_logs_stale = False
        self.power_path_logs_stale = False
        self.logs_updated_at = time.time() * 1000
        # 使用独立 stop_event，避免覆盖 threading.Thread 内部的 _stop()
        self.stop_event = threading.Event()
        self.fast_wakeup = threading.Event()
        self.logs_wakeup = threading.Event()
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
        self.last_wls_icl_ms: int | None = None
        self.last_wls_icl_key: tuple | None = None
        self.last_wls_chg_en: int | None = None
        # quick wireless 最终电池电流目标（CP 快充路径真正约束电流的值），cur_max 缺失时回退 buck_fcc
        self.last_quick_cur_max: int | None = None
        self.last_buck_fcc: int | None = None
        # sc8581 电荷泵工作模式：>0 表示 CP 路径生效（此时 buck 输入限流不约束实际电流）
        self.last_cp_mode: int | None = None
        self.last_wls_cp_evidence: bool = False
        # quick wireless 电荷泵分压比 work_mode（1/2/4）
        self.last_cp_work_mode: int | None = None
        # 最近一次无线 work_mode 变化行的归一化日志毫秒（跨文件单调）
        self.last_wls_work_mode_ms: int | None = None
        # 当前读取的 mca 日志文件名，用于日志时间归一化
        self.last_log_fname: str = ""
        # select_max_ibat 完整决策（输入 + cur_max Final + 日志时间）
        self.last_cur_decision: dict | None = None
        # 有线功率路径状态：cp / buck / unknown（时间顺序 + CP 证据优先）
        self.last_wired_state: str = "unknown"
        self.last_wired_cp_ratio: int | None = None
        self.last_wired_cur_cp: bool = False
        self.last_wired_buck: bool = False
        # 有线 CP quick_charge cur_max：1611 最终行 / 1597 阶段行
        self.last_wired_cur_max: dict | None = None
        self.last_wired_stage_cur_max: dict | None = None
        # HVDCP/QC3 target_limit_fcc_ma（只作为 QC 调节目标，不冒充 Quick Charge Final）
        self.last_wired_qc_target: dict | None = None
        # 有线输入遥测缓存：CP 与 Buck 各留一份，按 wired_cp.state 选择来源
        self.last_wired_cp_tel: dict | None = None
        self.last_wired_buck_tel: dict | None = None
        # 新会话/协议变化后尚无策略遥测：页面回退 USB uevent 并标记“等待策略遥测”
        self.last_wired_tel_waiting = False
        # 无线控制模式与 RX 输出电流上限（bpp drawload / epp_plus/QC）
        self.last_wls_mode = "unknown"
        self.last_rx_iout_limit: int | None = None
        # rx_iout_limit 会话状态机：随 power_good 边界，不随 work_mode；
        # captured=false 表示本会话尚未捕获；日志窗口滚动不失效；断开清空
        self.rx_iout_limit_captured: bool = False
        self.last_rx_iout_limit_at: int | None = None
        self.last_rx_iout_limit_log_time: str | None = None
        # SmartEndura / smartchg soc_limit 上下文（用于“当前上游限制”标记）
        self.last_smartendura_soc_limit: bool = False
        # 最后一条 power_good_on 的归一化毫秒，用于识别“新无线会话”
        self.last_wls_session_ms: int | None = None
        self.last_wireless_connected: bool | None = None
        # 日志行 stable key：同一行重复扫描不刷新 at（log_time + 关键值）
        self.last_cur_decision_key: tuple | None = None
        self.last_wired_cur_max_key: tuple | None = None
        self.last_wired_stage_cur_max_key: tuple | None = None
        self.last_wired_qc_target_key: tuple | None = None

    def start(self) -> None:
        threading.Thread(target=self.run_fast, name="sampler-fast", daemon=True).start()
        threading.Thread(target=self.run_logs, name="sampler-logs", daemon=True).start()

    def run_fast(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.collect_fast()
            except Exception as exc:  # keep the page alive on any sampling error
                self._publish(self._build_error_snapshot(str(exc)))
            self.fast_wakeup.wait(self.current_fast_interval())
            self.fast_wakeup.clear()

    def run_logs(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.collect_logs()
            except Exception as exc:  # 日志失败不打断整体循环
                print(f"[warn] collect_logs failed: {exc}")
            self.logs_wakeup.wait(self.current_logs_interval())
            self.logs_wakeup.clear()

    def stop(self) -> None:
        self.stop_event.set()
        self.fast_wakeup.set()
        self.logs_wakeup.set()

    def viewer_active(self) -> bool:
        return time.monotonic() - self.last_viewer_at <= self.no_viewer_after

    def current_fast_interval(self) -> float:
        if not self.viewer_active():
            return self.no_viewer_interval
        return self.fast_interval if self.logs_active else self.idle_interval

    def current_fast_mode(self) -> str:
        if not self.viewer_active():
            return "no_viewer"
        return "active" if self.logs_active else "idle"

    def mark_viewer_access(self) -> None:
        was_active = self.viewer_active()
        self.last_viewer_at = time.monotonic()
        if not was_active:
            self.fast_wakeup.set()

    def current_logs_interval(self) -> float:
        return self.logs_interval if self.logs_active else max(60.0, self.logs_interval)

    def _update_logs_active(self, data: dict) -> None:
        status = str(data.get("battery", {}).get("status", {}).get("value", ""))
        derived = data.get("derived", {})
        source = derived.get("input_source")
        vrect = derived.get("vrect")
        input_connected = (
            source in ("wired", "wireless")
            or bool(derived.get("wired_online"))
            or isinstance(vrect, (int, float)) and vrect > 0
        )
        was_active = self.logs_active
        self.logs_active = status.lower() == "charging" or input_connected
        if self.logs_active != was_active:
            self.fast_wakeup.set()
        if self.logs_active and not was_active:
            self.logs_wakeup.set()

    def get(self) -> dict:
        with self.lock:
            meta = self.snapshot.setdefault("meta", {})
            current = self.current_fast_interval()
            meta["interval"] = current
            meta["fast_interval"] = current
            meta["fast_mode"] = self.current_fast_mode()
            meta["viewer_active"] = self.viewer_active()
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
        self._update_logs_active(parsed)
        parsed["mode"] = "live"
        parsed["connected"] = True
        parsed["thermal"] = parse_thermal_dump(self.adb.read_thermal_dump())

        with self.lock:
            sample = {
                "t": parsed["ts"],
                "input_source": parsed["derived"].get("input_source"),
                "input_detail_source": parsed["derived"].get("input_detail_source"),
                "input_voltage_mv": parsed["derived"].get("input_voltage_mv"),
                "input_current_ma": parsed["derived"].get("input_current_ma"),
                "input_power_w": parsed["derived"].get("input_power_w"),
                "vout": parsed["derived"].get("vout"),
                "vrect": parsed["derived"].get("vrect"),
                "iout": parsed["derived"].get("iout"),
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
        pp_read_ok, pp_log = self.adb.read_power_path_logs()
        # 三条通道独立 stale：功率路径失败不再拖累 session/vote 主链路
        self.vote_logs_stale = not vote_read_ok
        self.session_logs_stale = not session_read_ok
        self.power_path_logs_stale = not pp_read_ok
        # 读取成功但内容为空（如 grep 无匹配）不算失败，仅保留旧数据
        if vote_read_ok and vote_log.strip():
            parsed_voters = parse_vote_blocks(
                vote_log, self.adb.utc_offset_minutes, self.last_log_fname)
            if parsed_voters:
                self.last_voters = merge_vote_topics(self.last_voters, parsed_voters)

        # 会话/EPP/ICL：低频瘦通道
        if session_read_ok and session_log.strip():
            parsed_sessions = parse_sessions(session_log, self.adb.utc_offset_minutes)
            if parsed_sessions:
                self.last_sessions = parsed_sessions
            epp = parse_epp_status(session_log)
            if epp is not None:
                self.last_epp = epp
            # SmartEndura / smartchg soc_limit 上下文：会话日志中出现即置位
            self.last_smartendura_soc_limit = any(
                "smartchg_soc_limit_callback" in line
                or "smart_charge_soc_limit" in line
                or "soc_limit_workfunc" in line
                for line in session_log.splitlines())
            if is_last_wireless_power_off(session_log):
                # 无线已断开：清掉全部无线会话状态，避免上一会话的值继续显示
                self.last_wireless_connected = False
                self.last_voters = clear_vote_topics(self.last_voters, WIRELESS_VOTER_TOPICS)
                self._clear_wireless_session_state()
                self.last_wls_session_ms = None
            else:
                # 所有无线执行层数据统一按最近一次 power_good_on 截断，
                # 避免上一会话的 ICL/buck_fcc 混进新会话
                wtail = split_after_last_wireless_attach(session_log)
                # 无线会话状态机：只有真正的新 power_good_on（session key 变化）
                # 才统一清空全部 wireless-session scoped 状态；
                # 同一 power_good_on 重复出现在日志窗口不重置，避免 Final/ICL 假刷新。
                pg_ms = last_wireless_attach_ms(
                    session_log, self.adb.utc_offset_minutes, self.last_log_fname)
                if pg_ms is not None and pg_ms != self.last_wls_session_ms:
                    self.last_wireless_connected = True
                    self.last_wls_session_ms = pg_ms
                    self.last_voters = clear_vote_topics(self.last_voters, WIRELESS_VOTER_TOPICS)
                    self._clear_wireless_session_state()
                wm = parse_wireless_mode(wtail, self.adb.utc_offset_minutes)
                self.last_wls_mode = wm["mode"]
                if wm["rx_iout_limit"] is not None:
                    self.last_rx_iout_limit = wm["rx_iout_limit"]
                    self.rx_iout_limit_captured = True
                    self.last_rx_iout_limit_at = int(time.time() * 1000)
                    self.last_rx_iout_limit_log_time = wm["rx_iout_limit_time"] or ""
                icl = parse_wls_icl(
                    wtail, self.adb.utc_offset_minutes, self.last_log_fname)
                if icl is not None:
                    value, chg_en, log_time, icl_ms = icl
                    new_key = (value, chg_en, log_time)
                    # 同一日志行重复扫描时不刷新“几秒前”，避免旧值伪装成新值
                    if new_key != self.last_wls_icl_key:
                        self.last_wls_icl_key = new_key
                        self.last_wls_icl = value
                        self.last_wls_chg_en = chg_en
                        self.last_wls_icl_log_time = log_time
                        self.last_wls_icl_ms = icl_ms
                        self.last_wls_icl_at = int(time.time() * 1000)
                buck_fcc = parse_buck_fcc(wtail)
                if buck_fcc is not None:
                    self.last_buck_fcc = buck_fcc
        # 功率路径：高频信号专用通道（手机端已 tail -n 200 封顶）
        if pp_read_ok and pp_log.strip():
            pp_off = is_last_wireless_power_off(pp_log)
            pg_ms_pp = last_wireless_attach_ms(
                pp_log, self.adb.utc_offset_minutes, self.last_log_fname)
            # 只有 pp 窗口出现比已确认会话更新的 power_good_on 才算真正新会话；
            # 同一 power_good_on 重复出现在 tail 窗口不重置（避免 Final 假刷新）。
            pp_new_session = (
                not pp_off and pg_ms_pp is not None
                and (self.last_wls_session_ms is None
                     or pg_ms_pp > self.last_wls_session_ms))
            if pp_off:
                self.last_wireless_connected = False
                self.last_voters = clear_vote_topics(self.last_voters, WIRELESS_VOTER_TOPICS)
                # pp 通道明确断开（session 通道失败时的兜底）：清无线 CP/quick 状态
                self.last_cp_mode = None
                self.last_wls_cp_evidence = False
                self.last_cp_work_mode = None
                self.last_wls_work_mode_ms = None
                self.last_cur_decision = None
                self.last_cur_decision_key = None
                self.last_quick_cur_max = None
                self.last_wls_session_ms = None
            elif pp_new_session:
                self.last_wireless_connected = True
                # 真正的新无线会话边界：统一清空 wireless-session scoped 状态，
                # 本轮 pp 有新证据再重新填（避免旧 Final 串进新会话）
                self.last_wls_session_ms = pg_ms_pp
                self._clear_wireless_session_state()
            # quick wireless cur_max：只接受当前会话窗口内的值
            if pp_off:
                self.last_quick_cur_max = None
            elif pg_ms_pp is not None and pg_ms_pp != self.last_wls_session_ms:
                # pp 窗口边界与会话 key 不一致：不采纳旧会话 cur_max
                pass
            else:
                wt = split_after_last_wireless_attach(pp_log)
                quick_cur_max = parse_quick_cur_max(wt)
                if quick_cur_max is not None:
                    self.last_quick_cur_max = quick_cur_max
            state = parse_session_cp_state(
                pp_log, self.adb.utc_offset_minutes, self.last_log_fname)
            (w_cp_mode, w_cp_work_mode, w_cp_work_mode_ms,
             w_decision, w_boundary) = state["wireless"]
            (d_state, d_cp_ratio, d_cur_cp, d_buck, d_boundary,
             d_cur_max, d_stage_cur_max, d_qc_target) = state["wired"]
            # 有线输入遥测：只解析最后一次会话边界之后的日志段；
            # 同一行（log_time/vbus/ibus）不刷新 at，避免旧值被伪装成刚刚采到；
            # 新会话/协议变化后尚无遥测时清空缓存并标记等待，页面回退 USB uevent
            if is_wired_disconnected(pp_log):
                self.last_voters = clear_vote_topics(self.last_voters, WIRED_VOTER_TOPICS)
                self.last_wired_qc_target = None
                self.last_wired_qc_target_key = None
                self.last_wired_cp_tel = None
                self.last_wired_buck_tel = None
                self.last_wired_tel_waiting = False
            else:
                tail = split_after_last_wired_boundary(pp_log)
                cp_tel = parse_wired_cp_telemetry(
                    tail, self.adb.utc_offset_minutes)
                buck_tel = parse_wired_buck_telemetry(
                    tail, self.adb.utc_offset_minutes)
                if cp_tel is None and buck_tel is None:
                    self.last_wired_cp_tel = None
                    self.last_wired_buck_tel = None
                    self.last_wired_tel_waiting = has_wired_boundary(pp_log)
                else:
                    self.last_wired_tel_waiting = False
                    if cp_tel is not None:
                        old = self.last_wired_cp_tel
                        key = (cp_tel["log_time"], cp_tel["vbus_mv"], cp_tel["ibus_ma"])
                        if old is None or (old.get("log_time"),
                                           old.get("vbus_mv"),
                                           old.get("ibus_ma")) != key:
                            self.last_wired_cp_tel = cp_tel
                    if buck_tel is not None:
                        old = self.last_wired_buck_tel
                        key = (buck_tel["log_time"],
                               buck_tel["vbus_mv"], buck_tel["ibus_ma"])
                        if old is None or (old.get("log_time"),
                                           old.get("vbus_mv"),
                                           old.get("ibus_ma")) != key:
                            self.last_wired_buck_tel = buck_tel
            # 无线 track：复用会话 key。同一 power_good_on 反复出现在窗口
            # 不重置 last_cur_decision_key，避免 at 被“本次扫描时间”假刷新。
            if pp_off:
                pass  # 上面已清空
            elif pp_new_session:
                if w_cp_mode is not None:
                    self.last_cp_mode = w_cp_mode
                    self.last_wls_cp_evidence = w_cp_mode > 0
                    if w_cp_mode == 0:
                        # 明确切到 Buck：清掉旧 work_mode，避免页面永远保持 CP
                        self.last_cp_work_mode = None
                        self.last_wls_work_mode_ms = None
                if w_cp_work_mode is not None:
                    self.last_cp_work_mode = w_cp_work_mode
                    self.last_wls_cp_evidence = w_cp_work_mode in (1, 2, 4)
                    if w_cp_work_mode_ms is not None:
                        self.last_wls_work_mode_ms = w_cp_work_mode_ms
                if w_decision is not None:
                    self.last_cur_decision = w_decision
                self.last_cur_decision_key = None
            elif w_boundary and pg_ms_pp != self.last_wls_session_ms:
                # pp 窗口里的边界与已确认会话不一致：不采纳本轮 CP/Final
                pass
            else:
                if w_cp_mode is not None:
                    self.last_cp_mode = w_cp_mode
                    self.last_wls_cp_evidence = w_cp_mode > 0
                    if w_cp_mode == 0:
                        # 明确切到 Buck：清掉旧 work_mode，避免页面永远保持 CP
                        self.last_cp_work_mode = None
                        self.last_wls_work_mode_ms = None
                if w_cp_work_mode is not None:
                    self.last_cp_work_mode = w_cp_work_mode
                    self.last_wls_cp_evidence = w_cp_work_mode in (1, 2, 4)
                    if w_cp_work_mode_ms is not None:
                        self.last_wls_work_mode_ms = w_cp_work_mode_ms
                if w_decision is not None:
                    key = (w_decision.get("log_time", ""),
                           w_decision.get("final"))
                    if key != self.last_cur_decision_key:
                        self.last_cur_decision_key = key
                        self.last_cur_decision = w_decision
            # 有线 track：usb online / real_type changed 边界
            if d_boundary:
                self.last_wired_state = d_state
                self.last_wired_cp_ratio = d_cp_ratio
                self.last_wired_cur_cp = d_cur_cp
                self.last_wired_buck = d_buck
                self.last_wired_cur_max = d_cur_max
                self.last_wired_stage_cur_max = d_stage_cur_max
                self.last_wired_qc_target = d_qc_target
                self.last_wired_cur_max_key = None
                self.last_wired_stage_cur_max_key = None
                self.last_wired_qc_target_key = None
            else:
                if d_state != "unknown":
                    self.last_wired_state = d_state
                if d_cp_ratio is not None:
                    self.last_wired_cp_ratio = d_cp_ratio
                if d_cur_cp:
                    self.last_wired_cur_cp = True
                if d_buck:
                    self.last_wired_buck = True
                if d_cur_max is not None:
                    key = (d_cur_max.get("log_time", ""),
                           d_cur_max.get("cur_max"))
                    if key != self.last_wired_cur_max_key:
                        self.last_wired_cur_max_key = key
                        self.last_wired_cur_max = d_cur_max
                if d_stage_cur_max is not None:
                    key = (d_stage_cur_max.get("log_time", ""),
                           d_stage_cur_max.get("cur_max"))
                    if key != self.last_wired_stage_cur_max_key:
                        self.last_wired_stage_cur_max_key = key
                        self.last_wired_stage_cur_max = d_stage_cur_max
                if d_qc_target is not None:
                    key = (d_qc_target.get("log_time", ""),
                           d_qc_target.get("fcc"), d_qc_target.get("ibus"))
                    if key != self.last_wired_qc_target_key:
                        self.last_wired_qc_target_key = key
                        self.last_wired_qc_target = d_qc_target

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

    def _clear_wireless_session_state(self) -> None:
        """无线会话级状态统一清空：真正的新 power_good_on 或断开时调用。

        last_wls_session_ms 由调用方维护；这里只清 wireless-session scoped 数据，
        避免上一会话的 Final/CP/ICL/rx_iout_limit 冒充当前会话。
        """
        self.last_wls_icl = None
        self.last_wls_icl_at = None
        self.last_wls_icl_log_time = None
        self.last_wls_icl_ms = None
        self.last_wls_icl_key = None
        self.last_wls_chg_en = None
        self.last_epp = None
        self.last_quick_cur_max = None
        self.last_buck_fcc = None
        self.last_cp_mode = None
        self.last_wls_cp_evidence = False
        self.last_cp_work_mode = None
        self.last_wls_work_mode_ms = None
        self.last_cur_decision = None
        self.last_cur_decision_key = None
        self.last_wls_mode = "unknown"
        self.last_rx_iout_limit = None
        self.rx_iout_limit_captured = False
        self.last_rx_iout_limit_at = None
        self.last_rx_iout_limit_log_time = None

    def _decorate_wireless_path(self, core: dict) -> None:
        """Publish wireless path independently from the rolling voter snapshot."""
        derived = core.setdefault("derived", {})
        cp_ibus = derived.get("cp_ibus_total_ma")
        batt_current = derived.get("batt_current_ma")
        input_current = derived.get("input_current_ma")
        status = str(core.get("battery", {}).get("status", {}).get("value", "")).lower()
        live_wireless = (
            derived.get("input_source") == "wireless"
            and status == "charging"
            and isinstance(input_current, (int, float)) and input_current > 0
            and isinstance(batt_current, (int, float)) and batt_current > 0
            and isinstance(cp_ibus, (int, float))
        )
        wm = getattr(self, "last_cp_work_mode", None)
        cp_mode = getattr(self, "last_cp_mode", None)
        session_cp = ((cp_mode is not None and cp_mode > 0)
                      or (cp_mode is None and wm in (1, 2, 4))
                      or bool(getattr(self, "last_wls_cp_evidence", False)))
        if live_wireless:
            current = abs(cp_ibus)
            live_state = "cp" if current >= 100.0 else "buck" if current <= 20.0 else "transition"
            path_state = "cp" if live_state == "buck" and session_cp else live_state
            path_source = "sysfs_cp_ibus_total+session_cp_mode" if path_state != live_state else "sysfs_cp_ibus_total"
        elif wm in (1, 2, 4):
            path_state, path_source = "cp", "quick_wireless_work_mode"
        elif cp_mode is not None:
            path_state = "cp" if cp_mode > 0 else "buck"
            path_source = "sc8581_operation_mode"
        else:
            path_state, path_source = "unknown", "none"
        path = {
            "session_at": getattr(self, "last_wls_session_ms", None) or 0,
            "state": path_state,
            "cp_active": path_state == "cp",
            "cp_state_source": path_source,
            "cp_session_evidence": bool(session_cp),
            "ratio": wm if path_state == "cp" and wm in (1, 2, 4) else None,
            "wls_mode": getattr(self, "last_wls_mode", "unknown") or "unknown",
            "rx_iout_limit": getattr(self, "last_rx_iout_limit", None),
            "rx_iout_limit_captured": bool(getattr(self, "rx_iout_limit_captured", False)),
            "rx_iout_limit_at": getattr(self, "last_rx_iout_limit_at", None) or 0,
            "rx_iout_limit_time": getattr(self, "last_rx_iout_limit_log_time", None) or "",
            "rx_iout_limit_stale": bool(getattr(self, "session_logs_stale", False)),
            "smartendura_soc_limit": bool(getattr(self, "last_smartendura_soc_limit", False)),
        }
        if isinstance(cp_ibus, (int, float)) and live_wireless:
            path["cp_ibus_total_ma"] = cp_ibus
        if getattr(self, "last_wls_work_mode_ms", None) is not None:
            path["wls_work_mode_ms"] = self.last_wls_work_mode_ms
        quick_cur = getattr(self, "last_quick_cur_max", None)
        buck_fcc = getattr(self, "last_buck_fcc", None)
        if quick_cur is not None or buck_fcc is not None:
            path["battery_limit_ma"] = (
                quick_cur if quick_cur is not None else buck_fcc)
            path["battery_limit_source"] = (
                "quick_wireless cur_max" if quick_cur is not None
                else "wireless loop buck_fcc")
        if getattr(self, "last_cur_decision", None) is not None:
            path["cur_max_decision"] = copy.deepcopy(self.last_cur_decision)
        derived["wireless_path"] = path

    def _merge_cached_logs(self, core: dict) -> None:
        core["voters"] = copy.deepcopy(self.last_voters)
        core["sessions"] = copy.deepcopy(self.last_sessions)
        # 独立发布无线功率路径：前端不得把 wireless_buck_input votable 是否出现在
        # 当前日志窗口，误当成 CP/Buck 路径是否存在。
        self._decorate_wireless_path(core)
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
                buck["icl_ms"] = self.last_wls_icl_ms or 0
                if self.last_wls_chg_en is not None:
                    buck["chg_en"] = self.last_wls_chg_en
        buck = core.get("voters", {}).get("wireless_buck_input")
        if buck is not None:
            actual = self.last_quick_cur_max if self.last_quick_cur_max is not None else self.last_buck_fcc
            if actual is not None:
                buck["actual_limit"] = actual
                buck["actual_limit_source"] = (
                    "quick_wireless cur_max" if self.last_quick_cur_max is not None
                    else "wireless loop buck_fcc")
            # 活跃无线充电优先使用实时 CP 总线电流；空闲时回退会话日志。
            derived = core.get("derived", {})
            status = str(core.get("battery", {}).get("status", {}).get("value", ""))
            cp_ibus = derived.get("cp_ibus_total_ma")
            input_iout = derived.get("input_current_ma")
            batt_current = derived.get("batt_current_ma")
            live_wireless_charging = (
                derived.get("input_source") == "wireless"
                and status.lower() == "charging"
                and isinstance(input_iout, (int, float)) and input_iout > 0
                and isinstance(batt_current, (int, float)) and batt_current > 0
                and isinstance(cp_ibus, (int, float))
            )
            wm = self.last_cp_work_mode
            if live_wireless_charging:
                cp_active = abs(cp_ibus) >= 1.0
                buck["cp_state"] = "cp" if cp_active else "buck"
                buck["cp_active"] = cp_active
                buck["cp_state_source"] = "sysfs_cp_ibus_total"
                buck["cp_ibus_total_ma"] = cp_ibus
                if cp_active and wm is not None:
                    buck["cp_ratio"] = wm
            elif wm in (1, 2, 4):
                buck["cp_state"] = "cp"
                buck["cp_ratio"] = wm
                buck["cp_active"] = True
                buck["cp_state_source"] = "quick_wireless_work_mode"
            elif self.last_cp_mode is not None:
                cp_active = self.last_cp_mode > 0
                buck["cp_state"] = "cp" if cp_active else "buck"
                buck["cp_active"] = cp_active
                buck["cp_state_source"] = "sc8581_operation_mode"
                if cp_active and wm is not None:
                    buck["cp_ratio"] = wm
            else:
                buck["cp_state"] = "unknown"
                buck["cp_active"] = False
                buck["cp_state_source"] = "none"
            if self.last_wls_work_mode_ms is not None:
                buck["wls_work_mode_ms"] = self.last_wls_work_mode_ms
            if self.last_cur_decision is not None:
                buck["cur_max_decision"] = copy.deepcopy(self.last_cur_decision)
            # 无线控制模式：仅标识 BPP drawload / EPP+/QC，不做 ICL/iout 一致性判定
            buck["wls_mode"] = self.last_wls_mode
            # rx_iout_limit：会话状态机输出（captured / at / stale）
            buck["rx_iout_limit"] = self.last_rx_iout_limit
            buck["rx_iout_limit_captured"] = self.rx_iout_limit_captured
            buck["rx_iout_limit_at"] = self.last_rx_iout_limit_at or 0
            buck["rx_iout_limit_time"] = self.last_rx_iout_limit_log_time or ""
            buck["rx_iout_limit_stale"] = bool(self.session_logs_stale)
            buck["smartendura_soc_limit"] = bool(self.last_smartendura_soc_limit)
        # 有线 CP 三态：cp / buck / unknown（时间顺序 + CP 证据优先）
        wstate = self.last_wired_state
        core.setdefault("derived", {})["wired_cp"] = {
            "state": wstate,
            "ratio": self.last_wired_cp_ratio if wstate == "cp" else None,
            "active": wstate == "cp",
            "cur_work_cp": bool(self.last_wired_cur_cp),
            "cur_max": (copy.deepcopy(self.last_wired_cur_max)
                        if self.last_wired_cur_max is not None else None),
            "stage_cur_max": (copy.deepcopy(self.last_wired_stage_cur_max)
                              if self.last_wired_stage_cur_max is not None else None),
            "qc_target": (copy.deepcopy(self.last_wired_qc_target)
                          if self.last_wired_qc_target is not None else None),
        }
        meta = core.setdefault("meta", {})
        meta.update({
            "interval": self.current_fast_interval(),
            "fast_interval": self.current_fast_interval(),
            "fast_mode": self.current_fast_mode(),
            "viewer_active": self.viewer_active(),
            "logs_interval": self.current_logs_interval(),
            "logs_updated_at": int(self.logs_updated_at),
            "logs_stale": self.vote_logs_stale or self.session_logs_stale,
            "power_path_logs_stale": self.power_path_logs_stale,
            "version": VERSION,
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
                "interval": self.current_fast_interval(),
                "fast_interval": self.current_fast_interval(),
                "fast_mode": self.current_fast_mode(),
                "viewer_active": self.viewer_active(),
                "logs_interval": self.current_logs_interval(),
                "logs_updated_at": int(self.logs_updated_at),
                "logs_stale": self.vote_logs_stale or self.session_logs_stale,
                "power_path_logs_stale": self.power_path_logs_stale,
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
        usb: dict[str, str] = {}
        if "usb_uevent" in batch:
            parsed = parse_uevent(batch["usb_uevent"]["raw"])
            for k in ("ONLINE", "TYPE", "VOLTAGE_NOW", "CURRENT_NOW"):
                usb[k.lower()] = parsed.get(k, "")
        return {"nodes": nodes, "battery": battery, "usb": usb}

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
        wireless_power = (
            (vout * iout / 1e6)
            if vout is not None and iout is not None
            else None
        )

        # 有线输入遥测：按 wired_cp.state 选择来源（CP→regulation，Buck→buckchg，
        # unknown→最新一条），USB uevent 仅作兜底且永远带 source/时间。
        usb = raw.get("usb", {})
        usb_online_raw = str(usb.get("online", "")).strip()
        usb_online_state = True if usb_online_raw == "1" else False if usb_online_raw == "0" else None
        usb_online = usb_online_state is True
        usb_known_off = usb_online_state is False
        usb_vbus_mv = num(usb.get("voltage_now", ""))
        if usb_vbus_mv is not None:
            usb_vbus_mv /= 1000.0          # uV -> mV
        usb_ibus_ma = num(usb.get("current_now", ""))
        if usb_ibus_ma is not None:
            usb_ibus_ma /= 1000.0          # uA -> mA

        wireless_signal = (
            vout is not None and iout is not None
            and vout > 1000.0 and iout > 100.0
        )
        # 物理输入源必须先于共享 ibus_total 归属；ONLINE=0 是有线硬否决。
        input_source = resolve_input_source(usb_online_state, usb_vbus_mv, wireless_signal)
        wired_present = input_source == "wired"
        wireless_connected = resolve_wireless_connected(
            self.last_wireless_connected, input_source, vout)

        wstate = self.last_wired_state
        cp_tel = self.last_wired_cp_tel
        buck_tel = self.last_wired_buck_tel
        if input_source == "wired" and wstate == "cp":
            chosen = cp_tel or buck_tel
        elif wstate == "buck":
            chosen = buck_tel or cp_tel
        elif cp_tel and buck_tel:
            chosen = cp_tel if cp_tel["log_time"] >= buck_tel["log_time"] else buck_tel
        else:
            chosen = cp_tel or buck_tel

        now_ms = int(ts * 1000)
        wired_stale = False
        if chosen is not None:
            age = now_ms - chosen["at"]
            wired_stale = age > WIRED_TELEMETRY_STALE_MS.get(chosen["source"], 30000)
        # 策略日志遥测（校验数据）：regulation/buckchg 的 vbus/ibus，不再承担实时曲线
        tel_vbus_mv = None
        tel_ibus_ma = None
        tel_source = None
        tel_log_time = ""
        tel_at = None
        if chosen is not None and not usb_known_off and not (wired_stale and usb_online):
            tel_vbus_mv = chosen["vbus_mv"]
            tel_ibus_ma = chosen["ibus_ma"]
            tel_source = chosen["source"]
            tel_log_time = chosen["log_time"]
            tel_at = chosen["at"]
        # 实时测量主源（每 3s）：
        #  有线 CP         → ibus_total（电荷泵总线电流，sysfs）
        #  有线 Buck/unknown → usb uevent CURRENT_NOW
        #  两者都不可用     → 回退策略日志遥测
        rt_vbus_mv = None
        rt_ibus_ma = None
        rt_source = None
        rt_at = None
        ibus_total = num(nodes.get("ibus_total", {}).get("value", ""))
        chg_block = self.last_voters.get("chg_enable") or {}
        chg_result = chg_block.get("result") if isinstance(chg_block, dict) else None
        charging_enabled = None
        if isinstance(chg_result, dict) and chg_result.get("value") is not None:
            try:
                charging_enabled = int(float(chg_result.get("value"))) == 1
            except (TypeError, ValueError):
                charging_enabled = None
        preferred_wired_source = resolve_wired_input_source(
            wstate, ibus_total, usb_ibus_ma, usb_online_state is True,
            charging_enabled, batt_cur_ma)
        if input_source == "wired" and preferred_wired_source == "cp_ibus_total":
            if ibus_total is not None:
                vb = tel_vbus_mv if tel_vbus_mv is not None else usb_vbus_mv
                if vb is not None:
                    rt_vbus_mv = vb
                    rt_ibus_ma = ibus_total
                    rt_source = "cp_ibus_total"
                    rt_at = now_ms
        if rt_source is None and input_source == "wired" and usb_online_state is True:
            if usb_vbus_mv is not None and usb_ibus_ma is not None:
                rt_vbus_mv = usb_vbus_mv
                rt_ibus_ma = usb_ibus_ma
                rt_source = "usb_uevent"
                rt_at = now_ms
        if rt_source is None and input_source == "wired" and tel_vbus_mv is not None and tel_ibus_ma is not None:
            rt_vbus_mv = tel_vbus_mv
            rt_ibus_ma = tel_ibus_ma
            rt_source = tel_source
            rt_at = tel_at
        wired_online = wired_present and rt_vbus_mv is not None and rt_ibus_ma is not None
        # mV × mA = µW，直接换算成 W（除以 1e6），前端只显示 W
        wired_power = (
            rt_vbus_mv * rt_ibus_ma / 1e6
            if wired_online else None
        )

        # 当前输入源已经由 USB/无线物理证据决定，不能被共享 CP 遥测反向改写。
        if input_source == "wireless":
            input_vol_mv = vout
            input_cur_ma = iout
            input_power = wireless_power
        elif input_source == "wired":
            input_source = "wired"
            input_vol_mv = rt_vbus_mv
            input_cur_ma = rt_ibus_ma
            input_power = wired_power
        else:
            input_vol_mv = None
            input_cur_ma = None
            input_power = None
        # 电池功率保留正负号（充电为正、放电为负）
        battery_power = (
            batt_cur_ma * batt_vol_mv / 1e6
            if batt_cur_ma is not None and batt_vol_mv is not None
            else None
        )

        derived = {
            "vout": vout, "vrect": wls.get("vrect"), "iout": iout,
            "input_source": input_source,
            "wireless_connected": bool(wireless_connected),
            "wired_connected": bool(wired_present),
            "input_connected": bool(wired_present or wireless_connected),
            "input_detail_source": (
                rt_source if input_source == "wired"
                else "wls_debug" if input_source == "wireless" else None
            ),
            "input_power_w": round(input_power, 2) if input_power is not None else None,
            "input_voltage_mv": input_vol_mv,
            "input_current_ma": input_cur_ma,
            "wired_online": wired_online,
            "wired_vbus_mv": round(rt_vbus_mv, 1) if rt_vbus_mv is not None else None,
            "wired_ibus_ma": round(rt_ibus_ma, 1) if rt_ibus_ma is not None else None,
            "wired_input_power_w": round(wired_power, 2) if wired_power is not None else None,
            "wired_input_source": rt_source,
            "wired_input_at": rt_at,
            "wired_input_log_time": tel_log_time,
            "wired_input_age": (
                round((now_ms - rt_at) / 1000.0)
                if rt_at is not None else None
            ),
            "wired_input_waiting": (
                self.last_wired_tel_waiting and chosen is None and usb_online
            ),
            "wired_input_stale": wired_stale,
            # 校验遥测（策略日志）：与实时曲线源分层展示
            "wired_tel_source": tel_source,
            "wired_tel_vbus_mv": (
                round(tel_vbus_mv, 1) if tel_vbus_mv is not None else None
            ),
            "wired_tel_ibus_ma": (
                round(tel_ibus_ma, 1) if tel_ibus_ma is not None else None
            ),
            "wired_tel_stale": wired_stale,
            "wired_tel_log_time": tel_log_time,
            "wired_tel_at": tel_at,
            "wired_usb_online": usb_online_state is True,
            "cp_ibus_total_ma": num(nodes.get("ibus_total", {}).get("value", "")),
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
                "interval": self.current_fast_interval(),
                "fast_interval": self.current_fast_interval(),
                "fast_mode": self.current_fast_mode(),
                "viewer_active": self.viewer_active(),
                "logs_interval": self.current_logs_interval(),
                "logs_updated_at": int(self.logs_updated_at),
                "logs_stale": self.vote_logs_stale or self.session_logs_stale,
                "power_path_logs_stale": self.power_path_logs_stale,
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
            self.server.sampler.mark_viewer_access()
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
            # 每次请求实时读取，避免旧进程一直提供启动时的旧页面
            index_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
            try:
                with open(index_path, "r", encoding="utf-8") as fh:
                    body = fh.read().encode("utf-8")
            except OSError:
                self.send_error(404, "index.html not found next to server.py")
                return
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
    parser.add_argument("--logs-interval", type=float, default=10.0,
                        help="vote/session log interval in seconds (default 10)")
    parser.add_argument("--idle-interval", type=float, default=12.0,
                        help="fast sampling interval while not charging (default 12)")
    parser.add_argument("--no-viewer-interval", type=float, default=45.0,
                        help="fast sampling interval without page access (default 45)")
    parser.add_argument("--no-viewer-after", type=float, default=30.0,
                        help="seconds without page access before background mode (default 30)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default 8765)")
    parser.add_argument("--open", action="store_true", help="open the page in the default browser")
    args = parser.parse_args()

    adb = AdbReader(args.adb_host, args.serial, args.adb)
    if not adb.available:
        print(f"[warn] ADB device unavailable ({adb.last_error}); page will show offline state.")
    sampler = Sampler(adb, args.interval, args.logs_interval,
                      args.idle_interval, args.no_viewer_interval, args.no_viewer_after)
    sampler.start()

    server = DashboardServer(("127.0.0.1", args.port), sampler, None)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"[ok] dashboard running: {url}  "
          f"(fast={args.interval}s, idle={args.idle_interval}s, "
          f"no-viewer={args.no_viewer_interval}s, logs={args.logs_interval}s, "
          f"adb={adb.adb_bin or 'not found'}, version={VERSION})")
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
