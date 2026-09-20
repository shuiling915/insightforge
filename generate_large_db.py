"""Generate a test database with 200 tables to validate the SchemaRegistry.

Creates tables across multiple business domains with some intentionally
ambiguous names to test schema disambiguation.
"""

import random
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "workspace" / "large_ecommerce.db"

DOMAINS = {
    "user": [
        ("users", "注册用户表"),
        ("user_profiles", "用户详细资料"),
        ("user_addresses", "用户收货地址"),
        ("user_contacts", "用户联系方式"),
        ("user_preferences", "用户偏好设置"),
        ("user_tags", "用户标签"),
        ("user_levels", "用户等级体系"),
        ("user_points", "用户积分账户"),
        ("user_points_log", "积分变动流水"),
        ("user_behavior", "用户行为埋点"),
    ],
    "order": [
        ("orders", "订单主表"),
        ("order_items", "订单明细"),
        ("order_payments", "订单支付记录"),
        ("order_refunds", "退款记录"),
        ("order_logistics", "物流信息"),
        ("order_coupons", "订单使用的优惠券"),
        ("order_comments", "订单评价"),
        ("order_status_log", "订单状态变更历史"),
        ("order_invoices", "发票信息"),
        ("order_gifts", "订单赠品"),
    ],
    "product": [
        ("products", "商品目录"),
        ("product_categories", "商品分类"),
        ("product_skus", "商品SKU"),
        ("product_inventory", "商品库存"),
        ("product_prices", "商品价格历史"),
        ("product_images", "商品图片"),
        ("product_reviews", "商品评价"),
        ("product_questions", "商品问答"),
        ("product_tags", "商品标签"),
        ("product_suppliers", "供应商信息"),
    ],
    "marketing": [
        ("promotions", "促销活动"),
        ("coupons", "优惠券"),
        ("coupon_templates", "优惠券模板"),
        ("flash_sales", "限时秒杀"),
        ("banners", "首页轮播图"),
        ("push_notifications", "推送通知"),
        ("email_templates", "邮件模板"),
        ("sms_templates", "短信模板"),
        ("landing_pages", "落地页配置"),
        ("affiliate_links", "推广链接"),
    ],
    "finance": [
        ("transactions", "交易流水"),
        ("payment_methods", "支付方式配置"),
        ("withdrawals", "提现记录"),
        ("reconciliations", "对账记录"),
        ("invoices", "发票管理"),
        ("tax_records", "税务记录"),
        ("settlements", "结算记录"),
        ("balance_sheet", "资产负债"),
        ("daily_reports", "日报表"),
        ("monthly_reports", "月报表"),
    ],
    "logistics": [
        ("warehouses", "仓库信息"),
        ("inventory", "库存明细"),
        ("shipments", "发货记录"),
        ("delivery_routes", "配送路线"),
        ("carriers", "承运商"),
        ("tracking", "物流跟踪"),
        ("returns", "退货记录"),
        ("damages", "损坏记录"),
        ("stock_transfers", "库存调拨"),
        ("stocktakes", "盘点记录"),
    ],
    "crm": [
        ("customers", "客户信息"),
        ("customer_segments", "客户分群"),
        ("customer_lifecycle", "客户生命周期"),
        ("customer_value", "客户价值评分"),
        ("customer_complaints", "客户投诉"),
        ("customer_service_logs", "客服记录"),
        ("customer_tickets", "工单系统"),
        ("customer_surveys", "客户调研"),
        ("customer_rewards", "客户奖励"),
        ("customer_referrals", "客户推荐"),
    ],
    "analytics": [
        ("page_views", "页面浏览统计"),
        ("sessions", "会话统计"),
        ("conversions", "转化统计"),
        ("funnels", "漏斗分析"),
        ("ab_tests", "AB测试"),
        ("ab_test_variants", "AB测试分组"),
        ("events", "事件埋点"),
        ("event_properties", "事件属性"),
        ("cohorts", "同期群分析"),
        ("retention", "留存分析"),
    ],
    "merchant": [
        ("merchants", "商家信息"),
        ("merchant_stores", "店铺信息"),
        ("merchant_products", "商家商品"),
        ("merchant_orders", "商家订单"),
        ("merchant_settlements", "商家结算"),
        ("merchant_reviews", "商家评价"),
        ("merchant_contracts", "商家合同"),
        ("merchant_fees", "商家费用"),
        ("merchant_promotions", "商家促销"),
        ("merchant_data", "商家数据"),
    ],
    "risk": [
        ("risk_events", "风险事件"),
        ("risk_rules", "风控规则"),
        ("risk_scores", "风险评分"),
        ("blocked_users", "封禁用户"),
        ("fraud_orders", "欺诈订单"),
        ("fraud_payments", "欺诈支付"),
        ("ip_blacklist", "IP黑名单"),
        ("device_blacklist", "设备黑名单"),
        ("card_blacklist", "银行卡黑名单"),
        ("audit_logs", "审计日志"),
    ],
    "content": [
        ("articles", "文章内容"),
        ("article_categories", "文章分类"),
        ("article_comments", "文章评论"),
        ("article_likes", "文章点赞"),
        ("article_views", "文章浏览量"),
        ("videos", "视频内容"),
        ("video_comments", "视频评论"),
        ("images", "图片资源"),
        ("documents", "文档资源"),
        ("tags", "内容标签"),
    ],
    "chat": [
        ("messages", "聊天消息"),
        ("conversations", "会话列表"),
        ("message_reads", "消息已读记录"),
        ("chat_rooms", "聊天室"),
        ("chat_members", "聊天室成员"),
        ("notifications", "系统通知"),
        ("notification_reads", "通知已读记录"),
        ("bots", "机器人配置"),
        ("bot_responses", "机器人回复模板"),
        ("chat_attachments", "聊天附件"),
    ],
    "search": [
        ("search_queries", "搜索关键词"),
        ("search_results", "搜索结果"),
        ("search_clicks", "搜索点击记录"),
        ("search_suggestions", "搜索建议"),
        ("search_trends", "搜索趋势"),
        ("search_filters", "搜索筛选条件"),
        ("search_histories", "搜索历史"),
        ("search_hotwords", "热搜词"),
        ("search_rankings", "搜索排名"),
        ("search_synonyms", "搜索同义词"),
    ],
    "social": [
        ("follows", "关注关系"),
        ("followers", "粉丝列表"),
        ("likes", "点赞记录"),
        ("shares", "分享记录"),
        ("comments", "评论记录"),
        ("mentions", "@提及记录"),
        ("reposts", "转发记录"),
        ("favorites", "收藏记录"),
        ("blocks", "拉黑记录"),
        ("reports", "举报记录"),
    ],
    "device": [
        ("devices", "设备信息"),
        ("device_sessions", "设备会话"),
        ("device_locations", "设备位置"),
        ("device_apps", "设备应用"),
        ("device_logs", "设备日志"),
        ("device_health", "设备健康状态"),
        ("device_models", "设备型号"),
        ("device_os", "操作系统信息"),
        ("device_network", "网络信息"),
        ("device_permissions", "权限信息"),
    ],
    "api": [
        ("api_keys", "API密钥"),
        ("api_logs", "API调用日志"),
        ("api_limits", "API限流配置"),
        ("api_endpoints", "API端点"),
        ("api_tokens", "API令牌"),
        ("api_scopes", "API权限范围"),
        ("api_errors", "API错误日志"),
        ("api_metrics", "API指标"),
        ("api_versions", "API版本"),
        ("api_docs", "API文档"),
    ],
    "config": [
        ("configs", "系统配置"),
        ("config_history", "配置变更历史"),
        ("feature_flags", "功能开关"),
        ("feature_flag_rules", "开关规则"),
        ("tenant_configs", "租户配置"),
        ("tenant_limits", "租户限额"),
        ("tenant_users", "租户用户"),
        ("tenant_billing", "租户账单"),
        ("tenant_plans", "租户套餐"),
        ("tenant_domains", "租户域名"),
    ],
    "ad": [
        ("ad_campaigns", "广告活动"),
        ("ad_groups", "广告组"),
        ("ad_ads", "广告素材"),
        ("ad_impressions", "广告曝光"),
        ("ad_clicks", "广告点击"),
        ("ad_conversions", "广告转化"),
        ("ad_budgets", "广告预算"),
        ("ad_bids", "广告出价"),
        ("ad_targeting", "广告定向"),
        ("ad_creatives", "广告创意"),
    ],
}

