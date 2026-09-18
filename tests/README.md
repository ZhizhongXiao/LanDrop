# 测试目录

使用 Python 标准库 `unittest` 覆盖文件安全、凭据存储、Bottle 路由、桌面服务控制、临时会话生命周期、HTTP Range 逻辑任务聚合、隐私化日志轮转、托盘/Toast 统一协调、旧通知隔离和并发原子动作，以及第六阶段的 endpoint 冻结与二次确认、Public 启动拒绝、异步深度诊断、防火墙证据分类、网络变化通知顺序、显式接口选择和原始流上传边界。当前共有 65 项测试。

运行：

```powershell
python -m unittest discover -s tests -v
```

测试只使用临时目录、进程内 WSGI 请求和模拟服务器，不监听局域网端口，也不打开 GUI。
