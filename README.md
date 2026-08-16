# K80 Pro 无线充电实时仪表盘（ADB 版）

单页仪表盘 + Python 标准库后端：通过 ADB root 读取 Redmi K80 Pro（miro）MCA 无线私有快充的所有充电实时数据，整合到一页展示。数据语义与安卓版 `SnapshotCollector.java` 完全对齐——本仓库是“安卓版仪表盘的 ADB 后端版本”，而不是另一套实现。

## v0.11.31 CP 比例解析修复

- 识别没有 `mca_*quick_charge` 前缀的 `sc8581_set_operation_mode` 日志，并按最近的 USB / 无线物理边界归属。
- 从同一行 `work_mode` 保留有线或无线 CP 的 1:1、2:1、4:1 比例，避免路径卡只显示“CP”。

## v0.11.30 mi_thermald 场景直读

- 快速采集在同一批 ADB 读取中加入 `sconfig` 与 `screen_state`，场景优先采用 `thermal-map.conf` 的实际配置索引。
- Web 在手机熄屏但仍充电时识别并记录 `chg-only（熄屏充电）`；`thermal.dump` 仍负责虚拟温度和无线热控目标，避免用滚动日志行猜场景。
- `/api/data.thermal` 增加 `sconfig`、`screen_state`、`scene_source` 字段，便于核对场景切换证据。

## v0.11.29 投票日志增量读取与空窗口回退

- mca_vote 前台按文件偏移增量读取，单次最多补读 256KiB；长时间未运行、日志轮转或进程重启时最多回退最新 2MiB，不会因历史日志增长而追赶无限内容。
- 首次打开或轮转边界没有投票行时，当前限制使用实时/会话派生路径和限制值，并明确标注投票日志待刷新，不再短暂显示空白限制卡。
- 无线旁路状态保留会话内 `wireless_icl`，与 RX 实测电流分层展示。

## 功能特性

- **有线 CP 限制摘要收敛**：主卡只显示最终瓶颈（Quick Charge/QC 目标、当前 div 热控与 SIC-BAT 取最小值）；非瓶颈候选保留在详情提示，避免 `12,400mA` 与 `15,500mA` 同时被误读为两个执行上限。
- **限制卡时序稳定**：日志通道在刷新边界短暂为空时，同一输入会话保留上一张稳定限制卡；新会话或路径变化不会沿用旧卡。
- **有线 HVDCP/QC3 目标兼容**：当前会话若没有 `mca_quick_charge_select_max_ibat` 的 Quick Charge Final，解析 `mca_qc_get_vbus_change_trend` 的 `target_limit_fcc_ma` 并标为“QC 调节目标”；该值仍与当前 div 热控票取证，不冒充 Quick Charge Final。

