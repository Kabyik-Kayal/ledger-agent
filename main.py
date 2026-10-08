import os
import re
import json
import sqlite3
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# =====================================================================
# CONFIGURATION
# =====================================================================
API_BASE = "https://exam.sanand.workers.dev/questionData?email=24f2007470%40ds.study.iitm.ac.in&quizSign=0CfETLFpAksUEXz1mdzbiVn%2BLQZeyIGuoleBxA84C%2BLxsyJTfDavUiJ3QKpKpKjmek57i8IJQduJJnVxUps0XU1JDd8ZysDFFTUnPzO9QW53MvlF1r4v%2Bq2H88l0S%2FzQUSGFZzRwvf3a6ZjaQDLO1xNYQsC%2Bu9Dhjdw0WA4cqNBlk%2BCTAHRg%2Fy9QjcEd%2B%2Fem8Zm8tOuwexEyXqGq1wbHiTwWbWJJ6XO9n9IqqUKKUf3apyk8esqmdYrt1qaW%2BkIiFu5JNfKR9J%2BsL9qW48W199Z3Jt7s%2BAnliV958Rz6LSpeoL0kfXjXggfhFuuGus9JeOuSD0eFTPTiOHVcvDSgBg%3D%3D&questionId=q-ledger-agent-server"

DB_FILE = "ledger.db"
TZ_KOLKATA = ZoneInfo("Asia/Kolkata")

# LLM Keys (Optional - works with or without!)
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
AIPROXY_TOKEN = os.environ.get("AIPROXY_TOKEN", "").strip()

gemini_client = None
if GEMINI_KEY:
    try:
        from google import genai
        gemini_client = genai.Client(api_key=GEMINI_KEY)
        print("[INFO] Connected to Gemini API")
    except Exception as e:
        print(f"[WARN] Gemini client init failed: {e}")


def parse_timestamp(dt_str: str) -> datetime:
    """Safely parse timestamps with 'Z' or timezone offsets."""
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"
    return datetime.fromisoformat(dt_str)


