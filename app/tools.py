"""业务工具层：工具注册表 + 意图选工具。

设计取舍（重要，面试会被问）：
这里**不让模型自己决定调哪个工具**（function calling 多一轮往返，客服场景 1 秒都贵），
而是用规则从问题里选工具，再把工具结果塞进 prompt。代价是新增工具要维护关键词，
收益是延迟可预测、行为可复现、出错时能一眼看出是选错工具还是工具本身错。

`TOOL_REGISTRY` 是唯一事实源：图里分发、对外暴露能力清单，都从这里读。
"""
from __future__ import annotations

import datetime
import re

from . import db

# ── 演示数据；真实场景替换为 ERP / 订单服务调用 ──────────────
MOCK_ORDERS = {
    "202610190001": {"order_no": "202610190001", "item": "智能客服专业账户（年付）",
                     "amount": "1288.00", "status": "已发货", "logistics": "顺丰 SF1234567890",
                     "shipped_at": "2026-10-18 09:20", "refund": None},
    "202610190002": {"order_no": "202610190002", "item": "坐席增值包（5 席）",
                     "amount": "600.00", "status": "待支付", "logistics": "-",
                     "shipped_at": "-", "refund": None},
    "202610180003": {"order_no": "202610180003", "item": "智能客服标准账户（月付）",
                     "amount": "129.00", "status": "已签收", "logistics": "京东 JD9988776655",
                     "shipped_at": "2026-10-17 14:05", "refund": "审核中"},
    "202610170004": {"order_no": "202610170004", "item": "智能客服标准账户（月付）",
                     "amount": "129.00", "status": "已签收", "logistics": "顺丰 SF5566778899",
                     "shipped_at": "2026-10-16 11:30", "refund": "已原路退回，银行处理中"},
    "202610160005": {"order_no": "202610160005", "item": "在线客服年费续费",
                     "amount": "899.00", "status": "已发货", "logistics": "顺丰 SF1122334455",
                     "shipped_at": "2026-10-15 10:10", "refund": None},
}

BUSINESS_INFO = {
    "hours": "工作日 9:00-18:00，周末 10:00-16:00",
    "phone": "400-800-1234",
    "address": "杭州市余杭区文一西路 969 号",
    "email": "support@selfdesk.local",
}

ORDER_NO = re.compile(r"\b(\d{10,18})\b")


def extract_order_no(text: str) -> str | None:
    m = ORDER_NO.search(text or "")
    return m.group(1) if m else None


# ── 工具实现 ────────────────────────────────────────────────
def query_order(order_no: str | None) -> dict:
    if not order_no:
        return {"ok": False, "error": "missing_order_no",
                "message": "没有识别到订单号，请让用户提供 10~18 位订单号。"}
    order = db.get_order(order_no)
    if not order:
        return {"ok": False, "error": "not_found",
                "message": f"系统中没有查询到订单 {order_no}，请核对单号或转人工核实。"}
    return {"ok": True, "order": order,
            "message": (f"订单 {order['order_no']}：{order['item']}，金额 ¥{order['amount']}，"
                        f"状态「{order['status']}」，物流 {order['logistics']}。")}


def query_refund(order_no: str | None) -> dict:
    """退款进度。与「退款政策」区分：政策类问题走知识库，这里只查具体订单。"""
    if not order_no:
        return {"ok": False, "error": "missing_order_no",
                "message": "查询退款进度需要订单号，请让用户提供 10~18 位单号。"}
    order = db.get_order(order_no)
    if not order:
        return {"ok": False, "error": "not_found",
                "message": f"订单 {order_no} 不存在，请核对后重试或转人工。"}
    state = order.get("refund")
    if not state:
        return {"ok": True, "order": order, "refund": None,
                "message": f"订单 {order_no} 目前没有退款申请记录。如需申请，可转人工办理。"}
    return {"ok": True, "order": order, "refund": state,
            "message": f"订单 {order_no}（¥{order['amount']}）的退款状态：{state}。"}


def business_info(question: str) -> dict:
    """营业时间 / 联系方式 / 地址 —— 高频但不需要查库的问题。"""
    items = []
    if any(w in question for w in ("时间", "几点", "上班", "营业")):
        items.append(f"服务时间：{BUSINESS_INFO['hours']}")
    if any(w in question for w in ("电话", "联系", "热线", "怎么找")):
        items.append(f"客服电话：{BUSINESS_INFO['phone']}")
    if any(w in question for w in ("地址", "在哪", "公司")):
        items.append(f"公司地址：{BUSINESS_INFO['address']}")
    if any(w in question for w in ("邮箱", "邮件", "email", "Email")):
        items.append(f"客服邮箱：{BUSINESS_INFO['email']}")
    if not items:
        items = [f"服务时间：{BUSINESS_INFO['hours']}",
                 f"客服电话：{BUSINESS_INFO['phone']}"]
    return {"ok": True, "message": "；".join(items) + "。"}


