# AGENTS.md — 智能水循环监测与温控物联网系统 · AI 编程约束文档

> 本文件是该项目对 AI 编程助手的行为约束与实现规范。
> 任何 AI 在本仓库执行修改前，必须先完整阅读本文件；用户最新指令与本文件冲突时，以用户最新指令为准，并同步更新本文件。

## 1. 项目背景与目标

- 赛题：智能水循环监测与温控物联网系统（`题目.md`）。
- 系统：现场 ESP32 采集控制端 + 上位机后端(FastAPI) + MySQL + Vue3 前端，构成
  「感知层-网络层-平台层-应用层」全栈链路。
- 任务：硬件搭建(20) / 采集与传输(15) / 后端与存储(20) / 前端可视化(15) /
  智能判定服务对接(10) / 进阶拓展：告警 + 恒温闭环 + 多终端自适应 + 统计(20)。

### 1.1 现场设备的真实能力（重要）

现场采集端 `http://192.168.31.100` 烧录的是 **《YF-S401 水流量检测》** 固件，
实际只提供以下通道：

| 通道 | 状态 | 说明 |
| --- | --- | --- |
| 瞬时流量 | ✅ 可用 | `/api/flow`、`/api/data.flowRate`，L/min |
| 累计水量 | ✅ 可用 | `/api/volume`、`/api/data.totalLiters`，L |
| 水泵开关 | ✅ 可用 | `/api/pump/on\|off\|toggle`、`/api/pump/state` |
| 定量浇水 | ✅ 可用 | `/api/pump/target?value=n`，达到目标由固件自动关泵 |
| 累计清零 | ✅ 可用 | `/api/reset`（破坏性） |
| 设备健康 | ✅ 可用 | `/api/health`：uptime/IP/RSSI |
| 液位 ×2 | ⏳ 待接入 | 双水槽（储水槽/加热槽）各一路，**尚未接入**；当前由后端模拟（界面标注「模拟」） |
| 水温 | ❌ 无 | `/api/temperature` 返回 404（不是文档里写的 503） |
| 压力 | ❌ 无 | `/api/data` 中无压力字段 |
| 加热模块 | ❌ 无 | `/api/heater/*` 全部返回 404 |

> ⚠️ 用户提供的接口清单与现场固件不一致：清单里有 `temperature`/`tempOk`/`heater`，
> 实测均不存在。**一切以实测为准**，改动前先用 `/api/health`、`/api/data` 与设备首页
> `GET /` 复核真实路由，不要照抄文档。

因此本项目采用「纯真实模式 + 按能力裁剪」：温度/压力/加热相关面板、告警与恒温闭环(PID)
均置为不可用并明确标注「设备不支持」，**不生成任何模拟值**。
能力开关集中在 `backend/config.py` 的 `DEVICE_FEATURES`，固件升级后改这一处即可启用。

**液位是唯一的例外**：用户明确要求「液位传感器即将加装、暂未接入、先用模拟数据」。
本系统为**双水槽循环加热系统**（储水槽 + 加热槽），故有两路液位通道
`level_storage` / `level_heater`；对应能力为 False 时由 `WaterPlant._simulate_levels()`
按「水量守恒」生成模拟水位（水泵运行：储水槽→加热槽，储水槽保留最低水位防抽干；
停机：两槽缓慢回平；速率见 `SIM_LEVEL_*`）。模拟与设备通信解耦——设备掉线时水位仍继续演化，不冻结。
这类模拟**必须在界面与接口上明确标注**（`tank.tanks.<槽>.source="sim"`、「模拟」徽标），
不得与实测数据混淆——它是经用户批准的演示数据，不是"伪造真实读数"。
传感器接入后把对应 `DEVICE_FEATURES["level_*"]` 改为 `True` 即自动切换为实测值（两槽可分别接入）。

## 2. 已确认的架构与技术栈（锁定决策）

未经用户明确同意不得更改：

