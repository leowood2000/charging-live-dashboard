# K80 Pro 无线充电实时仪表盘（ADB 版）

单页仪表盘 + Python 标准库后端：通过 ADB root 读取 Redmi K80 Pro（miro）MCA 无线私有快充的所有充电实时数据，整合到一页展示。数据语义与安卓版 `SnapshotCollector.java` 完全对齐——本仓库是“安卓版仪表盘的 ADB 后端版本”，而不是另一套实现。

## 功能特性

- **双周期采集**：快速采集 3 秒（sysfs 节点、battery uevent、thermal.dump、历史曲线），日志采集 10 秒（投票表、会话、EPP、实际下发 ICL），避免每 3 秒重扫几 MB 日志
- **电流符号统一**：充电为正、放电为负，`Not charging` 也按放电处理；电池功率保留正负号；原始 JSON 仍保留内核原值方便排查
- **MCA 仲裁移植**：支持 `voting on/off`、完整 client 字符集、单位白名单（未知主题不默认 mA）、`VOTE_POLICIES`（大部分来自 .ko 反汇编核实；`buck_charge_curr` 为项目假设 `MIN_ASSUMED`，仅详情卡“参考推算”）、按主题缓存 `changed`/`result`
- **总仲裁只显示实际结果**：无线输入侧只展示 **RX 输出电流上限**（`rx_iout_limit`，允许上限）与 **实际 RX 输出电流**（wls_debug iout）；`wireless_buck_input` / `wls_icl` / `xm_wls` / `wireless_qc` 不进总仲裁（详情卡保留）
- **投票区只显示生效主题**：有启用票或驱动已给出实际结果的主题才显示卡片
- `wireless_auth_*`（20w/30w/50w/80w/voice_box/magnet）与 `wireless_bpp/bppqc2/bppqc3/epp_in` 是按充电板型号/协议模式预置的热控表，无论连接哪台垫都会被整批投票，已直接隐藏
- `term_volt` / `term_curr`（JEITA 终止电压/电流）合并为一张“JEITA 终止参数”卡并标注**静态常数**（由温度档决定，会话内基本不变）
- 有线/无线快充禁用卡按当前连接动态显示
- 仲裁结果带**有效性校验**：主题全部撤票（如拔掉充电）后，旧 `effective vote is now` 不再展示，卡片按“生效主题”隐藏
- 功率路径判定只以 sc8581 电荷泵 work_mode 为准（不用电流大小猜测）：首页显示“当前功率路径”（电荷泵 / Buck 直充）
- “当前功率路径”chip 附带电荷泵转换比（由 quick wireless work_mode 映射：1:1 bypass / 2:1 div2 / 4:1 div4）
- 未充电（battery STATUS ≠ Charging）时隐藏全部投票/仲裁卡片，仅保留生效场景、虚拟温度、电池温度与 JEITA 静态参数
- `wireless_buck_input` 固定放在详情卡：MCA 仲裁（effective 赢家）+ ADSP 无线 ICL（prop 0x1003）+ `xm_wls` 能力票 + 说明（已下发 ADSP，闭源固件如何应用不可见，不等同 RX 输出电流上限）
- 不再用 `wls_icl` 与 `iout` 做“限流未生效”判定（BPP/EPP+/QC 均不比较）：反编译证据链为 `effective → strategy_wireless_set_input_curr_limit → platform_class_buckchg_ops_set_wls_input_curr_lmt → mca_adsp_glink_write_prop(0x1003)`，落地权在闭源 ADSP 固件
- `rx_iout_limit` 随无线会话保持：`power_good_on` 捕获、会话内持续有效，`work_mode`（1:1/2:1/4:1）切换与日志窗口滚动不失效，`power_good_off` 清空；会话日志读取失败时保留值并标 stale
- 无线电池侧上限按当前功率路径选择：CP 生效 → quick wireless `cur_max:[Final]`（算法决策，带年龄/历史值标注）+ 无线热控上限（`wireless_thermal_(20/30/50/80)w` 对应充电器类别，取驱动按检测类别唯一启用的 voter，多票时显示待确认、不做 max 猜测）；Buck 生效 → `buck_charge_curr` effective；路径未确认 → 显示“待确认”，不用 Buck FCC 冒充当前上限；`buck_charge_curr` 在 CP 下标注“Buck 路径 FCC”
- 新增只读卡“Quick Wireless 电池电流决策”：展示 select_max_ibat 的五个输入（channel_cur / temp_max_cur / tx_adapter_max / sw_qc_ichg / sw_thermal_ichg，标注当前瓶颈）、`cur_max:[Final]` 与实际 ibat，明确标注“算法聚合 · 非 MCA votable”
- CP 状态按会话解析：遇到 `power_good_on/off` 重置，只保留当前会话内的 sc8581 模式/分压比/cur_max，避免上一会话残留冒充当前值
- 功率路径三态显示：cp（本会话 operation mode>0，附 2:1 等分压比）/ buck（本会话明确 mode=0）/ Buck / CP 未激活（待确认）（本会话尚无 SC8581 模式日志）
- 有线功率路径正式接入：会话边界（`usb online` / `real_type changed` / `power_good`）内按时间顺序取最后一次 `sc8581 operation mode` 判定 cp/buck；`quickchg work_mode` 与 `map_ibus_to_fsw ratio` 提供分压比；`cur_work_cp` 作交叉证据；输出 `derived.wired_cp`
- 有线 CP 激活时只显示对应比例的 div 卡（4:1 → div4_single/div4_multi）+ single/multi_chg_cur + thermal_flip；Buck/未知时隐藏全部 div，保留 buck_input / buck_charge_curr / chg_enable / quick_chg_disable / input_voltage / smartchg_delta_ichg / JEITA；`buck_5v/9v_*` 档位表与 `wireless_*` 始终隐藏
- 日志抓取白名单补齐有线 quickchg 信号（`update_work_mode_para` / `map_ibus_to_fsw` / `mca_quick_charge_select_max_ibat` / `select_cur_work_mode`）与有线会话边界（`usb online` / `real_type changed`）
- 采集通道拆分：session/event 走 3 文件低频瘦通道；功率路径信号走最新 1 文件 1MB 专用通道（手机端 grep + tail -n 200 封顶）
- stale 独立：`logs_stale` 只代表 vote/session 主链路，`power_path_logs_stale` 单独输出；功率路径读取失败时保留上次成功状态
- 无线/有线 SC8581 状态彻底解耦：`power_good` 只重置无线 track，`usb online` / `real_type changed` 只重置有线 track；SC8581 operation mode 仅在对应 quickchg 上下文出现后写入对应 track
- 有线 Buck 确认：当前有线会话出现 `mca_strategy_buckchg / strategy_buckchg` 活动且无 CP 证据时，路径判为“Buck 直充（有线）”；有线状态按时间顺序 + CP 证据优先（mode>0 / mode=0 后 cur_work_cp → CP，mode=0 或 buckchg → Buck，均无 → 待确认）
- **仲裁展示分离**：`effective vote is now`（MCA 逻辑仲裁）与 `wls_icl`（ADSP GLINK prop 0x1003 下发值）在详情卡独立展示；`rx_iout_limit`（RX 输出电流上限）与 `wls_debug iout`（实时输出电流）在总仲裁展示，两组互不覆盖
- **会话档案**：以 `power_good_on` 建立会话，`power_good_off` 记入“充电板移除”事件；保留全部电流变化与 open path 事件；最多 3 个会话、每个会话最多 100 条事件
- **日志容错**：日志读取失败保留上次成功数据并标记 `logs_stale`；读取成功但 grep 无匹配不算失败
- **ADB 自动重连**：每 5 秒节流重试 `adb connect` + `adb devices`，启动时未连接或中途掉线可自动恢复；指定 `--adb-host` 时优先使用该设备
- **断开清理**：无线断开（最后的电源事件为 `power_good_off`）时清除旧 ICL 与 EPP，避免上一会话残留
- **主界面精简**：删除“芯片与系统 / 电荷泵与电池 / 电流投票与限流 / 电池标准属性”四张卡；对应 sysfs 节点与 battery uevent 仍完整保留在 `/api/data` 与原始 JSON
- **桌面端固定栅格**：1320px 居中，KPI 三列、实时数据两列、曲线两列、总仲裁整行、会话单列；900px/560px 以下自动切换移动布局
- **meta schema_version=2**：`fast_interval`、`logs_interval`、`logs_updated_at`、`logs_stale`、`source`、`device` 等字段供页面双倒计时与刷新使用

