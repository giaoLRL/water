# 智能水循环监测与温控物联网系统

依据《智能水循环监测与温控物联网系统》赛题实现的完整系统：现场 ESP32 采集控制端 +
FastAPI 后端 + MySQL + Vue 3 前端，覆盖「感知层 - 网络层 - 平台层 - 应用层」全栈链路。

## 现场设备（真实接入）

采集控制端默认地址 **`http://192.168.31.100`**，烧录固件为《YF-S401 水流量检测》。

| 通道 | 接口 | 状态 |
| --- | --- | --- |
| 瞬时流量 | `GET /api/data`、`/api/flow` | ✅ L/min |
| 累计水量 | `GET /api/data`、`/api/volume` | ✅ L |
| 水泵开关 | `GET /api/pump/on`、`/off`、`/toggle`、`/state` | ✅ |
| 定量浇水 | `GET /api/pump/target[?value=2.5]` | ✅ 达到目标固件自动关泵 |
| 累计清零 | `GET /api/reset` | ✅ 破坏性操作 |
| 设备健康 | `GET /api/health` | ✅ uptime / IP / RSSI |
| **液位(超声波)** | `GET /api/level` | ✅ 单路，**接在加热槽**（百分比）；储水槽无传感器 → 水位恒为 0 |
| 水温 ×2 | `GET /api/temperature` `/api/temperature2` | ✅ 探头1→储水槽、探头2→加热槽 |
| 压力 | `GET /api/pressure` | ✅ 固件单位 MPa，后端统一换算 kPa |
| 加热模块 | `GET /api/heater/*` | ✅ 开关 / 状态查询 |
| 光照 | `GET /api/light` | ✅ GY-302，单位 lx |

> ⚠️ 现场固件会持续升级，通道以实测为准（`/api/health`、`/api/data`、设备首页 `GET /`）。
>
> 本系统为 **纯真实模式 + 按能力裁剪**：能力关闭的通道不采集、不告警、不展示。
> **无数据一律保持 0（水位类）或 `null`（其他通道），不做任何模拟**，
> 并在界面明确标注原因（「无传感器」/「离线」/「设备不支持」）。
>
> 能力开关集中在 `backend/config.py` 的 `DEVICE_FEATURES`，固件变化时改这一处即可。

## 功能特性

- **实时监控（三栏驾驶舱，首屏零滚动）**：左栏关键指标（瞬时流量 / 累计水量 / 水泵 / 设备健康），
  中栏**双水槽循环回路**可视化——两罐体用出水与回流管路连成一个闭环，水泵画在管路上并随运行状态
  旋转、水流粒子沿管路移动；右栏操作区（定量浇水 / 最近操作 / 「更多操作」折叠区）。
  水箱高度随视口自适应，1366×768 以上分辨率**无需滚动**。
- **双水槽实时水位**：储水槽与加热槽各一个罐体，水面波浪、气泡上升、外侧刻度、罐内大字水位百分比。
  ⚠️ 液位**不做任何模拟**：加热槽为超声波实测值；储水槽无传感器，水位**恒显示 0** 并标注「无传感器」；
  设备离线时两槽水位统一归 0，罐内标注「离线」。接口用 `source: "device" | "none"` 区分来源。
- **远程控制**：水泵开关真实下发到现场设备，并回读真实状态；指令失败返回 40003，不伪造成功。
- **定量浇水**：设定目标水量，达到后由设备固件自动关泵；界面显示本次定量进度条，
  支持取消定量与累计水量清零（二次确认）。
- **历史数据**：按时间范围查询，折线图展示流量与累计水量趋势，支持自定义时间段。
- **数据统计**：区间平均/最高/最低流量、区间用水量、累计水量与采样点数。
- **异常告警**：流量阈值上下限可配置，超限即弹窗提示并入库；恢复后自动置为已恢复。
- **操作日志（可审计）**：记录**谁在什么时候做了什么**，覆盖三类操作——
  设备控制（水泵/定量/清零）、系统配置（告警阈值/采集周期/水槽容积）、账号管理（创建/删除/改角色/重置密码/权限矩阵）。
  系统自动动作（恒温闭环、判定服务下发指令）没有操作账号，标注为「系统 · 恒温闭环」/「系统 · 判定服务」，
  与人工操作明确区分；支持按分类筛选。**不记录任何密码明文。**