AMBIGUOUS_TABLES = [
    ("transactions_log", "交易日志（与 transactions 不同，这是操作日志）"),
    ("user_events", "用户事件（与 events 不同，这是用户行为事件）"),
    ("order_data", "订单数据汇总（与 orders 不同，这是聚合数据）"),
    ("product_info", "商品信息扩展表（与 products 不同，这是扩展字段）"),
    ("payment_records", "支付记录（与 order_payments 不同，这是第三方支付记录）"),
    ("customer_data", "客户数据（与 customers 不同，这是客户画像数据）"),
    ("sales_summary", "销售汇总表（与 daily_reports 不同，这是按品类汇总）"),
    ("inventory_log", "库存日志（与 inventory 不同，这是变动历史）"),
    ("user_stats", "用户统计（与 user_behavior 不同，这是聚合统计）"),
    ("product_sales", "商品销量（与 order_items 不同，这是聚合后的销量）"),
    ("order_stats", "订单统计（与 orders 不同，这是聚合统计）"),
    ("finance_data", "财务数据（与 transactions 不同，这是汇总数据）"),
    ("marketing_data", "营销数据（与 promotions 不同，这是汇总数据）"),
    ("logistics_data", "物流数据（与 shipments 不同，这是汇总数据）"),
    ("risk_data", "风险数据（与 risk_events 不同，这是汇总数据）"),
    ("content_data", "内容数据（与 articles 不同，这是汇总数据）"),
    ("chat_data", "聊天数据（与 messages 不同，这是汇总数据）"),
    ("search_data", "搜索数据（与 search_queries 不同，这是汇总数据）"),
    ("merchant_data_ext", "商家扩展数据（与 merchant_data 不同，这是更多字段）"),
    ("crm_data", "CRM数据（与 customer_value 不同，这是汇总数据）"),
]