## 界面预览

桌面端（1320px 固定栅格，KPI 三列、实时数据两列）：

![桌面端总览](docs/screenshot-desktop.png)

实时曲线与总仲裁结果：

![详情与曲线](docs/screenshot-detail.png)

电流仲裁实时表（总仲裁无线侧 = RX 输出电流上限 + 实测 RX 输出；电池侧 = 电池充电电流上限；wireless_buck_input 详情卡保留 MCA 仲裁 / ADSP ICL / 能力票）：

![电流仲裁实时表](docs/screenshot-arbitration.png)

实时会话档案：

![会话档案](docs/screenshot-sessions.png)

手机端（移动布局，KPI 单列、卡片双列小格）：

![手机端](docs/screenshot-mobile.png)

## 运行

```powershell
python server.py
```

打开 <http://127.0.0.1:8765/>。

也可以直接双击 **`启动仪表盘.bat`**：它会自动进入本目录、**先停止旧的 server.py 实例**（避免旧进程占住 8765 提供旧页面）、启动服务并打开浏览器；窗口会打印当前代码版本（git 短哈希），出错时停留显示错误信息。

**在其他电脑上使用（克隆仓库后）：**

1. 安装 Python 3（本项目只用标准库，无第三方依赖）
2. 安装/准备 adb：把 `adb.exe` 所在目录放到 `C:\adb`（脚本会自动探测），或加入 PATH；也可用 `--adb` 指定位置
3. 手机开启“无线调试”（开发者选项），并执行 `adb connect <手机IP>:5555` 连上设备
4. 双击 `启动仪表盘.bat`，或运行 `python server.py --open`