- **自适应双周期采集**：充电时快速采集 3 秒，未充电时 12 秒，页面超过 30 秒无访问时 45 秒；日志采集 10 秒（断开时 60 秒），避免后台持续唤醒手机或每 3 秒重扫几 MB 日志
- **电流符号统一**：充电为正、放电为负，`Not charging` 也按放电处理；电池功率保留正负号；原始 JSON 仍保留内核原值方便排查
- **MCA 仲裁移植**：支持 `voting on/off`、完整 client 字符集、单位白名单（未知主题不默认 mA）、`VOTE_POLICIES`（大部分来自 .ko 反汇编核实；`buck_charge_curr` 为项目假设 `MIN_ASSUMED`，仅详情卡“参考推算”）、按主题缓存 `changed`/`result`
- **总仲裁只放当前充电控制/最终限制结果**：无线侧 = 无线平台输入 ICL（`wireless_buck_input` effective → ADSP 0x1003，上游策略）+ 当前电池充电电流上限（按功率路径取 `cur_max:[Final]` 或 `buck_charge_curr`）；RX 输出上限、实测 RX 输出、控制模式、连接类型移入详情卡（链路能力/遥测信息）
- **投票区只显示生效主题**：有启用票或驱动已给出实际结果的主题才显示卡片
- `wireless_auth_*`（20w/30w/50w/80w/voice_box/magnet）与 `wireless_bpp/bppqc2/bppqc3/epp_in` 是按充电板型号/协议模式预置的热控表，无论连接哪台垫都会被整批投票，已直接隐藏
- `term_volt` / `term_curr`（JEITA 终止电压/电流）合并为一张“JEITA 终止参数”卡并标注**静态常数**（由温度档决定，会话内基本不变）
- 有线/无线快充禁用卡按当前连接动态显示
- 仲裁结果带**有效性校验**：主题全部撤票（如拔掉充电）后，旧 `effective vote is now` 不再展示，卡片按“生效主题”隐藏
- 无线功率路径判定：quick wireless `work_mode=1/2/4` 是 CP 硬证据（本会话捕获后持续持有，窗口滚动不失效），`operation mode>0` 作交叉验证、`operation mode=0` 明确切 Buck（并清旧 work_mode），均无 → 待确认；首页显示“当前功率路径”（电荷泵 · 1:1/2:1/4:1 / Buck 直充）
- “当前功率路径”chip 附带电荷泵转换比（由 quick wireless work_mode 映射：1:1 bypass / 2:1 div2 / 4:1 div4）
- 未充电（battery STATUS ≠ Charging）时隐藏全部投票/仲裁卡片，仅保留生效场景、虚拟温度、电池温度与 JEITA 静态参数
- `wireless_buck_input` 提升为总仲裁上游层“无线平台输入 ICL”（effective → ADSP 0x1003，上游策略，非 Buck 专属）；详情卡保留 MCA 仲裁、控制模式、RX 输出允许上限、实际 RX 输出、`xm_wls` 能力票与说明
- 不再用 `wls_icl` 与 `iout` 做“限流未生效”判定（BPP/EPP+/QC 均不比较）：反编译证据链为 `effective → strategy_wireless_set_input_curr_limit → platform_class_buckchg_ops_set_wls_input_curr_lmt → mca_adsp_glink_write_prop(0x1003)`，落地权在闭源 ADSP 固件
- `rx_iout_limit` 随无线会话保持：`power_good_on` 捕获、会话内持续有效，`work_mode`（1:1/2:1/4:1）切换与日志窗口滚动不失效，`power_good_off` 清空；会话日志读取失败时保留值并标 stale
- 无线平台输入 ICL：总仲裁上游层取 `wireless_buck_input` effective（ADSP prop 0x1003，上游策略，非 Buck 专属）；EPP+/QC 下与 RX iout 映射未闭环、不做数值比较；`soc_limit` 为 effective winner + SmartEndura 上下文 + ibat≈0 时标“当前上游限制”
- 无线电池侧上限按当前功率路径选择：CP 生效 → 总览只显示 quick wireless `cur_max:[Final]`（算法决策，带年龄/历史值标注），不单独展示“无线热控上限”（`wireless_sw_thermal_ich` effective 与本轮 `sw_thermal_ichg` 不等价，热控输入在 Quick Wireless 决策卡内看）；Buck 生效 → `buck_charge_curr` effective + 无线热控上限取 `wireless_thermal_XXw`；多票显示待确认、不做 max 猜测；路径未确认 → 显示“待确认”，不用 Buck FCC 冒充当前上限；`buck_charge_curr` 在 CP 下标注“Buck 路径 FCC”
- 新增只读卡“Quick Wireless 电池电流决策”：展示 select_max_ibat 的五个输入（channel_cur / temp_max_cur / tx_adapter_max / sw_qc_ichg / sw_thermal_ichg，标注当前瓶颈）、`cur_max:[Final]` 与实际 ibat，明确标注“算法聚合 · 非 MCA votable”
- CP 状态按会话解析：遇到 `power_good_on/off` 重置，只保留当前会话内的 sc8581 模式/分压比/cur_max，避免上一会话残留冒充当前值
- 功率路径三态显示：cp（本会话 work_mode=1/2/4 或 operation mode>0，附分压比）/ buck（本会话明确 operation mode=0）/ 待确认（本会话尚无路径证据）
- 活跃无线充电时优先读取实时 `mca_platform_cp/ibus_total`：总线电流大于 0 判 CP、等于 0 判 Buck；未充电/暂停或节点缺失时才回退会话日志
- 有线功率路径正式接入：会话边界（`usb online` / `real_type changed` / `power_good`）内按时间顺序取最后一次 `sc8581 operation mode` 判定 cp/buck；`quickchg work_mode` 与 `map_ibus_to_fsw ratio` 提供分压比；`cur_work_cp` 作交叉证据；输出 `derived.wired_cp`
- 有线 CP 激活时只显示对应比例的 div 卡（4:1 → div4_single/div4_multi）+ single/multi_chg_cur + thermal_flip；Buck/未知时隐藏全部 div，保留 buck_input / buck_charge_curr / chg_enable / quick_chg_disable / input_voltage / smartchg_delta_ichg / JEITA；`buck_5v/9v_*` 档位表与 `wireless_*` 始终隐藏
- 日志抓取白名单补齐有线 quickchg 信号（`update_work_mode_para` / `map_ibus_to_fsw` / `mca_quick_charge_select_max_ibat` / `select_cur_work_mode`）与有线会话边界（`usb online` / `real_type changed`）
- 采集通道拆分：session/event 最多扫描最近 2 个轮转文件并只保留最新 1 个会话；功率路径信号走最新 1 文件 1MB 专用通道（手机端 grep + tail -n 200 封顶）
- 日志白名单改用 `grep -F -e ...` 固定字符串多模式匹配，避免 Android toybox `grep -E` 大正则反复扫描历史日志造成高 CPU
- stale 独立：`logs_stale` 只代表 vote/session 主链路，`power_path_logs_stale` 单独输出；功率路径读取失败时保留上次成功状态
- 无线/有线 SC8581 状态彻底解耦：`power_good` 只重置无线 track，`usb online` / `real_type changed` 只重置有线 track；SC8581 operation mode 仅在对应 quickchg 上下文出现后写入对应 track
- 有线 Buck 确认：当前有线会话出现 `mca_strategy_buckchg / strategy_buckchg` 活动且无 CP 证据时，路径判为“Buck 直充（有线）”；有线状态按时间顺序 + CP 证据优先（mode>0 / mode=0 后 cur_work_cp → CP，mode=0 或 buckchg → Buck，均无 → 待确认）
- **仲裁展示分离**：总仲裁 = 无线平台输入 ICL（上游）+ 电池侧最终上限（按路径）；`rx_iout_limit`（RX 输出允许上限）与 `wls_debug iout`（实测 RX 输出）收进无线平台输入 ICL 详情卡，与上游策略 ICL 分层展示、互不覆盖
- **会话档案**：无线以 `power_good_on/off`、有线以 `usb online`/`real_type` 建立和结束会话；记录协议变化、CP 模式/分压比/阶段、Buck 并行启停、全部电流序列与 open path 事件；只保留最新 1 个会话、每个会话最多 100 条事件
- **日志容错**：日志读取失败保留上次成功数据并标记 `logs_stale`；读取成功但 grep 无匹配不算失败
- **ADB 自动重连**：每 5 秒节流重试 `adb connect` + `adb devices`，启动时未连接或中途掉线可自动恢复；指定 `--adb-host` 时优先使用该设备
- **断开清理**：无线断开（最后的电源事件为 `power_good_off`）时清除旧 ICL 与 EPP，避免上一会话残留
- **主界面精简**：删除“芯片与系统 / 电荷泵与电池 / 电流投票与限流 / 电池标准属性”四张卡；对应 sysfs 节点与 battery uevent 仍完整保留在 `/api/data` 与原始 JSON
- **紧凑信息层次**：实时指标压缩为单行读数；当前限制集中展示电池侧、无线输入与温度/热控；策略组和会话默认折叠；曲线固定为电池电流、输入电流、输入电压、输入功率
- **投票降噪**：按电池充电限制、无线输入限制、使能/保护、修正项排序；默认只展开生效票，未生效票收进主题内折叠区
- **桌面端固定栅格**：1320px 居中，KPI 三列、实时数据两列、曲线两列、总仲裁整行、会话单列；900px/560px 以下自动切换移动布局
- **meta schema_version=2**：`fast_interval`、`logs_interval`、`logs_updated_at`、`logs_stale`、`source`、`device` 等字段供页面双倒计时与刷新使用