- **账号与权限**：JWT 登录 + 可自定义的角色权限矩阵（管理员 / 运维 / 观察员等）。
- **判定服务对接**（任务五）：按 1Hz 向组委会判定服务上报、轮询指令、回传执行结果。
- **多终端自适应**：PC 与手机端自适应布局。

## 技术栈

| 层次 | 选型 |
| --- | --- |
| 前端 | Vue 3（本地库，无构建）+ ECharts，PC / 移动端自适应 |
| 后端 | Python 3.11+ FastAPI + Uvicorn（自带 `/docs`） |
| 数据库 | MySQL 8.x（PyMySQL + DBUtils 连接池），库名 `iot_system` |
| 采集端通信 | 标准库 `urllib`，HTTP + JSON，后端 1Hz 轮询，无第三方依赖 |

## 快速启动

### 1. 启动数据库

本地 MySQL 需已就绪（创建 `iot_system` 库及 `iot_user` 账号）：

```sql
CREATE DATABASE IF NOT EXISTS iot_system DEFAULT CHARACTER SET utf8mb4;
CREATE USER IF NOT EXISTS 'iot_user'@'127.0.0.1' IDENTIFIED BY 'iot_pass_2026';
GRANT ALL ON iot_system.* TO 'iot_user'@'127.0.0.1';
FLUSH PRIVILEGES;
```

建表与表结构迁移由后端启动时自动完成（幂等，不丢历史数据）。

### 2. 安装依赖并启动后端

```powershell
python -m pip install -r backend\requirements.txt
cd backend
python main.py
```

启动日志会打印采集设备地址、已启用的通道，以及 PID 是否因设备不支持而禁用。

浏览器访问 <http://127.0.0.1:8000>，接口文档 <http://127.0.0.1:8000/docs>。

### 3. 环境变量（可选）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `IOT_DB_HOST/PORT/USER/PASSWORD/NAME` | 127.0.0.1 / 3306 / iot_user / iot_pass_2026 / iot_system | 数据库连接 |
| `WATER_DEVICE_URL` | `http://192.168.31.100` | 现场采集控制端地址 |
| `WATER_DEVICE_TIMEOUT` | `1.5` | 单次设备请求超时（秒） |
| `WATER_DEVICE_OFFLINE_AFTER` | `5.0` | 连续无响应多久判定离线并清空读数 |
| `WATER_SAMPLE_PERIOD` | `1.0` | 采集周期（秒），须 ≥1Hz |
| `WATER_JUDGE_URL` | 空 | 判定服务地址；留空则不上报 |

设备地址、能力表、告警阈值等现场参数集中在 `backend/config.py`，均用【现场修改】标注。

## 核心接口

统一返回格式：`{"code": 0, "msg": "ok", "data": {...}}`。

| 接口 | 方法 | 功能 |
| --- | --- | --- |
| `/api/water/realtime` | GET | 实时数据（流量 / 累计水量 / 水泵 / 设备状态 / 能力表 / 活跃告警） |
| `/api/water/history?start=&end=` | GET | 历史传感数据查询 |
| `/api/water/stats?start=&end=` | GET | 统计指标（流量均值极值、区间用水量、采样点数） |
| `/api/water/pump` | POST | 水泵控制 `{"action":"on\|off\|toggle"}` |
| `/api/water/pump/target` | GET/POST | 查询 / 设定定量浇水目标 `{"liters":2.5}` |
| `/api/water/volume/reset` | POST | 清零累计水量（破坏性，需权限） |
| `/api/water/device` | GET | 设备链路信息（在线 / IP / RSSI / 运行时长 / 支持通道） |
| `/api/water/tank` | POST | 设置水槽容积 `{"tank":"storage\|heater","capacity":1000}` |
| `/api/water/system` | GET | 系统状态（数据库、采集周期、设备地址与在线状态） |
| `/api/water/alarm/config` | GET/POST | 告警阈值读写（当前为流量上下限） |
| `/api/water/alarms` | GET | 告警记录查询（分页） |
| `/api/water/logs?category=&page=&page_size=` | GET | 操作日志（含操作人与来源；category=device/config/account） |
| `/api/water/ingest` | POST | 采集端主动上报（可选链路） |
| `/api/auth/login` | POST | 登录获取 token |
| `/api/auth/users`、`/api/auth/roles` | GET/POST | 账号与角色权限管理 |

