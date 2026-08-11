# 维护与跨会话交接说明

最后更新：2026-08-11，目标发布版本：v0.11.14。

本文是 Web 与 Android 两个仓库的共同维护上下文。重新开始会话或修改充电语义前，先阅读本文件；不要仅凭变量名推测硬件含义。

## 仓库与对应关系

- Web / ADB：`leowood2000/charging-live-dashboard`
  - 采集与解析：`server.py`
  - UI：`index.html`
- Android：`leowood2000/charging-live-dashboard-android`
  - 采集与解析：`SnapshotCollector.java`
  - root 执行：`RootShell.java`
  - 生命周期：`MainActivity.java`
  - UI：`app/src/main/assets/index.html`
- Android package：`com.leowood.chargingdashboard`
- 主要实测设备：Redmi K80 Pro / `miro`，HyperOS，KernelSU root。

两版的字段语义、投票过滤、路径判定和 UI 信息顺序应同步。UI 文件不是逐字相同：Android 还包含 WebView bridge 和本地快照回调；Web 版包含 HTTP 轮询与桌面自适应刷新。同步时按功能移植，不要整文件互相覆盖。

## 已确认的关键语义

### 三个无线电流层级不能混用

1. `wireless_buck_input effective`：上游无线平台输入 ICL，是无线 Buck/旁路状态下“无线输入 ICL”的来源；无线 CP 活跃时不作为 RX 实际限流上限。
2. `rx_iout_limit`：驱动策略层 RX 允许上限，只在详情中作为能力/策略参考。
3. `wls_debug iout`：实际 RX 输出电流，是遥测值。

首页可以把 1 和 3 配对显示，方便观察，但禁止用二者不相等推导“限流未生效”。EPP+/QC 下它们处在不同控制域，映射关系未闭环。

### `wireless_qc=100` 的坑

- 它不是“最终输入电流被设为 100mA”。已确认它表达策略修正/下降量语义。
- 不得把它加入最终 ICL 结论，也不要恢复旧 README 中 `MIN(...,100)=100mA winner` 的说法。
- 无线平台 ICL 的最终字段仍以 `wireless_buck_input effective` 为准；它不等价于无线 CP 的 `rx_iout_limit`。

### 其他投票语义

- `xm_wls` 是无线能力/适配器允许值，不等同当前仲裁 winner。
- `buck_charge_curr` 的 `MIN_ASSUMED` 是项目假设，只能在详情中标“参考推算”；没有驱动 effective 时不能冒充首页最终上限。
- 预置的 `wireless_auth_*`、`wireless_bpp*`、`wireless_epp_in` 表会批量出现，不代表当前充电板/协议，首页和主要投票卡保持隐藏。
- `effective vote is now` 必须经过当前主题仍有启用票的有效性检查，断开后不能继续展示旧结果。

## 功率路径与电池上限