| 层次 | 选型 | 备注 |
| --- | --- | --- |
| 前端 | Vue 3（全局构建版）+ ECharts | 本地引库、无构建步骤 |
| 后端 | Python 3.11+ FastAPI + Uvicorn | 自带 `/docs` 便于验证接口 |
| 数据库 | MySQL 8.x（PyMySQL 连接池 + DBUtils） | 库名 `iot_system`，字符集 `utf8mb4` |
| 采集端 | 现场 ESP32，HTTP + JSON，后端 1Hz 轮询 | 见 `backend/esp32_client.py` |
| 数据来源 | **纯真实模式**（唯一例外：液位传感器未接入，暂用模拟值并明确标注） | 设备离线则字段为 `None`，前端提示离线 |
| 前后端通信 | HTTP + JSON，实时数据 2s 轮询 | 不引入 WebSocket |
| 鉴权 | JWT + 角色权限矩阵 | 权限点见 `backend/auth.py` |

## 3. 目录结构约定

```text
frontend/
├── index.html
├── css/style.css
├── js/
│   ├── api.js                  # 接口封装（统一 code/msg/data 处理）
│   ├── charts.js               # ECharts 辅助
│   ├── auth.js                 # 登录态与权限
│   ├── components/
│   │   ├── login.js
│   │   ├── waterLoop.js        # 双水槽循环回路 SVG 组件（罐体/管路/水泵/水流粒子）
│   │   ├── waterDashboard.js   # 三栏驾驶舱 + 历史/统计/告警/日志
│   │   ├── judgePanel.js       # 任务五：判定服务对接页
│   │   └── sysConfig.js        # 告警阈值/采集周期/水槽容积/设备信息/账号角色
│   ├── app.js                  # 根组件与轮询
│   └── lib/                    # vue / echarts（本地文件）
backend/
├── main.py                     # FastAPI 入口 + 1Hz 采集循环
├── config.py                   # 【现场修改】设备地址、能力表、双水槽液位、阈值、判定服务
├── esp32_client.py             # ESP32 HTTP 客户端（读写全部设备接口）
├── water_device.py             # 设备模型：纯真实模式 + 双水槽液位模拟，离线降级
├── database.py                 # MySQL 封装（连接池 + 建表 + 迁移）
├── alarm.py                    # 告警引擎（按设备能力启用通道）
├── api.py                      # HTTP 业务接口
├── pid_control.py              # PID 算法（当前设备不支持，已按能力禁用）
├── judge_service.py            # 判定服务客户端
├── store.py                    # 运行期动态配置（config 表 sys.* 前缀）
├── state.py                    # 全局运行时状态
└── tests/
    ├── test_api.py             # 接口功能测试（需先启动后端）
    └── test_level_sim.py       # 双水槽液位模拟单元测试（可独立运行）
tests/
└── e2e_web.js                  # 前端端到端测试（真实浏览器 + CDP，需先启动后端）
```

## 4. 数据模型（MySQL）

库 `iot_system`，utf8mb4，InnoDB。单套水循环设备，`device_id` 统一 `water`。

- `water_sensors`：`ts`、`flow_rate`、`total_flow`(累计水量 L)、`pump_target`、
  `pump_state`，以及**可空**的 `storage_temp`/`heater_temp`/`pressure`/`heater_state`。
  建 `idx_ts` 索引。
  **迁移说明**：因现场固件无温度/压力/加热通道，上述 4 列由 `NOT NULL` 改为可空，
  无数据写 `NULL` 而非 `0`（避免假数据污染曲线与统计）；新增 `pump_target`。
  迁移在 `database._migrate_water_sensors()` 中幂等执行，不丢历史数据。
- `alarms`：`device_id`、时间、类型、数值、阈值、方向、消息、状态、恢复时间。
- `control_log`：`device_id`、时间、`action`、`result`、`detail`、**`operator`(操作账号名)**、
  **`source`(manual/auto/judge)**。`action` 用前缀区分类别：设备控制无前缀
  （`pump_on` 等），系统配置为 `config.*`，账号管理为 `account.*`。
  **迁移说明**：原日志没有操作人，无法审计"谁做的"；新增 `operator`/`source` 两列，
  历史行保持 `operator=NULL`（前端显示「—」，不猜测、不回填）。
  迁移在 `database._migrate_control_log()` 中幂等执行。**日志中不得出现密码明文。**