def create_ticket(session_id: str, title: str, detail: str = "",
                  priority: str = "NORMAL") -> dict:
    ticket = db.create_ticket(session_id, title, detail, priority=priority)
    return {"ok": True, "ticket": ticket,
            "message": f"已为你创建工单 {ticket['id']}，稍后会有专员跟进。"}


TICKET_STATUS_CN = {"OPEN": "待处理", "DONE": "已解决", "CLOSED": "已关闭", "FAILED": "处理失败"}

PLANS = [
    {"plan": "标准版（月付）", "seats": 3, "expires": "2026-11-16", "balance": "0.00"},
    {"plan": "专业版（年付）", "seats": 10, "expires": "2027-06-30", "balance": "128.00"},
    {"plan": "试用版（14 天）", "seats": 3, "expires": "2026-10-26", "balance": "0.00"},
]

# 可售产品与定价（演示用；真实场景来自商品中心）
PRODUCTS = {
    "标准版（月付）": 129,
    "标准版（年付）": 1288,
    "专业版（年付）": 3990,
    "坐席增值包（5 席）": 600,
}

PURCHASE_WORDS = ("购买", "下单", "买一", "要买", "想买", "买个", "买套", "开通", "续费",
                  "升级套餐", "来一份", "开一个", "补一个", "增购", "加坐席", "买坐席",
                  "加几个坐席")
PRICE_WORDS = ("多少钱", "价格", "怎么收费", "费用", "贵不贵", "什么价")

REFUND_ACTION_WORDS = ("申请退款", "帮我退款", "提交退款", "我要退款", "退钱", "申请退货")


def _plan_index(session_id: str) -> int:
    try:
        return int((session_id or "0")[-4:] or "0", 16) % len(PLANS)
    except ValueError:
        return 0


def match_product(question: str, session_id: str = "") -> str | None:
    """从问句里识别要买哪个产品。识别不出返回 None，由工具引导用户说明。"""
    q = question or ""
    if "专业版" in q:
        return "专业版（年付）"
    if "标准版" in q:
        return "标准版（年付）" if ("年" in q) else "标准版（月付）"
    if any(w in q for w in ("坐席", "席位")):
        return "坐席增值包（5 席）"
    if "续费" in q or "升级" in q:
        # 没指明产品时，按当前套餐续：查了套餐说「那续费吧」，这是最自然的衔接
        current = PLANS[_plan_index(session_id)]["plan"]
        if "专业" in current:
            return "专业版（年付）"
        if "标准" in current:
            return "标准版（月付）"
    return None


def query_ticket(session_id: str) -> dict:
    """查本会话提交过的工单进度。"""
    tickets = [t for t in db.list_tickets(limit=200) if t["session_id"] == session_id]
    if not tickets:
        return {"ok": False, "error": "no_ticket",
                "message": "这个会话还没有提交过工单。如果需要，我可以现在帮你建一个。"}
    t = tickets[0]
    status = TICKET_STATUS_CN.get(t["status"], t["status"])
    msg = f"工单 {t['id']}（{t['title'][:24]}）当前状态：{status}"
    if t.get("assignee"):
        msg += f"，处理人 {t['assignee']}"
    return {"ok": True, "ticket": t, "message": msg + "。"}


def account_info(session_id: str) -> dict:
    """查账户套餐与到期时间。演示数据按会话确定，保证同一会话每次一致。"""
    try:
        idx = int((session_id or "0")[-4:] or "0", 16) % len(PLANS)
    except ValueError:
        idx = 0
    p = PLANS[idx]
    return {"ok": True, "account": p,
            "message": (f"当前套餐：{p['plan']}，{p['seats']} 个坐席，"
                        f"{p['expires']} 到期，账户余额 ¥{p['balance']}。")}


