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

现场采集端 `http://192.168.31.100` 烧录的是 **《YF-S401 水流量检测》** 固件
（**2026-09-16 起升级**：新增双温度探头、压力传感器、加热模块、超声波水位与光照通道，共 20 个 GET 接口）。
当前固件真实提供以下通道：

| 通道 | 状态 | 说明 |
| --- | --- | --- |
| 瞬时流量 | ✅ 可用 | `/api/flow`、`/api/data.flowRate`，L/min |
| 累计水量 | ✅ 可用 | `/api/volume`、`/api/data.totalLiters`，L |
| 水泵开关 | ✅ 可用 | `/api/pump/on\|off\|toggle`、`/api/pump/state` |
| 定量浇水 | ✅ 可用 | `/api/pump/target?value=n`，达到目标由固件自动关泵 |
| 累计清零 | ✅ 可用 | `/api/reset`（破坏性） |
| 设备健康 | ✅ 可用 | `/api/health`：uptime/IP/RSSI |
| 水温 ×2 | ✅ 可用 | 探头1 `/api/temperature`→储水槽(storage_temp)、探头2 `/api/temperature2`→加热槽(heater_temp)；读数失败返回 503；`/api/data` 一次带回 temperature/temperature2 与质量位 tempOk/tempOk2 |
| 压力 | ✅ 可用 | `/api/pressure`，**固件单位 MPa**，后端 ×1000 统一换算为 kPa 入库/展示/告警；raw 为 0-4095 ADC；异常时 503 仍带 raw/电压 |
| 加热模块 | ✅ 可用 | `/api/heater/on\|off\|toggle`、`/api/heater/state` |
| 液位(超声波) | ✅ 可用(1路) | `/api/level` 返回百分比 `{"value":85,"unit":"%"}`，**接在加热槽**；`/api/level/height?value=mm` 预定高度标定(NVS 掉电不丢)，由固件换算百分比，后端无需调用；**储水槽无传感器 → 水位恒为 0** |
| 光照 | ✅ 可用 | `/api/light`，`{"value":76.7,"unit":"lx"}`（GY-302）；未识别到传感器时 503 |

> ⚠️ 固件约定：读数接口的 `value` 为负数时表示无效读数，**不写入数据库、仅查询**；
> 单通道读数失败（503）时对应字段置 `None`（液位等水位类字段归 0），不得生成模拟值顶替。
> 固件可能继续升级，改动前仍先用 `/api/health`、`/api/data` 与设备首页 `GET /` 复核真实路由。

因此本项目采用「纯真实模式 + 按能力裁剪」：温度/压力/加热/光照通道及加热槽液位
已于 2026-09-16 全量接入（采集、入库、告警、恒温闭环 PID、前端展示），能力开关集中在
`backend/config.py` 的 `DEVICE_FEATURES`，固件能力变化时改这一处即可。

**液位通道**：本系统为**双水槽循环加热系统**（储水槽 + 加热槽），两路液位通道
`level_storage` / `level_heater`。**加热槽已接超声波传感器（`/api/level`，实测）**；
**储水槽无传感器**，该槽水位**恒为 0**，界面标注「无传感器」。

> ⚠️ **已清除全部模拟数据（用户 2026-09-16 指令）**：任何通道没有数据一律**保持 0**，
> 并在界面明确提示原因（「无传感器」/「离线」），不再生成任何模拟/演示值。
> 该指令优先于本文档原有的"不用 0 冒充真实读数"表述：0 只在**同时带有
> `source="none"` 或离线标志**时使用，绝不与有效读数混淆。
> 原 `_simulate_levels()` / `SIM_LEVEL_*` 已整体删除。

储水槽传感器接入后把 `DEVICE_FEATURES["level_storage"]` 改为 `True` 并填写
`LEVEL_PATH_STORAGE` 即自动切换为实测值。

## 2. 已确认的架构与技术栈（锁定决策）

