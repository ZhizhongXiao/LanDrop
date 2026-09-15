# 测试目录

第二阶段开始使用 Python 标准库 `unittest` 覆盖文件安全、凭据存储和 Bottle 路由。

运行：

```powershell
python -m unittest discover -s tests -v
```

测试只使用临时目录和进程内 WSGI 请求，不监听局域网端口。