def submit_refund(session_id: str, question: str, order_no: str | None = None) -> dict:
    """写操作第一阶段：只生成待确认提案，绝不直接执行。

    设计原则：模型/规则都不能自己决定动钱。必须由用户下一轮明确说「确认」，
    才由图里的 execute 节点真正提交。这样即使模型误判，也不会造成资金损失。
    """
    order_no = order_no or extract_order_no(question)
    if not order_no:
        return {"ok": False, "error": "missing_order_no",
                "message": "申请退款需要订单号，请提供 10~18 位订单号。"}
    order = db.get_order(order_no)
    if not order:
        return {"ok": False, "error": "not_found",
                "message": f"没有查到订单 {order_no}，请核对单号或转人工。"}
    if order.get("refund"):
        return {"ok": True, "done": True,
                "message": f"订单 {order_no} 已有退款记录：{order['refund']}，无需重复提交。"}

    pending = db.create_pending(session_id, "submit_refund",
                                {"order_no": order_no,
                                 "item": order["item"],
                                 "amount": order["amount"]})
    return {"ok": True, "need_confirm": True, "pending": pending,
            "message": (f"即将为订单 {order_no}（{order['item']}，¥{order['amount']}）提交退款申请。"
                        f"回复「确认」继续，回复「取消」放弃。")}


def _gen_order_no() -> str:
    """生成不冲突的订单号：日期 + 4 位序号。"""
    base = datetime.datetime.now().strftime("2026%m%d")
    for i in range(1, 9999):
        candidate = f"{base}{i:04d}"
        if not db.order_exists(candidate):
            return candidate
    raise RuntimeError("订单号空间已满")


def create_order(session_id: str, question: str, product: str | None = None) -> dict:
    """写操作第一阶段：下单 / 续费。

    与退款同理，只生成提案、不直接创建订单——下单同样会造成资金动作，
    必须由用户明确确认后才落到第二阶段。
    """
    product = product or match_product(question, session_id)
    if not product:
        listing = "、".join(f"{k} ¥{v}" for k, v in PRODUCTS.items())
        return {"ok": False, "error": "missing_product",
                "message": f"请告诉我要开通哪个套餐：{listing}。"}
    amount = PRODUCTS[product]
    pending = db.create_pending(session_id, "create_order",
                                {"product": product, "amount": amount})
    return {"ok": True, "need_confirm": True, "pending": pending,
            "message": (f"即将创建订单：{product}，金额 ¥{amount}。"
                        f"回复「确认」继续，回复「取消」放弃。")}


def execute_pending(session_id: str, pending: dict | None) -> dict:
    """写操作第二阶段：真正执行。只应由图里的 execute 节点、在用户确认后调用。

    幂等靠 db.finish_pending 的条件更新（WHERE status='PENDING'）：
    重复确认或并发确认时，只有第一次能把状态改走，后续返回「已处理过」。
    """
    if not pending:
        return {"ok": False, "error": "no_pending", "message": "没有待确认的操作。"}

    action = pending.get("action")
    payload = pending.get("payload") or {}

    if action == "create_order":
        product = payload.get("product")
        amount = payload.get("amount")
        if product not in PRODUCTS:
            db.finish_pending(pending["id"], "FAILED")
            return {"ok": False, "error": "bad_product", "message": "产品不存在，操作已终止。"}
        if not db.finish_pending(pending["id"], "DONE"):
            return {"ok": False, "error": "already_done",
                    "message": "该操作已经处理过了，无需重复提交。"}
        order_no = _gen_order_no()
        db.save_order({
            "order_no": order_no, "item": product, "amount": f"{float(amount):.2f}",
            "status": "待支付", "logistics": "-", "shipped_at": "-", "refund": None,
        })
        return {"ok": True, "done": True, "order_no": order_no,
                "message": (f"已创建订单 {order_no}：{product}，金额 ¥{amount}。"
                            f"请在 24 小时内完成支付，支付后可随时问我订单进度。")}

    if action != "submit_refund":
        db.finish_pending(pending["id"], "FAILED")
        return {"ok": False, "error": "unknown_action", "message": "不支持的操作类型。"}

    order_no = payload.get("order_no")
    order = db.get_order(order_no)
    if not order:
        db.finish_pending(pending["id"], "FAILED")
        return {"ok": False, "error": "not_found", "message": "订单不存在，操作已终止。"}

    if not db.finish_pending(pending["id"], "DONE"):
        return {"ok": False, "error": "already_done",
                "message": "该操作已经处理过了，无需重复提交。"}

    db.set_order_refund(order_no, "已提交，等待审核")
    ticket = db.create_ticket(session_id, f"退款申请：{order_no}",
                              f"{payload.get('item', '')} ¥{payload.get('amount', '')}",
                              priority="HIGH")
    return {"ok": True, "done": True, "ticket": ticket,
            "message": (f"已提交订单 {order_no} 的退款申请，工单号 {ticket['id']}，"
                        f"财务会在 3 个工作日内审核，退款按原支付方式退回。")}