- 活跃无线充电融合实时 `mca_platform_cp/ibus_total` 与当前会话路径证据：`≥100mA` 为 CP，`>20mA 且 <100mA` 为切换态；`≤20mA` 时，若当前会话已有 `operation mode>0`（或 mode 尚未捕获但 `work_mode=1/2/4`），仍判 CP，否则判 Buck。实机既观察到 CP 预启动 3–5mA，也观察到 CP 稳态节点瞬时读 0；禁止恢复“任意非零即 CP”或“只看单次 ibus”的判断。
- 自动停充阶段由 `charge_enable=0` 和实际电池电流直接覆盖显示为“停止中/已停止”，不展示停充前 CP/Buck 结论；无线断开或节点缺失时才按既有边界规则回退当前会话日志。
- 输入仍连接且 `chg_enable=0` 时，电流尚未降至 `300mA` 内显示“停止中”，降至 `≤300mA` 后显示“已停止”；两个阶段的当前电池上限都显示 `--`，不得回显旧 CP/Buck 目标。输入完全断开后不得用缓存的 OFF 票维持停充态，否则放电电流跨越阈值会造成充电项闪现。
- 无线仍保持有效 RX 输入而电池停充时属于旁路供电：当前会话的 `wireless_buck_input effective` 继续显示为“无线输入 ICL（旁路）”，但不作为电池或 CP 的直接限流结论；断开无线或新会话尚未捕获 ICL 时才显示 `-- / 待捕获`。
- `wired_online` 必须由实时 USB `ONLINE=1` 或 `VBUS>1V` 证明；CP `ibus_total` 和策略日志遥测只能补充电流/电压，不能单独证明仍插线。否则拔掉有线 CP 后残留的 0V/小电流或旧 regulation 行会制造幽灵有线连接。
- `buck_5v_in / buck_5v_ich / buck_9v_in / buck_9v_ich` 是四张独立的有线 Buck 档位表，不得在无线、未连接、有线 CP 或路径待确认时当作当前投票展示。仅当有线 Buck 已确认且实时 VBUS 有效时，按 `<7V → 5V 档`、`≥7V → 9V 档`显示对应的 `in + ich` 两张表；真正总仲裁仍以 `buck_input / buck_charge_curr` 为准。
- `work_mode=1/2/4` 分别对应 1:1、2:1、4:1 CP；明确 `operation mode=0` 表示 Buck。
- 无线 CP 路径的当前电池充电电流上限取手机原生 Quick Wireless `cur_max:[Final]`。
- 有线 CP 路径按当前分压比选择 `div1/div2/div4` 的 `mca_thermal` 上限；single/multi 同值时使用共同值，只有一侧有当前有效结果时使用该侧，二者异值且拓扑未知时显示“待确认”。有线 Quick Charge `cur_max` 只保留作诊断目标，不进入首页当前路径上限。
- Buck 路径取 `buck_charge_curr effective`。未捕获时必须显示 `-- / 待捕获`，不能整项消失。
- 路径不确定时显示“待确认”，不能用 Buck FCC 兜底冒充当前上限。
- 无线/有线 SC8581 会话状态必须隔离；无线 `power_good` 不应重置有线 track，反之亦然。

## 温度与界面不变量

- 实时数据中的“电池温度”来自 battery uevent，是电芯实体温度。
- “当前限制”中的“虚拟温度”来自 `VIRTUAL-SENSOR-FORMULA`，是热控决策主温度；保持绿色并与当前热控场景相邻。
- 未充电时仍显示虚拟温度与当前热控场景；投票/仲裁卡隐藏。
- thermal.dump 快速路径仍限定尾部 64KB，并用一次 `awk` 同时保留最新 `MONITOR-WIRELESS` 与最新通用 `VIRTUAL-SENSOR-FORMULA`；通用行负责空闲期的当前虚拟温度/场景，读取偶发为空时保留最后有效热控快照。
- 当前限制顺序固定：
  1. 充电路径独占一行；
  2. 当前电池充电电流上限 + 实际电池电流；
  3. 无线 CP：RX 输出允许上限 + 实际 RX 输出；无线 Buck/旁路：无线输入 ICL + 实际 RX 输出；
  4. 温度、场景、使能/保护等其他项目。
- Web 桌面端第 2、3 组各占一行，其他 chip 不得混入；窄屏可以在组内自然换行，但组与组之间必须硬换行。
- 曲线顺序固定：电池电流、输入电流、输入电压、输入功率。
- 投票详情优先：电池充电限制、无线输入限制、使能/保护、修正项；未生效票默认折叠。

## 功耗与采集约束

### Android

