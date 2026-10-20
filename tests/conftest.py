"""测试全局前置：必须在任何 app 模块被导入前把 DSN 指向测试库。

原因：`app.config` 在导入时就把 PG_DSN 算好了。
如果等到 fixture 里才设环境变量，其他测试模块早已触发 import，改不动了。
"""
from __future__ import annotations

import os

_test_dsn = os.environ.get("SELFDESK_TEST_DSN")
if _test_dsn:
    os.environ["SELFDESK_PG_DSN"] = _test_dsn