未经用户明确同意不得更改：

| 层次 | 选型 | 备注 |
| --- | --- | --- |
| 前端 | Vue 3（全局构建版）+ ECharts | 本地引库、无构建步骤 |
| 后端 | Python 3.11+ FastAPI + Uvicorn | 自带 `/docs` 便于验证接口 |
| 数据库 | MySQL 8.x（PyMySQL 连接池 + DBUtils） | 库名 `iot_system`，字符集 `utf8mb4` |
| 采集端 | 现场 ESP32，HTTP + JSON，后端 1Hz 轮询 | 见 `backend/esp32_client.py` |
| 数据来源 | **纯真实模式**（无任何模拟数据：没有数据一律为 0 或 `None` 并提示原因） | 设备离线则字段为 `None`/0，前端提示离线 |
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
│   ├── widgets.js              # 卡片目录/默认布局/取值解析（window.Widgets）
│   ├── components/
│   │   ├── login.js
│   │   ├── widgetShell.js      # 通用卡片渲染（value/spark/line/state/tank/control/builtin）
│   │   ├── waterDashboard.js   # 全卡片网格驾驶舱 + 历史/统计/告警/日志
│   │   ├── judgePanel.js       # 任务五：判定服务对接页
│   │   └── sysConfig.js        # 告警阈值/采集周期/水槽容积/设备信息/账号角色
│   ├── app.js                  # 根组件与轮询
│   └── lib/                    # vue / echarts / gridstack（本地文件）
backend/
├── main.py                     # FastAPI 入口 + 1Hz 采集循环
├── config.py                   # 【现场修改】设备地址、能力表、双水槽液位、阈值、判定服务
├── esp32_client.py             # ESP32 HTTP 客户端（读写全部设备接口）
├── water_device.py             # 设备模型：纯真实模式，离线降级（无数据一律为 0/None，不做模拟）
├── database.py                 # MySQL 封装（连接池 + 建表 + 迁移）
├── alarm.py                    # 告警引擎（按设备能力启用通道）
├── api.py                      # HTTP 业务接口
├── pid_control.py              # PID 算法（温度/加热通道已接入，恒温闭环可用，默认 WATER_PID_ENABLED=0 需界面/配置开启）
├── pid_loops.py                # 恒温闭环回路管理器（一张闭环卡=一路独立 PID，可多路并存）
├── judge_service.py            # 判定服务客户端
├── store.py                    # 运行期动态配置（config 表 sys.* 前缀）
├── state.py                    # 全局运行时状态
├── custom_channels.py          # 自定义传感器通道（由实时监控页卡片声明派生）
├── gateway.py                  # 外部设备接入适配器（Modbus TCP / 串口服务器 / 本机串口）
├── scheduler.py                # 定时任务调度（每天某时刻 / 每 N 秒执行卡片动作）
├── calibration.py              # 通道标定（scale/offset，两点标定换算工程值）
└── tests/
    ├── test_api.py             # 接口功能测试（需先启动后端）
    ├── test_level.py           # 液位通道契约单元测试（无模拟数据、无数据为 0，可独立运行）
    ├── modbus_sim.py           # Modbus 从站模拟器（外部设备接入自测，无需真实硬件）
    └── mock_judge.py           # 模拟智能判定服务（上报/轮询/反馈联调）