- 快速采集 3 秒；日志连接/充电 10 秒、完全断开 60 秒。
- App 进入后台/锁屏后停止 root 采集，并暂停 WebView 与 JavaScript 定时器。
- 快速路径合并为一次 root 命令；日志路径合并为一次 root 命令；`RootShell.exec` 保持串行，避免并发 `su`。
- 只保留最新一个会话；session/event 最多读取最近两个文件；功率路径只读最新文件 1 MiB 并 `tail -n 200`。
- 连续 `open path ibus` 是同一次 CP 建链电流爬升，展示时合并为一条并保留首末值/次数；不要解释为快充路径被反复开关。

### Web / ADB

- 快速采集：充电 3 秒、未充电且页面活跃 12 秒、页面 30 秒无访问后 45 秒。
- `/api/data` 请求会刷新 viewer 活跃时间；无页面请求才会进入 45 秒模式。
- 日志连接/充电 10 秒、完全断开 60 秒。
- `logs_stale` 与 `power_path_logs_stale` 分开；某一通道读取失败时保留该通道上次成功状态。

### 两版同时运行

- Android 前台和 Web 页面活跃时不会共享缓存，会分别产生 root/sysfs/日志读取，CPU 唤醒与日志峰值会叠加。
- 2026-08-10 实机方向性短测：Android 前台单独运行 20 秒时手机总忙碌率约 13.97%；同时开启 Web ADB 活跃采集约 14.91%。短窗口受系统任务和充电控制影响，只用于确认增量方向，不作为精确功耗指标。
- 做功耗基准时只保留一个采集前台：看 Web 时把 Android App 置后台（`onPause` 会停止采集）；看 Android 时关闭 Web 页面或停止 Web 服务。

## 日志窗口与会话

- 只展示最新一个会话，每个会话最多 100 个事件。
- 若最新日志文件没有当前会话起点，可向前读取一个轮转文件以拼出当前会话；不要恢复“三个历史会话”的扫描。
- grep 使用 toybox 兼容的固定字符串多模式：`grep -F -e ...`。不要重新引入巨型 `grep -E` 正则。
- `power_good_on` 开始无线会话，`power_good_off` 结束并清理旧的 ICL/EPP/路径状态。
- 读取成功但没有匹配不算失败；ADB、su 或文件读取失败才设置 stale。
- 所有带“几秒前”或用于新旧选择的日志事件必须使用“日志文件日期 + 行内本地时刻”的绝对毫秒，不得用扫描发生时刻。单个日志文件跨本地 00:00 时，若行时刻比文件名起始时刻早超过 12 小时，该行归到次日；CP/Buck 遥测也按绝对时间比较，禁止直接比较 `HH:mm:ss` 字符串。

## 发布检查表

1. Android 同时更新 `versionName` 与递增的 `versionCode`。
2. 清理 README 中与当前 UI/语义冲突的旧说法，尤其是 `wireless_qc=100mA` 和“实际 RX 只在详情卡”。
3. Web：解析内联 JavaScript、`py_compile server.py`、运行自适应间隔状态测试，并用真实 `/api/data` 做页面 smoke test。
4. Android：`gradle clean assembleDebug`，安装到实机，核对包版本、root 采集、前后台暂停和当前限制分组。
5. 用最终 APK 的实机画面更新 README 截图；Web 也使用当前本机服务截图。
6. 对两个仓库执行 `git diff --check`，只暂存本次相关文件。
7. Android Release 上传重命名后的 APK 和 `SHA256SUMS.txt`；Web Release 上传源代码包和校验文件。
8. 推送后核对远端 commit、tag、Release 状态和资产 SHA-256。

## 不要轻易改变的结论

- 不要根据变量名中的 `buck` 断言它只在 Buck 模式生效；`wireless_buck_input` 已确认是无线平台上游 ICL 主题。
- 不要把“实际电流小于上限”自动标为瓶颈或故障；电池、输入、RX、热控处在不同层级。
- 不要根据旧会话的 CP 模式、cur_max 或 RX 上限推断当前会话；所有会话态都必须有边界与 freshness。
- 不要为页面好看而伪造数据；没有证据时显示 `--`、待捕获或待确认。