## 界面预览

桌面端（1320px 固定栅格，KPI 三列、实时数据两列）：

![桌面端总览](docs/screenshot-desktop.png)

实时曲线与总仲裁结果：

![详情与曲线](docs/screenshot-detail.png)

电流仲裁实时表（总仲裁 = 当前功率路径 + 无线平台输入 ICL + 当前电池充电电流上限 + 充电使能/快充禁用；RX 上限与实测 RX 输出在无线平台输入 ICL 详情卡）：

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
- `--logs-interval`：连接/充电时的日志采集间隔（投票表、会话、EPP、实际下发 ICL），默认 `10` 秒；完全断开时自动降到 `60` 秒
- `--idle-interval`：页面打开但未充电时的快速采集间隔，默认 `12` 秒
- `--no-viewer-interval`：页面长时间无访问后的快速采集间隔，默认 `45` 秒
- `--no-viewer-after`：多久没有页面访问后进入后台模式，默认 `30` 秒
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
- MCA `effective vote is now` 是逻辑仲裁结果；`wls_icl` 经 `mca_adsp_glink_write_prop(0x1003)` 下发 ADSP（上游无线平台输入 ICL，闭源固件如何应用不可见）；`rx_iout_limit` 是 RX 输出允许上限、`wls_debug iout` 是实时测量值，二者为链路能力/遥测信息，收在详情卡

### 会话

- 以 `power_good_on` 建立新会话，并结束上一个未结束会话
- `power_good_off` 标记会话结束并写入“充电板移除”事件，之后的事件不再追加到已结束会话
- 保留最新会话内全部 `set chg current` / `open path ibus` 事件；峰值/当前限流取该会话内的最大/最新值

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

`K80Pro_小米无线私有充电协议深度分析.md`（2026-08-03 实测）；实时投票表来自设备滚动日志 `/data/vendor/bsplog/charge/charge_logger/mca_log/`（投票变化时打印完整 VOTER 表，SoC 步进期间约每 10 秒一次）。连接/充电时页面按配置周期更新，完全断开时日志采集自动降到 60 秒。
