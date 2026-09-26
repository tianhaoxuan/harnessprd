"""api —— 路由层。

- router.py  聚合各子路由，由 main.py 挂到 /api/v1
- health.py  健康检查
- prd.py     PRD 生成 / 澄清
- deps.py    依赖注入（配置、service）

本层只做三件事：校验入参、调用 services、把异常翻译成 HTTP 状态码。
业务逻辑不写在这里。
"""
