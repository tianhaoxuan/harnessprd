<!-- 来源 backend/validation_out/split_api.txt 第 756–804 行，已通过评审的真实模型产出，原样摘录未改写。 -->
<!-- 选它是因为一个接口就同时体现统一响应（分页包裹）、错误码引用写法、查询参数默认值与上限。 -->

### GET /api/v1/exports

**用途**：查看与当前用户有关的导出历史记录。

**PRD 来源**：FR-06

**鉴权**：需要登录，角色 owner 或 member。

**查询参数**

| 字段 | 类型 | 必填 | 默认 | 约束 |
| --- | --- | --- | --- | --- |
| page | integer | 否 | 1 | 从 1 起 |
| page_size | integer | 否 | 20 | 上限 100 |

**响应**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| items | array | 导出记录列表 |
| items[].id | string | 导出记录 id，形如 `exp_001` |
| items[].report_id | string | 草稿 id |
| items[].feishu_doc_url | string | 飞书文档链接 |
| items[].exported_at | string | ISO 8601 UTC |
| page | integer | 当前页 |
| page_size | integer | 每页条数 |
| total | integer | 总条数 |

```json
{
  "items": [
    {
      "id": "exp_001",
      "report_id": "rpt_001",
      "feishu_doc_url": "https://example.feishu.cn/docx/AbCdEf123456",
      "exported_at": "2025-01-13T03:10:00Z"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

**错误码**：`AUTH005`

**业务规则**：仅返回与当前用户有关的记录，owner 返回其归属群聊下的全部导出，member 仅返回自己被汇总到的草稿的导出。

**关联验收标准**：AC-10