tests/
└── e2e_web.js                  # 前端端到端测试（真实浏览器 + CDP，需先启动后端）
scripts/
├── start_*.ps1                 # 启动脚本
└── make_submission.ps1         # 一键生成「技能赛提交文件」提交包（源码+文档+数据导出）
```

## 4. 数据模型（MySQL）

库 `iot_system`，utf8mb4，InnoDB。单套水循环设备，`device_id` 统一 `water`。

- `water_sensors`：`ts`、`flow_rate`、`total_flow`(累计水量 L)、`pump_target`、
  `pump_state`，以及**可空**的 `storage_temp`/`heater_temp`/`pressure`/`light`/`heater_state`。
  建 `idx_ts` 索引。
  **迁移说明**：上述 5 列由 `NOT NULL` 改为可空——设备离线或单通道读数无效
  （503/负值）时写 `NULL` 而非 `0`（避免假数据污染曲线与统计），通道现已启用并正常写入；
  新增 `pump_target`。
  迁移在 `database._migrate_water_sensors()` 中幂等执行，不丢历史数据。
- `alarms`：`device_id`、时间、类型、数值、阈值、方向、消息、状态、恢复时间。
- `control_log`：`device_id`、时间、`action`、`result`、`detail`、**`operator`(操作账号名)**、
  **`source`(manual/auto/judge)**。`action` 用前缀区分类别：设备控制无前缀
  （`pump_on` 等），系统配置为 `config.*`，账号管理为 `account.*`。
  **迁移说明**：原日志没有操作人，无法审计"谁做的"；新增 `operator`/`source` 两列，
  历史行保持 `operator=NULL`（前端显示「—」，不猜测、不回填）。
  迁移在 `database._migrate_control_log()` 中幂等执行。**日志中不得出现密码明文。**
- `config`：key-value。业务阈值用原名，运行期配置用 `sys.` 前缀（见 `store.py`）。
  仪表盘布局存 `sys.dashboard_layout`（JSON，全局共享一套；不建新表）。
  报警联动规则存 `sys.alarm_links`。**不再有执行器档案/通道声明**：`sys.actuators`、
  `sys.custom_channels` 是 2026-09-18 之前的遗留键，代码不读取，可随时清理。
- `custom_sensor_data`：自定义传感器通道数据（**由实时监控页的自定义接口卡派生**、
  后端轮询入库）。channel_id 即卡片 id，+ ts + value；历史曲线/数据统计/联动按通道查询。
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
| 40009 | 仪表盘布局版本冲突（布局已被其他窗口修改，需刷新页面后重试） |
| 40101 | 未登录或登录过期 |
| 40301 | 无权限 |

核心接口见 `README.md` 的「核心接口」表。

**可编辑仪表盘（实时监控页整体卡片化）**：实时监控页是一个 gridstack 卡片网格
（`frontend/js/widgets.js` 目录 + `components/widgetShell.js` 渲染），展示与操作全部卡片化，
可自由增删/拖拽/缩放（保存需 `cfg_system` 权限）。卡片 = JSON 配置
（type/source/cmd/thresholds/grid），type ∈ value/spark/line/state/tank/control/builtin。
**接口自由编辑（全卡直填 URL）**：value/spark/line/state/tank/control 六种卡的配置对话框
均有「接口 URL」一栏——留空读本机实时快照（source.kind=realtime），填了走自定义 GET 接口
（source.kind=custom，经 `/api/water/dashboard/proxy` 代发，**白名单防 SSRF**，见
`config.DASHBOARD_PROXY_HOSTS` / 环境变量 `WATER_DASHBOARD_PROXY_HOSTS`；非 JSON 返回也接受）。
**控制开关卡可改指令**：字段 `cmd:{on,off}` 为一对 GET 指令 URL，经代理下发（log=1 写入
操作日志 device.custom 供审计）；留空则用内置水泵/加热通道（API.pump/heater）。toggle 始终
需要 `ctrl_light` 权限与设备在线。**单水槽卡（tank）**为通用卡：数据源支持对象
（`tank.tanks.*`，带无传感器/离线角标与水量）或纯数值百分比（自定义接口），目录 `multi:true`
不判重、可加任意多个。builtin 特殊卡：quant（定量浇水）/ pid（恒温闭环）/ recent（最近操作）/
reset（清零累计）/ device（设备健康），逻辑自包含于 widgetShell（自行拉取数据、直调控制接口、
同一权限点、后端入日志），不提供接口编辑。目录项可选门控字段：`feature`（DEVICE_FEATURES 键）、
`perm`（Auth 权限点）、`flag`（realtime 顶层布尔）；无权限/离线时卡片渲染禁用态而非移除。
默认布局 14 行（1366×768 首屏零滚动）：流量+累计 / 双温+水压+光照 / 两张单水槽卡(w4h9) +
水泵/加热/定量卡；页头无快捷开关；**无循环回路系统**（原回路 SVG 组件已整体删除）。
布局读写/重置：`/api/water/dashboard/layout[/reset]`，全局共享一套，未保存时按设备能力生成
默认布局；loadLayout 按 builtin 去重防同类卡重复。
**布局并发保护与一键回退（2026-09-18 增补，事故驱动）**：布局是全局共享的一份 JSON，
任何窗口保存都会整体覆盖。曾发生真实事故：E2E 测试期间把布局临时改成 4 张测试卡，
另一个停留在旧布局的页面随后把这份测试布局保存回库，用户 14 张卡被整片覆盖。
现在的约定：
- **保存前自动备份**：`/water/dashboard/layout`（POST）与 `/reset` 都会先把当前布局存为
  `sys.dashboard_layout_prev`（+ `sys.dashboard_layout_prev_ts`），再写新值；`layout=None`
  不产生备份。
- **版本号并发保护**：`sys.dashboard_layout_rev` 每次写入 +1；`GET` 返回 `rev`，
  前端保存时回传 `base_rev`（= 自己载入时的 rev）。服务端发现 `base_rev != 当前 rev`
  返回 **40009** 并拒绝写入（前端提示"已被其他窗口修改"并自动重载最新布局）。
  不传 `base_rev` 表示不校验（向后兼容脚本/测试）。
- **一键回退**：`POST /api/water/dashboard/layout/restore_prev`（cfg_system）恢复上一次布局；
  恢复本身也会备份当前版，因此可反复点击在两版之间切换。前端入口：实时监控页
  编辑模式下的「恢复上一次」按钮（仅存在备份时显示）。
- **跑测试的前提**：E2E/接口测试会临时改写这份共享布局（开头快照、结束还原）。运行前必须
  确认没有别的浏览器停在实时监控页的「编辑卡片」模式（页面退出编辑会保存它手里的那份布局）；
  人工改动前可先点一次「恢复上一次」确认备份可用。E2E 恢复失败时不会再静默——它会在
  收尾用 API 回写快照并在日志里输出卡片数。

**报警联动（v3：卡片即来源）**：「实时监控页哪张卡片的阈值 → 触发哪张卡片的动作」，
规则自带阈值。**触发源与动作都直接选自实时监控页的卡片，没有独立档案/声明表**——
用户加一张卡就等于完成声明，不需要在系统配置页再填一遍 URL。
- 触发源：规则存 `source_card`（卡片 id）+ `source{kind,url,path,period}` 快照。
  系统配置页下拉列出**可读数值的卡片**（type ∈ value/spark/line/tank；水槽卡取 percent）。
  `kind="realtime"` 直接读采集快照路径；`kind="custom"`（自定义接口卡）由
  `custom_channels.CustomChannelManager` 轮询后并入 `data["custom"]`，卡片已不在布局中时
  由 `alarm.check_custom()` 按快照 URL 兜底轮询。URL 主机须在代理白名单。
- 动作：规则存 `{card, state:"on"|"off", kind, url}` 快照。下拉列出**可执行卡片**
  （type=control 或 builtin=quant）。执行时优先按卡片**当前**配置解析（改了卡片规则自动跟随），
  卡片不存在时回退快照：kind=url → 白名单 GET 下发；pump/heater → 本机水泵/加热；
  quant → 取消定量。
- 沿语义：读数越过阈值沿执行 actions 一次，回正常沿执行可选 `recover.actions`；
  状态去重（同方向不重复触发）。越限/恢复写 alarms 表（type=custom:<规则id>）。
- 存储/端点：规则 sys.alarm_links；系统配置页「报警联动」区编辑（cfg_alarm），
  `GET/POST /api/water/alarm/links`；保存即 reload_links 生效，写日志 config.link；
  动作写 control_log（action=link_card_*/link_pump_*/link_heater_*/link_quant_cancel，失败 link_fail，source=auto）。
  仪表盘布局保存/重置时会同时 reload 自定义通道与联动规则（api `_reload_card_consumers()`）。
- 注意：后端重启后越限状态归零，已越限规则会重新触发一次（安全动作，可接受）；
  heater 动作与 PID 无互锁（加热建议用 PID）；规则建议 ≤10 条（同步执行，单动作超时 1.5s）。

**传感器通道（卡片即声明）**：实时监控页加的卡片就是声明，后端按两种来源派生通道：
- **自定义接口卡**（source.kind=custom，含 URL/取值路径/轮询周期）→ 按周期 GET 该接口取值；
- **本机快照卡**（source.kind=realtime 的数值卡/曲线卡/单水槽卡）→ 若其取值路径**不属于**
  `water_sensors` 标准列（标准列：flow_rate/total_liters/storage_temp/heater_temp/pressure/
  light/pump_state/heater_state/pump_target，这些已逐秒入库），则按周期从本轮采集快照取样入库，
  通道 id 为 `card:<卡片id>`（于是「加一张卡＝历史/统计多一个指标」，如水槽卡 → 水位曲线）。
派生后的共同行为：
- 值有效 → 写入 realtime 快照（`realtime.custom.<卡片id>` 与 `realtime.custom_channels`
  元数据）并入库 `custom_sensor_data`（channel_id=卡片id）；
- 历史曲线页出现对应标签（`/api/water/custom/history`），统计页出现指标卡
  （`/api/water/custom/stats`），均按时间范围查询；
- 联动规则的触发源下拉会自动列出这张卡片（卡片删除后不再采集，规则回退快照兜底）。
- 端点：`GET /api/water/custom/history|stats`（view_history）；**声明类端点已删除**
  （不再有 `POST /api/water/custom/channels`、`/api/water/alarm/actuators`）。
- 无数据不入库不做模拟；轮询在采集循环内同步执行（自定义卡建议 ≤10 张）。

**历史曲线内置标签**：瞬时流量 / 累计水量 / 储水槽温度 / 加热槽温度 / 水压 / 光照
（数值型），以及 **水泵状态 / 加热状态 / 定量目标**（状态型：`on/off` 在前端映射为 1/0，
`unknown` 或空值断开曲线——便于"什么时候开过、开了多久"）；派生通道（含 `card:<id>` 水位等）
自动追加为标签。数据来自 `water_sensors` 的对应列，不额外存储。
**标签高亮只允许一个**：内置标签与"自定义/派生通道"标签比较的是**同一个唯一键 `activeHistKey`**
（自定义通道优先，形式 `c:<通道id>`），因此点自定义通道后内置标签会自动取消选中。
新增标签时必须沿用 `activeHistKey` 比较，不要再单独用 `histType`/`histCustomId` 判高亮
（曾出现"内置标签 + 自定义通道各亮一个"的问题）；通道类型标签行类名为 `.hist-type-tabs`
（E2E 据此做互斥断言，避免与时间范围标签行混淆）。

**恒温闭环多路回路（v2：卡片即来源）**：**一张「恒温闭环」卡 = 一路独立 PID**，可多路并存（多水槽各自控温）。
- 卡片字段 `pid`：`{sensor_card, sensor_source{kind,url,path,period}, actuator_card, actuator{kind,url_on,url_off},
  guard_card, guard_source, guard_min, target, kp, ki, kd, enabled}`。
  来源卡必须是数值类卡片（value/spark/line/tank）；执行器卡是控制开关卡（内置加热/水泵卡 → 本机通道，
  带自定义开/关指令 URL 的卡 → 走白名单 GET）；`guard_card` 为可选的防干烧联锁（如液位卡 ≥ 阈值）。
- 后端 `pid_loops.PidLoopManager` 从布局派生回路：每路一份独立 PID 状态（积分/上次误差/上次占空比）；
  温度无有效读数 → 跳过本轮；联锁不满足 → 强制断开加热并给出原因；自定义指令型执行器按"本路上次下发状态"去抖
  （没有回读，不能像内置通道那样比对设备状态，否则会每周期重复发指令）。
- **未配置来源/执行器的老式闭环卡 = 旧版默认回路**（加热槽温度 → 本机加热，参数取 `sys.pid_*`），
  旧接口 `/water/pid`、`/water/pid/mode`、`/water/target` 保持不变；新增
  `GET /water/pid/loops`（view_monitor，回各路状态）与 `POST /water/pid/loops`（cfg_alarm，改某路 target/kp/ki/kd/enabled，
  **写回卡片配置**并写 `config.pid` 日志）。
- ⚠️ 卡片新增字段必须同步加入 `waterDashboard.serialize()` 的白名单，否则保存布局时会被丢弃
  （闭环卡 `pid` 配置曾因此整块丢失，表现为"保存后回路变成默认回路、甚至去控本机加热"）。

**真实数据原则**：设备不支持或离线时，接口返回 `None` 与 `features` 能力表；
水位类字段按用户要求**无数据一律为 0**，但必须同时带 `source="none"` 或离线标志。
**绝不生成任何模拟/演示值**；控制指令失败必须返回 40003，不得伪造成功。

**现场适配能力（2026-09-18 增补，竞赛现场未知题目的兜底）**：
- **CSV 导出**：`GET /api/water/export.csv?kind=sensors|alarms|logs|custom&start&end&channel_id&category&limit`，
  带 UTF-8 BOM（Excel 直接打开不乱码），按类型校验 view_history / view_alarm / view_log 权限；
  前端历史、告警、日志三页各有「导出 CSV」按钮。
- **提交包**：`scripts/make_submission.ps1` 生成「技能赛提交文件」文件夹（源码+文档+数据导出+提交说明），
  自动排除 .venv/.runtime/__pycache__/.git；脚本为 UTF-8 BOM 以避免 Windows PowerShell 5.1 乱码。
- **组合条件与持续判定**（报警联动 v4）：规则可带 `extra[]`（≤5 条附加条件，全部满足才算越限）与
  `hold`（连续满足 N 秒才触发，0=立即）；任一条件读数缺失则本轮跳过、不改状态。
- **定时任务**：`GET/POST /api/water/timers`（cfg_alarm），mode=interval（every_sec≥5）或 daily（at HH:MM），
  动作与联动同构（卡片）；到点执行写 `timer_*` 日志、`source="timer"`。
- **通道标定**：`GET/POST /api/water/calibration`、`POST /api/water/calibration/two_point`（cfg_system）；
  工程值 = 原始值 × scale + offset，应用于本机通道（water_device 读取后）与自定义通道（custom:<卡片id>）；
  未标定（1/0）的通道不写入配置，保持零开销。
- **外部设备接入**：`GET/POST /api/water/gateway`、`GET /api/water/gateway/test?id=`、`GET /api/water/gateway/coil?id=&addr=&state=on|off`；
  模式 `tcp`（Modbus TCP/MBAP）/ `rtu_tcp`（串口服务器透明传输）/ `rtu_serial`（需 pyserial）；
  采集项 `{key,func,addr,count,type,scale,offset,target}`，target 为本机通道键或 `custom:<卡片id>`（会入库，历史/统计/联动可用）；
  读数**覆盖同名本机通道**（现场接的就是它），失败在 `/api/water/gateway` 的 errors 里给原因；线圈写入可直接作为控制卡指令 URL。
- **判定服务模板化**：`GET/POST /api/water/judge/template`（url + headers + report/poll/feedback 三段 {path, body}），
  body 内 `{{字段}}` 用实时快照替换、`{{data_json}}` 展开整份快照；未配置段落回退内置报文；
  地址优先取 `sys.judge_url`，其次 config.JUDGE_URL。
- **历史回放**：`GET /api/water/replay?ts=`（按时间点取快照，含各自定义通道该时刻最近值）与
  `GET /api/water/replay/frames?start&end&limit=`（等距降采样帧）；前端历史页回放条支持滑块与播放。
  **生长式回放 + 曲线联动**（2026-09-18 增强）：曲线只画到当前回放时刻（已回放段实线，
  其后的"未来"段用同色半透明幽灵线保留整段轮廓，透明度 0.18；幽灵序列不进图例、不参与 tooltip；
  非回放态时幽灵线透明度 0 —— 注意 ECharts 增量 setOption 不能新增序列，幽灵序列必须在首次渲染时成对创建）；
  播放/拖动时曲线随之生长，同时曲线上有**回放游标**（ECharts markLine，
 吸附到最近的曲线点，标签显示当前时刻），并**视口跟随**——缩放窗口固定为总点数约 15%
  （最少 10 点），游标移出窗口才滚动（在窗口内不动，避免频繁重绘打断观察）；
  切换时间范围/通道类型时游标与视口状态重置。曲线单序列视图也带 dataZoom，否则无法跟随。
  游标位置通过 `window.Charts.get(id).getOption().series[0].markLine` 可读取（E2E 据此断言）。
- **历史取样与统计口径（2026-09-18 修正）**：
  - 图表接口 `/water/history`、`/water/custom/history` 的 `limit` 是**图上最大点数**：
    区间再长也覆盖到最新数据，超出时**等距降采样**（保留首尾、末点必为区间最新一条），
    并返回 `raw_count`（区间原始行数）与 `sampled`（是否降采样）；前端显示「已降采样」提示。
    **禁止再用 `ORDER BY ts ASC LIMIT`**（那样会丢弃最新数据，长区间曲线会停在几小时前）。
  - 回放 `/water/replay/frames` 同样覆盖整区间并返回 `frames/raw_count/sampled`。
  - CSV 导出上限 20 万条（1Hz 下约 55 小时），超限时保留**最新**数据；响应头
    `X-Exported-Rows` / `X-Total-Rows` 供前端提示是否被截断。
  - 统计 `/water/stats`：`samples` 含设备离线期间写入的空行，`valid_samples.{flow,storage_temp,
    heater_temp,pressure,light}` 才是各通道有效读数点；`avg_flow_weighted` 为按持续时间加权的
    平均流量（采样间隔不均时比 `avg_flow` 准确），间隔 >60s 的离线空档不计入。
- **拓扑卡与场景卡**（前端 builtin）：`topo` 系统拓扑（节点状态绑定实时值、有流量时管路流动动画、
  无传感器槽位标注）；`scene` 一键场景（启动循环＝开泵；急停＝关泵/关加热/取消定量 + 依次关闭布局内所有
  配了自定义指令的控制卡），两者均二次确认并写操作日志。

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
- [ ] 实时监控页为全卡片网格（gridstack），展示卡与操作卡（水泵/加热/定量/PID/最近操作/清零）均可增删/拖拽/缩放；页头无快捷开关
- [ ] 编辑模式可拖拽/缩放/删除/添加内置卡/添加自定义接口卡（全类型可添加，预览取值），保存后刷新布局仍在；代理拒绝白名单外地址
- [ ] 卡片接口可编辑：显示卡配置里 URL 留空读实时快照、填了走代理轮询；控制卡可配自定义开/关指令 URL（成对校验、代理下发、写入 device.custom 日志）
- [ ] 通用单水槽卡默认两张（储水槽/加热槽）左右并排，无传感器槽位标注「无传感器」、离线标注「离线」，水位与后端一致；无循环回路系统
- [ ] 清零累计卡不在默认布局（危险操作降权），从目录添加后需二次确认且权限受控；未接传感器的槽位标注「无传感器」、离线时标注「离线」
- [ ] 报警联动可在系统配置页编辑，**触发源与动作都从实时监控页的卡片里选**（内置卡/后加的自定义卡），保存即生效并写入 config.link 日志；越限沿执行 link_* 动作日志，恢复沿执行可选恢复动作
- [ ] 自定义传感器通道：实时监控页加「自定义接口」卡即完成声明，realtime 快照含读数、历史曲线/统计页出现对应标签、联动触发源可选；删卡即停止采集；越限/恢复写告警记录（custom:<规则id>）
- [ ] 系统配置页不再出现「执行器档案」「自定义传感器通道」区块，后端无 `/api/water/alarm/actuators`、`/api/water/custom/channels` 端点
- [ ] CSV 导出：历史/告警/日志三页各有「导出 CSV」，文件带 UTF-8 BOM 与中文表头，按类型校验权限（`/api/water/export.csv`）
- [ ] 提交包脚本：`scripts/make_submission.ps1` 能在目标位置生成「技能赛提交文件」（源码+文档+数据导出+提交说明），不删除目标已有内容
- [ ] 报警联动支持组合条件与持续判定：`extra[]` 全部满足 + `hold` 连续 N 秒才触发；读数缺失时跳过不改状态
- [ ] 定时任务：interval/daily 两种模式可保存回读，到点执行卡片动作并写 `timer_*` 日志（source=timer）
- [ ] 通道标定：两点标定算出 scale/offset 并生效，工程值进入实时快照/入库/告警；未标定通道不受影响
- [ ] 外部设备接入：Modbus TCP 与串口服务器(RTU over TCP) 两种模式均可配置并试读，线圈写入可作为控制卡指令
- [ ] 判定服务报文模板：可配置地址/请求头/三段报文，未配置段落回退内置报文，非法地址或路径被拒 40002
- [ ] 历史回放：按时间点取快照 + 帧序列，前端滑块/播放可用
- [ ] 拓扑卡与场景卡：topo 节点状态随实时值变化；scene 急停可一键关闭本机执行器与全部自定义执行器（均二次确认并写日志）
- [ ] 恒温闭环多路：一张闭环卡 = 一路独立 PID（来源卡 + 执行器卡 + 可选防干烧联锁卡），参数写回卡片配置；未配置的老式闭环卡仍按默认回路（加热槽温度→本机加热）运行
- [ ] 闭环安全：温度无有效读数时跳过本轮；联锁不满足时强制断开加热并给出原因；自定义指令型执行器不重复下发
- [ ] 布局并发保护：保存时回传 `base_rev`，版本过期返回 40009（不传 base_rev 仍兼容）；保存/重置前自动备份上一版
- [ ] 布局一键回退：`POST /api/water/dashboard/layout/restore_prev` 可恢复上一版、且可反复点击在两版间切换；界面编辑模式提供「恢复上一次」按钮
- [ ] 全站无模拟数据：无数据的水位恒显示 0，设备离线时两槽水位归 0 并提示离线
- [ ] 操作日志记录操作人（人工为账号名，自动动作为「系统 · 恒温闭环 / 判定服务」），支持分类筛选
- [ ] 系统配置与账号管理类操作均已入日志，且不含密码明文
- [ ] `python backend/tests/test_api.py` 全部通过（默认不执行开泵与清零）
- [ ] `python backend/tests/test_level.py` 全部通过
- [ ] `node tests/e2e_web.js` 全部通过（真实浏览器，默认不点水泵开关、不清零）
- [ ] 前端页面无控制台报错，实时数据 ≤2s 刷新