def create_table(conn, name: str, desc: str) -> None:
    """Create a table with realistic columns and sample data."""
    columns = [
        ("id", "INTEGER PRIMARY KEY"),
        ("created_at", "TEXT NOT NULL"),
        ("status", "TEXT NOT NULL"),
    ]

    rng = random.Random(hash(name) % (2**31))

    if "user" in name or "customer" in name:
        columns.extend([
            ("name", "TEXT"),
            ("email", "TEXT"),
            ("phone", "TEXT"),
            ("city", "TEXT"),
        ])
    elif "order" in name or "payment" in name or "transact" in name:
        columns.extend([
            ("amount", "REAL NOT NULL"),
            ("currency", "TEXT DEFAULT 'CNY'"),
            ("user_id", "INTEGER"),
        ])
    elif "product" in name or "sku" in name:
        columns.extend([
            ("name", "TEXT"),
            ("category", "TEXT"),
            ("price", "REAL"),
            ("stock", "INTEGER DEFAULT 0"),
        ])
    else:
        columns.extend([
            ("name", "TEXT"),
            ("value", "TEXT"),
            ("type", "TEXT"),
        ])

    col_defs = ", ".join(f"{c} {t}" for c, t in columns)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {name} ({col_defs})")

    row_count = rng.randint(5, 200)
    statuses = ["active", "inactive", "pending", "cancelled", "completed"]
    cities = ["Beijing", "Shanghai", "Guangzhou", "Shenzhen", "Hangzhou", "Chengdu"]
    categories = ["electronics", "clothing", "food", "books", "home"]

    rows = []
    for i in range(row_count):
        row = [i + 1, f"2024-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}", rng.choice(statuses)]
        if "user" in name or "customer" in name:
            row.extend([f"user_{i}", f"user{i}@example.com", f"138{rng.randint(0,99999999):08d}", rng.choice(cities)])
        elif "order" in name or "payment" in name or "transact" in name:
            row.extend([round(rng.uniform(10, 5000), 2), "CNY", rng.randint(1, 1000)])
        elif "product" in name or "sku" in name:
            row.extend([f"product_{i}", rng.choice(categories), round(rng.uniform(5, 2000), 2), rng.randint(0, 500)])
        else:
            row.extend([f"item_{i}", f"value_{rng.randint(1,100)}", rng.choice(["A", "B", "C"])])
        rows.append(tuple(row))

    placeholders = ", ".join("?" * len(columns))
    conn.executemany(f"INSERT INTO {name} VALUES ({placeholders})", rows)


def main():
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")

    all_tables = []
    for domain_tables in DOMAINS.values():
        all_tables.extend(domain_tables)
    all_tables.extend(AMBIGUOUS_TABLES)

    print(f"Creating {len(all_tables)} tables...")
    for name, desc in all_tables:
        create_table(conn, name, desc)
    conn.commit()

    table_count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]
    print(f"Created {table_count} tables in {DB_PATH}")

    sample = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' LIMIT 10"
    ).fetchall()
    print("Sample tables:", ", ".join(r[0] for r in sample))
    conn.close()

    # Generate schema descriptions YAML
    yaml_path = DB_PATH.parent / "large_ecommerce_descriptions.yaml"
    generate_descriptions_yaml(all_tables, yaml_path)
    print(f"Generated descriptions YAML: {yaml_path}")


def generate_descriptions_yaml(tables, yaml_path):
    """Generate a YAML file with table descriptions."""
    import yaml

    data = {"tables": {}}
    for name, desc in tables:
        data["tables"][name] = {"description": desc}

    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False)


if __name__ == "__main__":
    main()