- `config`：key-value。业务阈值用原名，运行期配置用 `sys.` 前缀（见 `store.py`）。
- `users`：账号、密码哈希、角色、状态。

时间戳统一 `yyyy-MM-dd HH:mm:ss`。

> 遗留：`config` 表里还存着与本赛题无关的历史键（`smoke_max`、`humidity_*`、
> `lamp_posts`、`valve_*` 等）。水循环业务不读取它们，属数据库历史残留，暂未清理。

## 5. 接口规范（契约）

统一返回：`{"code": 0, "msg": "ok", "data": {...}}`。

| 错误码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 40002 | 参数错误 |
| 40003 | 控制指令执行失败 / 设备不支持该通道 |
| 40004 | 设备或记录不存在 |
| 40006 | 账号或密码错误 |
| 40101 | 未登录或登录过期 |
| 40301 | 无权限 |

核心接口见 `README.md` 的「核心接口」表。

**真实数据原则**：设备不支持或离线时，接口返回 `None` 与 `features` 能力表，
**绝不用模拟值或 0 冒充真实读数**；控制指令失败必须返回 40003，不得伪造成功。

## 6. AI 编码行为准则

1. **先读后改**：修改任何文件前先读取其当前内容。
2. **锁定决策优先**：第 2 节为锁定项，不得自行替换；确需变更先征得用户同意。
3. **范围克制**：只实现用户要求的改动，不做超出范围的顺手重构。
4. **离线优先**：不得引入 CDN 依赖；新增 Python/JS 依赖最小化并同步 `requirements.txt`。
   调用设备仅用标准库 `urllib`。
5. **命名与注释**：标识符英文，注释中文。
6. **避免破坏性操作**：不执行 `rm -rf`、`git reset --hard` 等；不擅自调用 `/api/reset` 清零
   现场累计水量，不擅自开泵抽水。
7. **以实测为准**：设备接口清单可能与现场固件不符，改动前先探测真实路由。
8. **验证后再交付**：每次改动完成后运行相应验证并在回复中说明。

## 7. 验证清单

- [ ] `python main.py` 启动无报错，`/docs` 可访问
- [ ] 启动日志打印采集设备地址、支持通道、PID 是否禁用
- [ ] `/api/water/realtime` 返回真实流量/累计水量/水泵状态，`features` 正确
- [ ] 传感数据周期入库，`/api/water/history` 按时间范围可查，无数据通道为 NULL
- [ ] 水泵控制真实改变设备状态并由实时接口回显；失败返回 40003
- [ ] 定量浇水目标可读可写；清零接口有二次确认且权限受控
- [ ] 实时监控为三栏驾驶舱（左KPI / 中循环回路 / 右操作），1366×768 以上首屏零滚动
- [ ] 循环回路：两罐体并排、水泵节点位于两罐之间、管路与水流粒子动画正常、SVG id 唯一不串位
- [ ] 「更多操作」中的清零按钮默认折叠，展开后才可见；液位为模拟时明确标注「模拟」
- [ ] 设备掉线时模拟水位仍继续演化（不冻结）
- [ ] 操作日志记录操作人（人工为账号名，自动动作为「系统 · 恒温闭环 / 判定服务」），支持分类筛选
- [ ] 系统配置与账号管理类操作均已入日志，且不含密码明文
- [ ] `python backend/tests/test_api.py` 全部通过（默认不执行开泵与清零）
- [ ] `python backend/tests/test_level_sim.py` 全部通过
- [ ] `node tests/e2e_web.js` 全部通过（真实浏览器，默认不点水泵开关、不清零）
- [ ] 前端页面无控制台报错，实时数据 ≤2s 刷新