默认管理员：`admin` / `admin123`（首次启动自动创建，请及时修改）。

已按设备能力停用：`/api/water/heater`、`/api/water/target`、`/api/water/pid/*`
（均返回 40003「当前采集设备不支持该通道」）。

## 测试

```powershell
# 1) 后端接口测试（需先启动后端）
python backend\tests\test_api.py

# 2) 液位通道契约单元测试（无模拟数据、无数据为 0；不访问网络/数据库，可独立运行）
python backend\tests\test_level.py

# 3) 前端端到端测试（真实 Chrome/Edge 无头浏览器 + CDP，需先启动后端）
node tests\e2e_web.js
```

E2E 覆盖：登录页与登录校验、双水槽 SVG 动画（数量/唯一 id/水位高度/布局）、各标签页与图表渲染、
控制件与后端状态一致性、系统配置保存（阈值/容积/采集周期，改后回读并恢复）、账号管理
（创建/删除用户）、判定服务页、响应式布局（手机 390px / 桌面 1440px 无横向溢出）、退出登录、
以及控制台报错收集。

环境变量：`E2E_BASE`（被测地址）、`E2E_BROWSER`（浏览器路径）、`E2E_HEADED=1`（显示窗口调试）。

**安全**：E2E 与接口测试都**不会启动水泵、不会清零累计水量**；点「清零累计水量」只验证
二次确认弹窗出现，然后选择"取消"。设备暂时无响应时，相关校验会降级为 `WARN` 而非 `FAIL`。

测试默认只执行**安全动作**（关水泵、读状态、设定定量目标为 0），
不会开泵抽水、不会清零累计水量。如需完整验证开泵链路：

```powershell
$env:WATER_TEST_ACTUATE = "1"; python backend\tests\test_api.py
```

清零累计水量为不可恢复操作，测试脚本永不执行，仅验证其权限拦截。

## 目录结构

```text
backend/        FastAPI 后端（配置 / 设备客户端 / 设备模型 / 数据库 / 告警 / 接口 / 判定）
frontend/       Vue 3 + ECharts 前端（双水槽水位 + 综合面板 + 判定服务页 + 系统配置页）
tests/          前端端到端测试（e2e_web.js，真实浏览器 + CDP）
scripts/        MySQL 与后端启动脚本
.runtime/       后端运行日志
```

## 现场注意事项

- **储水槽液位传感器接入后如何切换为实测值**：把 `backend/config.py` 的
  `DEVICE_FEATURES["level_storage"]` 改为 `True`，并按固件实际接口填写
  `LEVEL_PATH_STORAGE`（当前加热槽用的是 `/api/level`）
  与 `TANK_HEIGHT_CM_STORAGE`（水槽高度，cm）。
  采集端支持返回百分比（`unit:"%"`）或厘米（`unit:"cm"`/`"mm"`，自动按高度换算）。
  改完重启后端即可，界面会自动由「无传感器 / 0」切换为实测值；两槽可分别接入，互不影响。
- 现场实测本水循环回路流量约 **21~82 L/min** 且波动很大（2 秒内可从 16 跳到 82），
  `flow_max` 默认取 `80.0`；**实测已出现 81.9 L/min 的尖峰，可能触发误报**，
  建议按实际工况上调（如 120），或改为对流量均值告警。
- 现场设备存在 **`pump` 状态与真实水流不一致** 的现象（实测水泵状态为 `false` 时
  流量仍达 20~50 L/min、累计水量持续增长）。后端如实反映设备上报值，未做掩盖；
  请现场核对水泵供电与控制接线。
- **定量完成后设备会自动清零**：实测设定定量目标并完成后，固件会把 `totalLiters`
  复位为 0 并清除 `pumpTarget`。这是固件行为，后端与界面已按此处理（进度条会回退）。
- ESP32 偶发单次请求超时或连接被重置（实测约几分钟一次）。后端设计为：单次失败保留上一次读数
  并标记离线，连续超过 `WATER_DEVICE_OFFLINE_AFTER`(默认 5s) 才清空读数，避免界面数据闪烁。
- 加热模块无水位保护时严禁无水通电，避免干烧扣分。
