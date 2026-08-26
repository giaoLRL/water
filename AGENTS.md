# AGENTS.md — 基于物联网的分布式智慧灯杆监控系统 · AI 编程约束文档

> 本文件是该项目对 AI 编程助手的行为约束与实现规范。
> 任何 AI 在本仓库执行修改前，必须先完整阅读本文件；用户最新指令与本文件冲突时，以用户最新指令为准，并同步更新本文件。

## 1. 项目背景与目标

- 赛题：基于物联网的分布式智慧灯杆监控系统。
- 系统由多个分散的智慧灯杆组成，每个灯杆作为独立监控单元，部署传感器（温湿度、光照）、执行器（电磁阀控制灯光总开关）与摄像头。
- 功能：环境参数实时监测、异常报警、视频监控、人员智能监测、数据存储与查询、远程设备控制。
- 评分：信息获取/显示/记录/查询 60 分；灯杆附近人员智能监测与管理 40 分。

## 2. 已确认的架构与技术栈（锁定决策）

未经用户明确同意不得更改：

| 层次 | 选型 | 备注 |
| --- | --- | --- |
| 前端 | Vue 3（Composition API）+ ECharts | 本地引库、无构建步骤 |
| 后端 | Python 3.11+ FastAPI + Uvicorn | 自带 `/docs` 便于验证接口 |
| 数据库 | MySQL 8.x（PyMySQL 连接池） | 库名 `iot_system`，字符集 `utf8mb4` |
| 视频流 | OpenCV + MJPEG（`multipart/x-mixed-replace`） | 有 RTSP 读真实流，否则渲染模拟画面 |
| 人员识别 | HTTP 调用智能识别接口 `POST /infer` | `{"image": "data:image/...base64"}`，返回 `inference_results` 与 `processed_image` |
| 前后端通信 | HTTP + JSON | 实时数据 2s 轮询，不引入 WebSocket |
| 前端页面结构 | 灯杆列表导航 + 灯杆详情（实时监控 / 历史数据 / 告警记录 / 人员监测 / 操作日志） | PC / 手机自适应 |

## 3. 目录结构约定

```text
frontend/
├── index.html
├── css/style.css
├── js/
│   ├── api.js              # 接口封装
│   ├── charts.js           # ECharts 辅助
│   ├── components/         # lampList.js / lampDetail.js
│   ├── app.js              # 两级导航与轮询
│   └── lib/                # vue / echarts（本地文件）
backend/
├── main.py                 # FastAPI 入口，采集循环
├── config.py               # 灯杆列表、阈值、识别接口等配置
├── database.py             # MySQL 封装（连接池 + 建表）
├── device.py               # 灯杆设备（传感器模拟 + 视频流）
├── alarm.py                # 告警引擎
├── api.py                  # HTTP 业务接口
├── state.py                # 全局运行时状态
└── requirements.txt
```

## 4. 数据模型（MySQL）

库 `iot_system`，utf8mb4，InnoDB：

- `lamp_sensors`：`lamp_id`、`ts`、`temperature`(℃)、`humidity`(%)、`luminance`(lx)、`light_state`；建立 `(lamp_id, ts)` 索引。
- `alarms`：时间、类型、数值、阈值、方向、状态。
- `person_detections`：时间、灯杆、人数、最高置信度、原始/标注图（base64）。
- `control_log`：设备操作日志。
- `config`：告警阈值 key-value。

时间戳统一 `yyyy-MM-dd HH:mm:ss`；变更表结构须说明原因并给出迁移方案。

## 5. 接口规范（契约）

统一返回：`{"code": 0, "msg": "ok", "data": {...}}`。

| 错误码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 40002 | 参数错误 |
| 40003 | 控制指令执行失败 |
| 40004 | 灯杆不存在 / 记录不存在 |
| 40005 | 视频流暂无画面 |
| 50000 | 智能识别服务调用失败 |

核心接口见 `README.md` 的"核心接口"表。智能识别接口严格按照赛题约定（`http://127.0.0.1:5000/infer`，POST，`image` base64 前缀 `data:image/png;base64,`）。

## 6. AI 编码行为准则

1. **先读后改**：修改任何文件前先读取其当前内容。
2. **锁定决策优先**：第 2 节为锁定项，不得自行替换；确需变更先征得用户同意。
3. **范围克制**：只实现用户要求的改动，不做超出范围的顺手重构。
4. **离线优先**：不得引入 CDN 依赖；新增 Python/JS 依赖最小化并同步 `requirements.txt`。
5. **命名与注释**：标识符英文，注释中文。
6. **避免破坏性操作**：不执行 `rm -rf`、`git reset --hard` 等破坏性命令。
7. **验证后再交付**：每次改动完成后运行相应验证并在回复中说明。

## 7. 验证清单

- [ ] `python main.py` 启动无报错，`/docs` 可访问
- [ ] `/api/lampposts` 返回 ≥3 个灯杆，字段完整
- [ ] 传感数据周期入库，`/history` 按时间范围可查
- [ ] 控制接口真实改变灯光状态并由实时接口回显
- [ ] `/detect` 调用识别服务成功返回并入库
- [ ] `python backend/tests/test_api.py` 全部通过
- [ ] 前端页面无控制台报错，实时数据 ≤2s 刷新