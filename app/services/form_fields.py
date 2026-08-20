"""表单字段单一事实源：导入更正表单与台账/设备弹窗共用。

字段以「业务域 / 设备域」分组，key 是弹窗表单的提交字段名，column 是
导入更正表单的中文列名（与 app/services/imports.py 的 LEDGER_COLUMNS
一致）。两处表单的字段集必须保持对齐：修改本清单时同时检查
app/templates/ledger.html、app/templates/devices.html 与
app/templates/import_detail.html 的渲染循环。
"""

from __future__ import annotations

BUSINESS_FIELDS: list[dict[str, object]] = [
    {"key": "service_number", "column": "号码", "label": "业务号码", "required": True},
    {"key": "customer_name", "column": "户名", "label": "客户名称", "required": True},
    {"key": "county", "column": "县分", "label": "县分", "datalist": "county-options"},
    {"key": "grid", "column": "网格", "label": "网格", "datalist": "grid-options"},
    {"key": "service_status", "column": "服务状态", "label": "服务状态", "datalist": "service-status-options"},
    {"key": "accessed_at", "column": "入网时间", "label": "入网时间", "type": "date"},
    {"key": "agreement_expires_at", "column": "协议到期时间", "label": "协议到期时间", "type": "date"},
    {"key": "business_type", "column": "业务类型", "label": "业务类型", "datalist": "business-type-options"},
    {"key": "channel_name", "column": "渠道名称", "label": "渠道名称", "datalist": "channel-options"},
    {"key": "developer_name", "column": "发展人", "label": "发展人"},
    {"key": "developer_phone", "column": "发展人联系电话", "label": "发展人电话", "inputmode": "numeric"},
    {"key": "account_manager_name", "column": "客户经理", "label": "客户经理"},
    {"key": "account_manager_phone", "column": "客户经理联系电话", "label": "客户经理电话", "inputmode": "numeric"},
]

DEVICE_FIELDS: list[dict[str, object]] = [
    {"key": "device_code", "column": "设备编码", "label": "设备编码", "required": True, "datalist": "device-code-options"},
    {"key": "maintenance_name", "column": "网络维护责任人", "label": "维护责任人"},
    {"key": "maintenance_phone", "column": "网络维护责任人联系电话", "label": "维护责任人电话", "inputmode": "numeric"},
    {"key": "asset_class", "column": "设备属性", "label": "资产类别", "datalist": "asset-class-options"},
    {"key": "asset_value", "column": "资产原值或物资购置价格", "label": "资产原值/购置价格", "type": "number"},
    {"key": "device_type", "column": "设备及物资类型", "label": "设备类型", "datalist": "device-type-options"},
    {"key": "vendor_model", "column": "设备厂家+型号", "label": "设备厂家+型号", "datalist": "vendor-model-options"},
    {"key": "location", "column": "设备放置地点", "label": "设备放置地点", "datalist": "device-location-options"},
    {"key": "recovery_status", "column": "设备是否已回收", "label": "回收状态", "datalist": "recovery-status-options"},
    {"key": "recovery_reason", "column": "设备未回收原因", "label": "回收原因", "datalist": "recovery-reason-options"},
]

# 更正表单只读技术列（业务记录ID/业务版本/设备记录ID/设备版本）。
TECHNICAL_FIELDS: list[str] = ["业务记录ID", "业务版本", "设备记录ID", "设备版本"]

# 字段 key → 中文列名（更正表单提交键），供两处表单互转。
COLUMN_BY_KEY = {field["key"]: field["column"] for field in BUSINESS_FIELDS + DEVICE_FIELDS}