不写死设备地址：服务会**自动使用 `adb devices` 里已连接的设备**；有多台设备或需要指定时再用 `--adb-host`/`--serial`。

常用参数：

```powershell
python server.py --adb-host 192.168.33.118:5555 --port 8765 --interval 3 --logs-interval 20
启动仪表盘.bat 192.168.33.118:5555
```

- `--adb-host`：ADB over Wi-Fi 地址（默认空 = 自动识别已连接设备，且优先选择该设备）
- `--serial`：使用 `adb -s` 指定序列号
- `--adb`：adb 可执行文件或所在目录，默认自动探测 `C:\adb\adb.exe`，找不到再找 PATH
- `--interval`：快速采样/刷新间隔（sysfs、电池、热控、历史曲线），默认 `3` 秒
- `--logs-interval`：日志采集间隔（投票表、会话、EPP、实际下发 ICL），默认 `20` 秒
- `--port`：HTTP 端口，默认 `8765`
- `--open`：启动后自动打开浏览器

需要设备已 root（KernelSU 等），后端通过 `adb shell su -c "cat <节点>"` 读取 sysfs 节点。

## 数据语义（与安卓版一致）

### 电流与功率

| 电池状态 | `derived.batt_current_ma` | `derived.battery_power_w` |
| --- | --- | --- |
| Charging | 正值 | 正（充电功率） |
| Discharging | 负值 | 负（放电功率） |
| Not charging | 负值 | 负 |
| Full / Unknown | 保留内核原始值 | 跟随电流符号 |

- `derived.batt_current_ma`：µA → mA 并统一符号
- `derived.batt_voltage_mv`：µV → mV
- `battery.current_now` / `battery.voltage_now` 等原始 uevent 值**原样保留**，页面展示层负责换算；排查内核原始值直接看原始 JSON
- `real_type` 状态化：`Unknown` 在放电/未充电时显示“未连接（放电中）”，充电中显示“未识别（充电中）”，不当作采集失败
- 热控电流节点（`wired_chg_curr` / `wireless_chg_curr`）原始单位 µA，页面按 `ua_to_ma` 换算为 mA 并标注“策略上限”

### 投票仲裁

