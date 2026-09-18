# SeatBond

影院连座锁座：按场次厅图查找连续空座，过道列断开，冲突检测既有持座。

## 启动

```bash
docker compose up --build
```

| 服务 | 地址 |
| --- | --- |
| 前端 | http://localhost:4100 |
| API | http://localhost:9100 |
| API 文档 | http://localhost:9100/docs |
| Postgres | localhost:5442 |

健康检查：`GET http://localhost:9100/api/health`

## 页面

- `/halls` — 影厅
- `/showtimes` — 场次
- `/seatmap` — 座位图（大网格热力）
- `/hold` — 锁座
- `/orders` — 订单
- `/conflicts` — 冲突

## 使用说明

1. 在影厅与场次页确认厅图与排期。
2. 打开座位图查看占用热力，在锁座页输入连座人数并提交。
3. 订单页查看持座结果；冲突页查看重叠请求。

## 同场次并发互斥

针对同一 `showtime_id` 的锁座请求按以下方式保证「至多一笔成功」：

- 每笔请求先按当前快照算出意向连座区间，随后在单事务内对该场次行执行
  `SELECT ... FOR UPDATE` 取**场次级行锁**；并发请求在锁上串行化。
- 持锁后重新读取最新持座并校验意向区间：无重叠 → 插入持座并提交（成功方）；
  重叠 → 向 `conflict_logs` 写入被拒记录（场次、人数、被拒座位区间、原因）并提交，
  返回结构化 `409`（失败方）。失败提示明确标注「非网络问题」，可与网络异常区分。
- `seat_holds` 上的唯一约束 `uq_hold_span` 作为第二道兜底；锁等待超时/约束冲突同样
  落冲突日志并返回 409。
- 失败方重试时，自动搜座与座位图只体现成功方占用，会改选下一段连续空座；
  过道列打断与最左搜座语义不变。

冲突页（`/conflicts`）展示每次被拒：类型（座位冲突 / 无连续空座）、场次片名与影厅、
被拒座位区间、人数、原因。

## 开发与测试

```bash
docker compose exec api pytest -q
```

并发互斥测试（`tests/test_hold_concurrency.py`）需要真实 PostgreSQL（行锁无 SQLite
等价实现）：通过线程屏障让多笔请求在同一快照算出相同意向区间后同时抢锁，确定性地验证
「一胜 N-1 负」、成功方坐标正确、失败方有冲突记录、持座条数不膨胀；另含经 uvicorn
的真实 HTTP 全链路并发用例。设置 `DATABASE_URL` 指向可达的 Postgres 即可运行，
不可达时这些用例自动跳过。