def init_database():
    """Fetches /rates and /export, cleans, deduplicates, and loads into SQLite."""
    print("[INFO] Fetching currency rates from /rates...")
    rates_resp = requests.get(f"{API_BASE}&path=/rates", timeout=15)
    rates_data = rates_resp.json().get("usd_per_unit", {"USD": 1.0, "EUR": 1.02, "INR": 0.01212})
    print(f"       Rates loaded: {rates_data}")

    print("[INFO] Fetching all ledger data from /export...")
    export_resp = requests.get(f"{API_BASE}&path=/export", timeout=30)
    lines = export_resp.text.strip().splitlines()
    print(f"[INFO] Total raw rows downloaded: {len(lines)}")

    # Rule 2: Keep only the latest updated_at for each order id
    latest_orders = {}
    for line in lines:
        if not line.strip():
            continue
        row = json.loads(line)
        oid = row["id"]
        up_dt = parse_timestamp(row["updated_at"])

        if oid not in latest_orders or up_dt > latest_orders[oid]["_parsed_up"]:
            row["_parsed_up"] = up_dt
            latest_orders[oid] = row

    print(f"[INFO] Unique orders after deduplication: {len(latest_orders)}")

    # Initialize SQLite database
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS orders")
    cur.execute("""
    CREATE TABLE orders (
        id TEXT PRIMARY KEY,
        customer TEXT,
        region TEXT,
        product TEXT,
        qty INTEGER,
        unit_price REAL,
        amount REAL,
        currency TEXT,
        usd_amount REAL,
        status TEXT,
        created_at TEXT,
        updated_at TEXT,
        business_date TEXT,     -- YYYY-MM-DD
        business_year INTEGER,
        business_month INTEGER,
        business_day INTEGER
    )
    """)

    # Populate rows
    for order in latest_orders.values():
        c_dt = parse_timestamp(order["created_at"]).astimezone(TZ_KOLKATA)
        u_dt = order["_parsed_up"].astimezone(TZ_KOLKATA)
        rate = rates_data.get(order["currency"], 1.0)
        usd_amt = round(order["amount"] * rate, 2)

        cur.execute("""
        INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order["id"],
            order["customer"],
            order["region"],
            order["product"],
            order["qty"],
            order["unit_price"],
            order["amount"],
            order["currency"],
            usd_amt,
            order["status"],
            c_dt.isoformat(),
            u_dt.isoformat(),
            c_dt.strftime("%Y-%m-%d"),
            c_dt.year,
            c_dt.month,
            c_dt.day
        ))

    conn.commit()
    conn.close()
    print("[INFO] Database ready for queries!")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    yield

app = FastAPI(lifespan=lifespan)


class QuestionRequest(BaseModel):
    question: str


# =====================================================================
# ZERO-KEY RULE-BASED QUERY PARSER (Works 100% offline without any API key)
# =====================================================================
def parse_question_rule_based(q: str) -> str:
    q_low = q.lower()

    # 1. Determine Status Filter
    status_filter = None
    if any(w in q_low for w in ["refund", "refunded", "returns"]):
        status_filter = "refunded"
    elif any(w in q_low for w in ["void", "cancelled", "canceled"]):
        status_filter = "void"
    elif any(w in q_low for w in ["paid", "bought", "buy", "purchased", "purchase", "sold", "sales", "revenue", "gross", "earned", "income"]):
        status_filter = "paid"

    # 2. Extract Region
    regions = ["North", "South", "East", "West", "Central"]
    found_region = None
    for r in regions:
        if re.search(r"\b" + r.lower() + r"\b", q_low):
            found_region = r
            break

    # 3. Extract Product (supports singular & plural)
    products = ["Blender", "Grinder", "Air Fryer", "Kettle", "Juicer", "Rice Cooker", "Mixer", "Toaster"]
    found_product = None
    for p in products:
        pattern = r"\b" + re.escape(p.lower()) + r"s?\b"
        if re.search(pattern, q_low):
            found_product = p
            break

    # 4. Extract Year
    m_year = re.search(r"\b(202[0-9])\b", q)
    found_year = int(m_year.group(1)) if m_year else None

    # 5. Extract Month
    months = {
        "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
        "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
        "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10,
        "november": 11, "nov": 11, "december": 12, "dec": 12
    }
    found_month = None
    for m_name, m_num in months.items():
        if re.search(r"\b" + m_name + r"\b", q_low):
            found_month = m_num
            break

    # 6. Extract Quarter
    quarter_months = None
    if re.search(r"\b(q1|1st quarter|first quarter)\b", q_low):
        quarter_months = (1, 3)
    elif re.search(r"\b(q2|2nd quarter|second quarter)\b", q_low):
        quarter_months = (4, 6)
    elif re.search(r"\b(q3|3rd quarter|third quarter)\b", q_low):
        quarter_months = (7, 9)
    elif re.search(r"\b(q4|4th quarter|fourth quarter)\b", q_low):
        quarter_months = (10, 12)

    # 7. Intent flags
    is_count_customers = any(w in q_low for w in ["customer", "customers"]) and any(w in q_low for w in ["how many", "number of", "count of", "distinct", "unique"])
    is_count_orders = any(w in q_low for w in ["how many order", "number of order", "count of order", "orders were", "orders placed"])
    is_quantity = any(w in q_low for w in ["how many unit", "total quantity", "quantity of", "units of", "units sold"]) or ("quantity" in q_low and "highest" not in q_low and "lowest" not in q_low)
    
    is_which_prod = any(w in q_low for w in ["which product", "best selling product", "top product", "most popular product"]) or ("product" in q_low and any(w in q_low for w in ["highest", "most", "top", "best", "lowest", "least"]))
    is_which_cust = any(w in q_low for w in ["which customer", "top customer", "customer spent the most", "customer with highest"])
    is_which_region = any(w in q_low for w in ["which region", "top region", "region with highest", "region had the highest", "region generated"])

    is_avg = any(w in q_low for w in ["average", "avg", "mean"])
    is_order_desc = not any(w in q_low for w in ["lowest", "least", "bottom", "worst"])

    # Base WHERE clauses
    where_clauses = []
    if status_filter:
        where_clauses.append(f"status = '{status_filter}'")
    if found_region and not is_which_region:
        where_clauses.append(f"region = '{found_region}'")
    if found_product and not is_which_prod:
        where_clauses.append(f"product = '{found_product}'")
    if found_year:
        where_clauses.append(f"business_year = {found_year}")
    if found_month:
        where_clauses.append(f"business_month = {found_month}")
    elif quarter_months:
        where_clauses.append(f"business_month BETWEEN {quarter_months[0]} AND {quarter_months[1]}")

    where_str = " AND ".join(where_clauses) if where_clauses else "1=1"
    order_dir = "DESC" if is_order_desc else "ASC"

    # Route by intent
    if is_which_prod:
        order_col = "SUM(qty)" if "unit" in q_low or "quantity" in q_low else "SUM(usd_amount)"
        s_filter = "status = 'paid' AND " if "status = 'paid'" not in where_str and not status_filter else ""
        return f"SELECT product FROM orders WHERE {s_filter}{where_str} GROUP BY product ORDER BY {order_col} {order_dir} LIMIT 1"

    if is_which_cust:
        s_filter = "status = 'paid' AND " if "status = 'paid'" not in where_str and not status_filter else ""
        return f"SELECT customer FROM orders WHERE {s_filter}{where_str} GROUP BY customer ORDER BY SUM(usd_amount) {order_dir} LIMIT 1"

    if is_which_region:
        s_filter = "status = 'paid' AND " if "status = 'paid'" not in where_str and not status_filter else ""
        return f"SELECT region FROM orders WHERE {s_filter}{where_str} GROUP BY region ORDER BY SUM(usd_amount) {order_dir} LIMIT 1"

    if is_count_customers:
        return f"SELECT COUNT(DISTINCT customer) FROM orders WHERE {where_str}"

    if is_quantity:
        s_filter = "status = 'paid' AND " if "status = 'paid'" not in where_str and not status_filter else ""
        return f"SELECT SUM(qty) FROM orders WHERE {s_filter}{where_str}"

    if is_count_orders:
        return f"SELECT COUNT(*) FROM orders WHERE {where_str}"

    if is_avg:
        s_filter = "status = 'paid' AND " if "status = 'paid'" not in where_str and not status_filter else ""
        return f"SELECT ROUND(AVG(usd_amount), 2) FROM orders WHERE {s_filter}{where_str}"

    # Default: Revenue / Money
    s_filter = "status = 'paid' AND " if "status = 'paid'" not in where_str and not status_filter else ""
    return f"SELECT ROUND(SUM(usd_amount), 2) FROM orders WHERE {s_filter}{where_str}"


# =====================================================================
# LLM SQL GENERATION (Used if an API key is provided)
# =====================================================================
SYSTEM_PROMPT = """You are a senior SQL analyst. Write a single SQLite SQL query to answer questions on Acme Appliances orders.

Table Schema:
CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    customer TEXT,
    region TEXT,           -- 'North', 'South', 'East', 'West', 'Central'
    product TEXT,          -- 'Blender', 'Grinder', 'Air Fryer', 'Kettle', 'Juicer', 'Rice Cooker', 'Mixer', 'Toaster'
    qty INTEGER,
    unit_price REAL,
    amount REAL,
    currency TEXT,         -- 'USD', 'EUR', 'INR'
    usd_amount REAL,       -- amount in USD
    status TEXT,           -- 'paid', 'refunded', 'void'
    business_date TEXT,    -- 'YYYY-MM-DD' in Asia/Kolkata timezone
    business_year INTEGER, -- 2026
    business_month INTEGER,-- 1 to 12
    business_day INTEGER
);

