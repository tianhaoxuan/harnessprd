# Postgres 初始化脚本目录

此目录被挂载到容器的 `/docker-entrypoint-initdb.d`。

- 只在**数据卷首次创建**时执行一次；已有数据卷时改动无效（需 `docker compose down -v` 重置）。
- 会按文件名顺序执行 `*.sql`、`*.sql.gz`、`*.sh`。
- 本 README 不会被 Docker 的 entrypoint 执行，仅作说明。

约定：扩展安装、schema 创建等首启动作放这里；业务表结构走应用侧迁移（migrations），不要写在这里。
