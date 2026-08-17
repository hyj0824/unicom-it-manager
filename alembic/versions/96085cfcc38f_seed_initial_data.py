"""seed initial data

预置字典分类/字典项、权限与五个岗位角色（见 docs/data-model-baseline.md、
docs/permission-workflow-baseline.md）。只建表与字典，不导入台账数据。

Revision ID: 96085cfcc38f
Revises: b4127c1a2567
Create Date: 2026-08-17 13:53:38.756090
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '96085cfcc38f'
down_revision: Union[str, Sequence[str], None] = 'b4127c1a2567'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_dictionary_categories = sa.table(
    "dictionary_categories",
    sa.column("id", sa.Integer),
    sa.column("code", sa.String),
    sa.column("label", sa.String),
    sa.column("sort_order", sa.Integer),
    sa.column("is_active", sa.Boolean),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)

_dictionary_items = sa.table(
    "dictionary_items",
    sa.column("id", sa.Integer),
    sa.column("category_id", sa.Integer),
    sa.column("code", sa.String),
    sa.column("label", sa.String),
    sa.column("sort_order", sa.Integer),
    sa.column("is_active", sa.Boolean),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)

_permissions = sa.table(
    "permissions",
    sa.column("id", sa.Integer),
    sa.column("code", sa.String),
    sa.column("description", sa.String),
)

_roles = sa.table(
    "roles",
    sa.column("id", sa.Integer),
    sa.column("code", sa.String),
    sa.column("name", sa.String),
    sa.column("description", sa.String),
    sa.column("is_preset", sa.Boolean),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)

_role_permissions = sa.table(
    "role_permissions",
    sa.column("role_id", sa.Integer),
    sa.column("permission_id", sa.Integer),
    sa.column("domain", sa.String),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _categories() -> list[dict]:
    return [
        {"id": 1, "code": "county", "label": "县分"},
        {"id": 2, "code": "grid", "label": "网格"},
        {"id": 3, "code": "business_type", "label": "业务类型"},
        {"id": 4, "code": "service_status", "label": "服务状态"},
        {"id": 5, "code": "asset_class", "label": "设备属性"},
        {"id": 6, "code": "device_type", "label": "设备类型"},
        {"id": 7, "code": "recovery_status", "label": "回收状态"},
        {"id": 8, "code": "recovery_reason", "label": "回收原因"},
        {"id": 9, "code": "contact_duty", "label": "联系人职责"},
        {"id": 10, "code": "data_quality_status", "label": "数据质量状态"},
    ]


def _items() -> list[dict]:
    """字典项按 0811 台账的实际取值预置（频率降序）；异常值如“其它”、
    混入县分列的 BD 名称不入字典，导入时标记为语义异常。"""

    county = [
        ("shiquan", "石泉"),
        ("hanbin", "汉滨"),
        ("ziyang", "紫阳"),
        ("langao", "岚皋"),
        ("xunyang", "旬阳"),
        ("baihe", "白河"),
        ("ningshan", "宁陕"),
        ("hanyin", "汉阴"),
        ("pingli", "平利"),
        ("zhenping", "镇坪"),
    ]
    grid = [
        "军民融合BU", "石泉政企", "政要行业BU", "紫阳要客", "岚皋政企",
        "教育行业BU", "汉滨要客", "旬阳要客", "政务行业BU", "医疗行业BU",
        "白河政企", "紫阳商企", "高新政企", "宁陕政企", "数字政府兼工业互联网BU",
        "汉滨商企", "汉阴政企", "旬阳商企", "平利政企", "镇坪政企", "恒口政企",
        "汉滨南环路综合网格", "汉滨巴山路综合网格", "紫阳城区综合网格",
        "紫阳向阳综合网格", "汉滨进站路综合网格", "白河城区综合网格",
        "汉滨五里综合网格", "岚皋农村综合网格", "高新综合网格", "石泉城区综合网格",
        "紫阳蒿坪综合网格", "汉阴农村综合网格", "汉阴城区综合网格",
        "旬阳城区综合网格", "恒口综合网格", "岚皋城区综合网格", "宁陕综合网格",
        "石泉农村综合网格",
    ]
    business_type = [
        ("data_and_network_element", "数据及网元业务"),
        ("broadband", "宽带业务"),
    ]
    service_status = [
        ("normal_active", "正常开机"),
        ("voluntary_termination_requested", "主动退网(申请拆机)"),
    ]
    asset_class = [("asset", "资产类"), ("cost", "成本类")]
    recovery_status = [("recovered", "已回收"), ("not_recovered", "未回收")]
    recovery_reason = [
        ("in_use", "在用"),
        ("lost", "遗失"),
        ("dispute", "纠纷"),
        ("other", "其他"),
    ]
    contact_duty = [
        ("developer", "发展人"),
        ("account_manager", "客户经理"),
        ("network_maintenance", "网络维护责任人"),
    ]
    data_quality_status = [
        ("ok", "完整"),
        ("missing", "缺项"),
        ("invalid", "语义异常"),
        ("conflict", "冲突"),
    ]

    items: list[dict] = []
    item_id = 1
    for category_id, groups in [
        (1, county),
        (3, business_type),
        (4, service_status),
        (5, asset_class),
        (7, recovery_status),
        (8, recovery_reason),
        (9, contact_duty),
        (10, data_quality_status),
    ]:
        for sort_order, (code, label) in enumerate(groups, start=1):
            items.append(
                {
                    "id": item_id,
                    "category_id": category_id,
                    "code": code,
                    "label": label,
                    "sort_order": sort_order,
                }
            )
            item_id += 1
    for sort_order, label in enumerate(grid, start=1):
        items.append(
            {
                "id": item_id,
                "category_id": 2,
                "code": None,
                "label": label,
                "sort_order": sort_order,
            }
        )
        item_id += 1
    return items


def _permission_rows() -> list[dict]:
    codes = [
        ("read", "查看数据"),
        ("create_draft", "创建草稿/新增申请"),
        ("update_draft", "修改草稿/变更申请"),
        ("delete_draft", "删除草稿"),
        ("submit", "提交审核"),
        ("review", "审核（通过/退回/驳回）"),
        ("apply", "应用变更到正式数据"),
        ("import", "导入 Excel 到暂存区"),
        ("export", "导出数据"),
        ("call_now", "立即拨打"),
        ("manage_users", "用户与角色管理"),
        ("manage_config", "字典与系统配置管理"),
    ]
    return [
        {"id": i, "code": code, "description": description}
        for i, (code, description) in enumerate(codes, start=1)
    ]


def _role_rows() -> list[dict]:
    return [
        {
            "id": 1,
            "code": "business_maintainer",
            "name": "业务维护人",
            "description": "查询、草稿、新增/修改/作废申请、提交",
        },
        {
            "id": 2,
            "code": "business_auditor",
            "name": "业务稽核人",
            "description": "查询、审核、退回、应用",
        },
        {
            "id": 3,
            "code": "network_maintainer",
            "name": "网络维护人",
            "description": "查询、草稿、新增/修改/作废申请、提交",
        },
        {
            "id": 4,
            "code": "network_auditor",
            "name": "网络稽核人",
            "description": "查询、审核、退回、应用",
        },
        {
            "id": 5,
            "code": "system_admin",
            "name": "系统管理员",
            "description": "用户、角色、字典、配置、导入导出、审计查看",
        },
    ]


def _role_permission_rows() -> list[dict]:
    permission_codes = {
        row["code"]: row["id"] for row in _permission_rows()
    }
    rows: list[dict] = []
    for role_id, domain, codes in [
        (1, "business", ["read", "create_draft", "update_draft", "delete_draft", "submit"]),
        (2, "business", ["read", "review", "apply"]),
        (3, "network", ["read", "create_draft", "update_draft", "delete_draft", "submit"]),
        (4, "network", ["read", "review", "apply"]),
        (5, "system", ["read", "manage_users", "manage_config", "import", "export"]),
    ]:
        for code in codes:
            rows.append(
                {
                    "role_id": role_id,
                    "permission_id": permission_codes[code],
                    "domain": domain,
                }
            )
    return rows


def upgrade() -> None:
    """Upgrade schema."""
    now = _now()
    op.bulk_insert(
        _dictionary_categories,
        [
            {**row, "sort_order": row["id"], "is_active": True,
             "created_at": now, "updated_at": now}
            for row in _categories()
        ],
    )
    op.bulk_insert(
        _dictionary_items,
        [
            {**row, "is_active": True, "created_at": now, "updated_at": now}
            for row in _items()
        ],
    )
    op.bulk_insert(_permissions, _permission_rows())
    op.bulk_insert(
        _roles,
        [
            {**row, "is_preset": True, "created_at": now, "updated_at": now}
            for row in _role_rows()
        ],
    )
    op.bulk_insert(_role_permissions, _role_permission_rows())


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(_role_permissions.delete())
    op.execute(_roles.delete())
    op.execute(_permissions.delete())
    op.execute(_dictionary_items.delete())
    op.execute(_dictionary_categories.delete())