RULES:
1. ONLY orders with status = 'paid' count as revenue/sales.
2. If asked about refunds, filter by status = 'refunded'.
3. For monetary answers, return ROUND(SUM(usd_amount), 2).
4. Return ONLY the raw SQL query. No markdown, no explanations.
5. Query MUST return a single scalar value (1 row, 1 column).
"""

def generate_sql_with_llm(question: str) -> str:
    if gemini_client:
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"{SYSTEM_PROMPT}\n\nQuestion: {question}\nSQL Query:",
        )
        sql = response.text.strip()
        if "```" in sql:
            sql = re.sub(r"^```[a-zA-Z]*\n?", "", sql)
            sql = re.sub(r"\n?```$", "", sql)
        return sql.strip()

    elif AIPROXY_TOKEN or OPENAI_KEY:
        key = AIPROXY_TOKEN or OPENAI_KEY
        base_url = "https://aiproxy.sanand.workers.dev/openai/v1" if AIPROXY_TOKEN else "https://api.openai.com/v1"
        res = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": question}
                ],
                "temperature": 0
            },
            timeout=10
        )
        data = res.json()
        sql = data["choices"][0]["message"]["content"].strip()
        if "```" in sql:
            sql = re.sub(r"^```[a-zA-Z]*\n?", "", sql)
            sql = re.sub(r"\n?```$", "", sql)
        return sql.strip()

    else:
        return parse_question_rule_based(question)


# =====================================================================
# API ROUTES
# =====================================================================
@app.get("/")
def health_check():
    return {"status": "ok", "service": "Acme Ledger Agent", "version": "1.1"}


@app.post("/")
async def answer_question(req: QuestionRequest):
    question = req.question.strip()
    print(f"\n[QUERY] Question: {question}")

    sql = ""
    # Try LLM if configured, otherwise rule-based
    if gemini_client or OPENAI_KEY or AIPROXY_TOKEN:
        try:
            sql = generate_sql_with_llm(question)
            print(f"[LLM] Generated SQL: {sql}")
        except Exception as e:
            print(f"[WARN] LLM generation failed ({e}), falling back to Rule-based parser...")
            sql = parse_question_rule_based(question)
    else:
        sql = parse_question_rule_based(question)
        print(f"[RULE] Rule-Based SQL: {sql}")

    # Execute SQL
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute(sql)
        row = cur.fetchone()
        conn.close()

        if row is None or row[0] is None:
            ans = 0
        else:
            ans = row[0]

        if isinstance(ans, float):
            ans = round(ans, 2)

        print(f"[RESULT] Final Answer: {ans}")
        return {"answer": ans}

    except Exception as e:
        print(f"[ERROR] SQL Execution Error: {e}")
        # As emergency fallback, try rule-based if LLM SQL broke
        try:
            fallback_sql = parse_question_rule_based(question)
            print(f"[RETRY] Trying emergency fallback SQL: {fallback_sql}")
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute(fallback_sql)
            row = cur.fetchone()
            conn.close()
            ans = row[0] if (row and row[0] is not None) else 0
            if isinstance(ans, float):
                ans = round(ans, 2)
            return {"answer": ans}
        except Exception as fb_err:
            raise HTTPException(status_code=500, detail=f"Execution error: {e}, Fallback error: {fb_err}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
