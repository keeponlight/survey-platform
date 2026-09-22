-- 首启时创建测试库（与开发库 survey 分离，主文档 T1 要求）。
-- 仅在 postgres 数据卷为空时执行一次。
CREATE DATABASE survey_test;