# ── 工具注册表（唯一事实源）─────────────────────────────────
def seed_demo_orders() -> int:
    """把内置演示订单导入数据库（仅首次；不覆盖用户后来新建的订单）。"""
    return db.seed_orders(list(MOCK_ORDERS.values()))


TOOL_REGISTRY = {
    "query_order": {
        "desc": "按订单号查询订单状态与物流",
        "params": ["order_no"],
        "fn": query_order,
    },
    "query_refund": {
        "desc": "按订单号查询退款进度（非政策咨询）",
        "params": ["order_no"],
        "fn": query_refund,
    },
    "business_info": {
        "desc": "查询服务时间、客服电话、公司地址、邮箱",
        "params": [],
        "fn": business_info,
    },
    "query_ticket": {
        "desc": "查询本会话提交过的工单进度",
        "params": [],
        "fn": query_ticket,
    },
    "account_info": {
        "desc": "查询账户套餐、坐席数、到期时间、余额",
        "params": [],
        "fn": account_info,
    },
    "submit_refund": {
        "desc": "提交退款申请（写操作，必须用户二次确认后才执行）",
        "params": ["order_no"],
        "fn": submit_refund,
    },
    "create_order": {
        "desc": "下单购买套餐或续费（写操作，必须用户二次确认后才执行）",
        "params": ["product"],
        "fn": create_order,
    },
    "create_ticket": {
        "desc": "创建售后工单，转人工跟进",
        "params": ["title", "detail"],
        "fn": create_ticket,
    },
}

# 供对外暴露能力清单 / 未来接 function calling 用
TOOL_SPECS = [
    {"name": name, "desc": spec["desc"], "params": spec["params"]}
    for name, spec in TOOL_REGISTRY.items()
]


def pick_tool(question: str) -> str | None:
    """按问题选工具。返回 None 表示这不是工具类问题（应走知识库）。

    顺序有意义：写操作优先 → 有单号看查询类 → 再按业务词 → 最后静态信息。
    「退款多久能到账」这类**政策问题**刻意不命中任何退款工具，
    因为它没有单号、也没有申请动作词，应交给知识库。
    """
    q = question or ""
    order_no = extract_order_no(q)

    # 写操作优先：有明确申请动作词才走 submit_refund（无单号也识别，由工具内部引导补号）
    if any(w in q for w in REFUND_ACTION_WORDS):
        return "submit_refund"

    # 问价格属于政策咨询，交给知识库；不要直接弹下单提案
    if any(w in q for w in PRICE_WORDS):
        return None

    # 下单 / 续费：写了「续费」但没指明产品时，工具会按当前套餐自动匹配
    if any(w in q for w in PURCHASE_WORDS):
        return "create_order"

    if order_no:
        if any(w in q for w in ("退款", "退钱", "到账", "退回")):
            return "query_refund"
        return "query_order"

    if any(w in q for w in ("物流", "发货", "签收", "快递", "配送", "到哪了", "单号")):
        return "query_order"

    if any(w in q for w in ("我的退款", "退款进度", "退款到哪", "退到哪了")):
        return "query_refund"

    if any(w in q for w in ("工单", "我上次提", "处理得怎么样", "问题解决了吗", "进度如何")):
        return "query_ticket"

    if any(w in q for w in ("套餐", "到期", "续费", "余额", "多少席", "几个坐席", "我的账户")):
        return "account_info"

    if any(w in q for w in ("营业时间", "服务时间", "几点上班", "客服电话", "联系电话",
                            "怎么联系", "公司地址", "你们在哪", "邮箱")):
        return "business_info"

    return None


def run_tool(name: str, question: str, session_id: str = "",
             args: dict | None = None) -> dict:
    """统一执行入口：按名字分发。

    参数优先用上游校验节点放进 state.tool_args 的值，没有才从问题里抽。
    这样 v_order / v_refund 校验过的参数不会在执行时被换掉。
    """
    spec = TOOL_REGISTRY.get(name)
    if not spec:
        return {"ok": False, "error": "unknown_tool", "message": ""}
    args = args or {}
    if name in ("query_order", "query_refund"):
        return spec["fn"](args.get("order_no") or extract_order_no(question))
    if name == "submit_refund":
        return spec["fn"](session_id, question, args.get("order_no"))
    if name == "create_order":
        return spec["fn"](session_id, question, args.get("product"))
    if name in ("business_info", "account_info"):
        return spec["fn"](question) if name == "business_info" else spec["fn"](session_id)
    if name == "query_ticket":
        return spec["fn"](session_id)
    if name == "create_ticket":
        return spec["fn"](session_id, question[:60], question)
    return {"ok": False, "error": "bad_dispatch", "message": ""}
