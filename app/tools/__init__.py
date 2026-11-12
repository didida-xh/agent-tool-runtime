"""工具包入口。

注册机制约定：registry.py 不 import 任何工具模块（防循环导入），
由本文件把四个工具模块 import 进来，触发 @tool 装饰器完成注册。
Day1 手敲 tools 时把下面几行补上：

    from app.tools import weather, order, refund  # noqa: F401
"""