- `VOTE_UNITS`：只有已核实的电流主题才标注 `mA`，未知主题默认空单位
- `VOTE_POLICIES`：大部分来自 miro 固件 .ko 反汇编核实（MIN / FIRST_NONZERO / FIRST_ZERO / UNKNOWN）；`buck_charge_curr` 为项目假设 `MIN_ASSUMED`，无 effective 行时只允许详情卡“参考推算”，不进入总仲裁 fallback
- `changed`/`result` 按主题分别缓存，日志交错时不会串线；`voting off` 正确解析为 `enabled: false`
- MCA `effective vote is now` 是逻辑仲裁结果；`wls_icl` 经 `mca_adsp_glink_write_prop(0x1003)` 下发 ADSP（闭源固件如何应用不可见）；`rx_iout_limit` 是 RX 输出电流上限（允许上限）；`wls_debug iout` 是实时测量值，四者互不覆盖

### 会话

- 以 `power_good_on` 建立新会话，并结束上一个未结束会话
- `power_good_off` 标记会话结束并写入“充电板移除”事件，之后的事件不再追加到已结束会话
- 保留全部 `set chg current` / `open path ibus` 事件；峰值/当前限流取最后 3 个会话内的最大/最新值

## API

`GET /api/data` 返回完整快照，顶层结构：

```json
{
  "ts": 1786023004333,
  "iso": "2026-08-06 21:54:36",
  "mode": "live",
  "connected": true,
  "nodes": [],
  "battery": {},
  "derived": {},
  "history": [],
  "voters": {},
  "sessions": [],
  "thermal": {},
  "meta": {
    "schema_version": 2,
    "interval": 3,
    "fast_interval": 3,
    "logs_interval": 20,
    "logs_updated_at": 1786023004333,
    "logs_stale": false,
    "adb": "192.168.5.13:5555",
    "source": "adb",
    "device": "192.168.5.13:5555"
  }
}
```

`GET /` 返回单页仪表盘（index.html）。

## 设备连不上时

**不显示任何伪造数据。** 页面会显示“设备离线”横幅和具体错误（如 `adb executable not found in PATH`、无设备在线），所有实时数值保持占位符；ADB 恢复后页面自动恢复实时采集（后端每 5 秒自动重试连接）。

## 常见问题

- **改了代码但页面没变化**：`index.html` 现在每次请求实时读取，不需要重启；`server.py` 的改动需要重启服务（启动脚本会自动停旧实例再启动）
- **页面打不开 / 数值一直回跳**：启动脚本会先停止旧实例；若仍异常，确认只有一个 `server.py` 监听 8765（页面状态栏有版本号可核对是否为最新）
- **页面显示“服务连接失败”**：确认访问的是 <http://127.0.0.1:8765/>，而不是直接双击打开 `index.html`（file:// 协议下无法请求 `/api/data`）
- **日志显示“读取失败”但设备正常**：只有 ADB/su/文件读取失败才标记 `logs_stale`；grep 无匹配（日志中暂时没有投票/会话内容）不算失败

## 文件

- `server.py`：Python 标准库实现，批量读取 MCA 实时节点 + battery uevent；双线程分别执行快速采集与日志采集，提供 `/`（页面）与 `/api/data`（JSON，含最近 180 个采样点的历史曲线数据）
- `index.html`：单页仪表盘（实时数据按 `fast_interval` 轮询 `/api/data`，投票/会话按 `logs_interval` 展示，桌面端固定栅格布局）
- `启动仪表盘.bat`：一键启动脚本（进入目录、带默认 ADB 地址启动、打开浏览器、出错停留）
- `无线充电热控数据库.md`：K80 Pro 无线充电热控完整数据库（温度→等级→电流/功率、全部充电场景、虚拟温度公式），数据来自设备树与解密后的热控配置

## 数据来源

`K80Pro_小米无线私有充电协议深度分析.md`（2026-08-03 实测）；实时投票表来自设备滚动日志 `/data/vendor/bsplog/charge/charge_logger/mca_log/`（投票变化时打印完整 VOTER 表，SoC 步进期间约每 10 秒一次），页面每 10 秒更新一次并按最新一张表展示。
