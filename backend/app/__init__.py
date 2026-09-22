# SPDX-License-Identifier: GPL-3.0-or-later
"""万级智能体模拟问卷平台 —— 后端应用包。

模块边界（主文档 §3 / architecture.md §1）：
    contracts      请求/响应与内部契约（Pydantic v2，无业务依赖）
    config         环境配置与无秘密模型配置
    db             异步会话/事务/连接池
    models         五张业务表、索引、约束
    personas       用户表导入、列映射与逐行快照（T1）
    surveys        问卷草稿与版本（T1）
    inference      提示词模板 / 第三方适配 / 严格校验（T2）
    runs           批次创建、幂等、控制状态（T3/T4）
    worker         单 worker 锁与调度循环（T3）
    reports        SQL 聚合与流式导出（T5）
    api            路由绑定（T4）
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
