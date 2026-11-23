"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, snapshot_migration

# snapshot_migration 是固定前缀路由，必须先于 resource 的 /api/{module} 动态路由注册，
# 否则 /api/snapshot-migration/status 这类两段路径会被 {module}/{entry_id} 抢先匹配。
ROUTERS = [snapshot_migration, resource]
