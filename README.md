# 生物样本库知情同意与撤回

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8302`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8302
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `participant`：参与者；`consent`：同意版本；`sample`：样本；`withdrawal`：撤回申请。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/locations`：当前在占库位清单（格位、样本、占用时间）。
- `GET /api/entities/<id>/locations`：单个样本的历次库位变化（占用/释放时间）。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 库位占用与移库

- 样本的`store`（入库）、`relocate`（移库）、`destroy`（销毁）由规则层声明库位效果，服务层在单个SQLite事务内编排：实体版本更新、旧位释放、新位占用、审计记录一起提交，失败一起回滚。
- 一个冻存格位同一时刻只允许一个在库样本：规则层预判，`location_occupancy`表上的部分唯一索引（`released_at IS NULL`）兜底并发。两个请求抢同一格时只有一人成功，落败方返回`409 ConflictError`并说明原位置保持不变。
- 目标位置已被占用时不移库：样本停留在原库位，冲突说明中给出占用样本与前后位置。
- 移库记录保留前/后位置、占用时间与释放时间；`GET /api/entities/<id>/locations`和演示页面可查看当前库位与历次变化。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

样本销毁和外部机构调用是流程演示，不会自动删除真实存储中的样本。
