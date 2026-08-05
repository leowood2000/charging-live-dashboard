# K80 Pro 无线充电实时仪表盘

单页仪表盘 + 本地后端：把 Redmi K80 Pro（MCA 无线私有快充）的所有充电实时数据整合到一页，默认每 3 秒刷新；同时包含 2026-08-03 实测会话档案（握手时间线、功率实测、协议要点、状态机、投票机制、FOD 白名单、模块清单等）。

## 运行

```powershell
python server.py
```

打开 <http://127.0.0.1:8765/>。

也可以直接双击 **`启动仪表盘.bat`**：它会自动进入本目录、启动服务并打开浏览器；出错时窗口会停留并显示错误信息。

**在其他电脑上使用（克隆仓库后）：**

1. 安装 Python 3（本项目只用标准库，无第三方依赖）
2. 安装/准备 adb：把 `adb.exe` 所在目录放到 `C:\adb`（脚本会自动探测），或加入 PATH；也可用 `--adb` 指定位置
3. 手机开启“无线调试”（开发者选项），并执行 `adb connect <手机IP>:5555` 连上设备
4. 双击 `启动仪表盘.bat`，或运行 `python server.py --open`

不写死设备地址：服务会**自动使用 `adb devices` 里已连接的设备**；有多台设备或需要指定时再用 `--adb-host`/`--serial`。

常用参数：

```powershell
python server.py --adb-host 192.168.33.118:5555 --port 8765 --interval 3
启动仪表盘.bat 192.168.33.118:5555
```

- `--adb-host`：ADB over Wi-Fi 地址（默认空 = 自动识别已连接设备）
- `--serial`：使用 `adb -s` 指定序列号
- `--adb`：adb 可执行文件或所在目录，默认自动探测 `C:\adb\adb.exe`，找不到再找 PATH
- `--interval`：采样/刷新间隔，默认 `3` 秒
- `--port`：HTTP 端口，默认 `8765`
- `--open`：启动后自动打开浏览器

需要设备已 root（KernelSU 等），后端通过 `adb shell su -c "cat <节点>"` 读取 sysfs 节点。

## 设备连不上时

**不显示任何伪造数据。** 页面会显示“设备离线”横幅和具体错误（如 `adb executable not found in PATH`、无设备在线），所有实时数值保持占位符；ADB 恢复后页面自动恢复实时采集。

## 文件

- `server.py`：Python 标准库实现，批量读取 MCA 实时节点 + battery uevent，提供 `/`（页面）与 `/api/data`（JSON，含最近 180 个采样点的历史曲线数据）
- `index.html`：单页仪表盘（实时数据每 3 秒轮询 `/api/data` + 静态实测档案）
- `无线充电热控数据库.md`：K80 Pro 无线充电热控完整数据库（温度→等级→电流/功率、全部充电场景、虚拟温度公式），数据来自设备树与解密后的热控配置

数据来源：`K80Pro_小米无线私有充电协议深度分析.md`（2026-08-03 实测）。

实时投票表来自设备滚动日志 `/data/vendor/bsplog/charge/charge_logger/mca_log/`（投票变化时打印完整 VOTER 表，SoC 步进期间约每 10 秒一次），页面按最新一张表展示。
