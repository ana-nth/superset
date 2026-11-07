"""
Deterministic seed script for the wh schema.
Volumes: 200 products, 8 warehouses, 52 weekly snapshots, ~100k movements, ~60k order lines.
Planted patterns:
  1. Q4 seasonality in sales
  2. Pareto: ~20% SKUs -> ~80% revenue
  3. 10 dead-stock SKUs: no ISSUE for 120+ days
  4. warehouse_id=5 (WH-MID2) has abnormally high negative ADJUSTMENTs
  5. Discount spike: Retail/Online channel in month 8 (August)
  6. A few stockouts with open demand (NEW orders, qty_on_hand=0)
  7. ~1% NULLs in ship_date and reason_code; a few duplicate reference_doc values
Usage:
    pip install psycopg[binary] pyyaml
    python seed.py  # or set WAREHOUSE_DSN env var
"""
from __future__ import annotations

import io
import os
import random
import datetime
import math
from decimal import Decimal

import psycopg

random.seed(42)

DSN = os.getenv(
    "WAREHOUSE_DSN",
    "host=localhost port=5433 dbname=warehouse user=warehouse password=warehouse"
)

# ── helpers ────────────────────────────────────────────────────────────────────

def rand_date(start: datetime.date, end: datetime.date) -> datetime.date:
    delta = (end - start).days
    return start + datetime.timedelta(days=random.randint(0, delta))


def copy_rows(cur, table: str, cols: list[str], rows: list[tuple]) -> None:
    """Bulk-load via COPY."""
    buf = io.StringIO()
    for row in rows:
        parts = []
        for v in row:
            if v is None:
                parts.append("\\N")
            elif isinstance(v, bool):
                parts.append("t" if v else "f")
            elif isinstance(v, datetime.datetime):
                parts.append(v.isoformat())
            elif isinstance(v, datetime.date):
                parts.append(v.isoformat())
            elif isinstance(v, Decimal):
                parts.append(str(v))
            else:
                parts.append(str(v).replace("\t", " ").replace("\n", " "))
        buf.write("\t".join(parts) + "\n")
    buf.seek(0)
    with cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
        cp.write(buf.read())


# ── dimension data ─────────────────────────────────────────────────────────────

CATEGORIES = {
    "Electronics":   ["Sensors", "Displays", "Controllers", "Power Units"],
    "Consumables":   ["Lubricants", "Adhesives", "Packaging", "Cleaning"],
    "Mechanical":    ["Bearings", "Seals", "Fasteners", "Shafts"],
    "Safety":        ["PPE", "Signage", "Detection", "Containment"],
    "Tools":         ["Hand Tools", "Cutting", "Measurement", "Fixtures"],
}
BRANDS = ["AlphaTech", "BetaMfg", "GammaParts", "DeltaInc", "EpsilonCo",
          "ZetaWorks", "EtaSupply", "ThetaCorp"]
UOMS = ["EA", "BOX", "KG", "L", "M", "ROLL", "SET"]
WAREHOUSES = [
    (1, "WH-NE1", "Northeast Hub",      "Boston",      "MA", "USA", "Northeast", 120000, "Julia Chen",     True,  "2018-03-01"),
    (2, "WH-NE2", "Northeast Secondary","Hartford",    "CT", "USA", "Northeast",  80000, "Marcus Lee",     True,  "2019-07-15"),
    (3, "WH-SE1", "Southeast DC",       "Atlanta",     "GA", "USA", "Southeast", 150000, "Sandra King",    True,  "2017-01-10"),
    (4, "WH-SE2", "Florida Storage",    "Miami",       "FL", "USA", "Southeast",  60000, "Carlos Ruiz",    True,  "2020-05-22"),
    (5, "WH-MID1","Midwest Central",    "Chicago",     "IL", "USA", "Midwest",   200000, "Rachel Brown",   True,  "2016-09-01"),  # high adj
    (6, "WH-MID2","Midwest Secondary",  "Columbus",    "OH", "USA", "Midwest",   100000, "Tom Davis",      True,  "2021-02-14"),
    (7, "WH-W1",  "West Coast Hub",     "Los Angeles", "CA", "USA", "West",      180000, "Amy Zhang",      True,  "2017-06-30"),
    (8, "WH-W2",  "Pacific Northwest",  "Seattle",     "WA", "USA", "West",       70000, "Dave Wilson",    True,  "2022-11-01"),
]

SEGMENTS     = ["Enterprise", "SMB", "Retail", "Government"]
CHANNELS     = ["Direct", "Online", "Distributor", "Retail"]
ORDER_STATUS = ["NEW", "SHIPPED", "DELIVERED", "CANCELLED", "RETURNED"]
MOV_TYPES    = ["RECEIPT", "ISSUE", "TRANSFER_IN", "TRANSFER_OUT", "ADJUSTMENT", "RETURN"]
REASON_CODES = ["DAMAGE", "EXPIRY", "COUNT_ERROR", "THEFT", "CYCLE_COUNT", "SYSTEM_ADJ"]
REF_TYPES    = {"RECEIPT": "PO", "ISSUE": "SO", "TRANSFER_IN": "TO", "TRANSFER_OUT": "TO",
                "ADJUSTMENT": "ADJ", "RETURN": "RM"}

TODAY = datetime.date(2025, 10, 19)
YEAR_AGO = TODAY - datetime.timedelta(days=365)

def generate_products(n: int = 200) -> list[dict]:
    products = []
    cat_list = list(CATEGORIES.items())
    # First 40 are "Pareto winners" (high value)
    for i in range(1, n + 1):
        cat, subs = cat_list[(i - 1) % len(cat_list)]
        sub = subs[(i - 1) % len(subs)]
        brand = BRANDS[(i - 1) % len(BRANDS)]
        uom = UOMS[(i - 1) % len(UOMS)]
        is_pareto = i <= 40                     # top-20% by count
        base_cost = random.uniform(5, 200) if not is_pareto else random.uniform(100, 2000)
        unit_cost = round(base_cost, 2)
        list_price = round(unit_cost * random.uniform(1.3, 2.5), 2)
        # Dead-stock: products 181-190 will have no ISSUE for 120+ days
        products.append({
            "product_id":    i,
            "sku":           f"SKU-{i:04d}",
            "product_name":  f"{brand} {sub} {i:04d}",
            "category":      cat,
            "subcategory":   sub,
            "brand":         brand,
            "uom":           uom,
            "unit_cost":     Decimal(str(unit_cost)),
            "list_price":    Decimal(str(list_price)),
            "reorder_level": random.randint(10, 200),
            "lead_time_days":random.randint(3, 45),
            "is_active":     True,
            "created_at":    datetime.datetime(2015, 1, 1) + datetime.timedelta(days=random.randint(0, 1000)),
            "is_dead_stock": 181 <= i <= 190,
            "is_pareto":     is_pareto,
        })
    return products


def weekly_sundays(start: datetime.date, end: datetime.date) -> list[datetime.date]:
    # Advance to first Sunday on or after start
    d = start + datetime.timedelta(days=(6 - start.weekday()) % 7)
    sundays = []
    while d <= end:
        sundays.append(d)
        d += datetime.timedelta(weeks=1)
    return sundays[-52:]  # last 52 weeks


def q4_multiplier(d: datetime.date) -> float:
    return 2.5 if d.month in (10, 11, 12) else 1.0


def main() -> None:
    print("Connecting …")
    conn = psycopg.connect(DSN)
    conn.autocommit = False
    cur = conn.cursor()

    print("Truncating existing data …")
    cur.execute("TRUNCATE wh.fact_sales_order_line, wh.fact_stock_movement, "
                "wh.fact_inventory_snapshot, wh.dim_product, wh.dim_warehouse RESTART IDENTITY CASCADE")

    # ── Warehouses ─────────────────────────────────────────────────────────────
    print("Seeding dim_warehouse …")
    wh_rows = [
        (w[0], w[1], w[2], w[3], w[4], w[5], w[6], w[7], w[8], w[9], w[10])
        for w in WAREHOUSES
    ]
    copy_rows(cur, "wh.dim_warehouse",
              ["warehouse_id","warehouse_code","warehouse_name","city","state",
               "country","region","capacity_units","manager_name","is_active","opened_date"],
              wh_rows)

    # ── Products ───────────────────────────────────────────────────────────────
    print("Seeding dim_product …")
    products = generate_products(200)
    prod_rows = [
        (p["product_id"], p["sku"], p["product_name"], p["category"], p["subcategory"],
         p["brand"], p["uom"], p["unit_cost"], p["list_price"], p["reorder_level"],
         p["lead_time_days"], p["is_active"], p["created_at"])
        for p in products
    ]
    copy_rows(cur, "wh.dim_product",
              ["product_id","sku","product_name","category","subcategory","brand",
               "uom","unit_cost","list_price","reorder_level","lead_time_days","is_active","created_at"],
              prod_rows)

    product_map = {p["product_id"]: p for p in products}
    warehouse_ids = [w[0] for w in WAREHOUSES]
    sundays = weekly_sundays(YEAR_AGO, TODAY)

    # ── Inventory snapshots ────────────────────────────────────────────────────
    print("Seeding fact_inventory_snapshot …")
    snap_rows = []
    # Track on-hand per (product, warehouse) to maintain a running tally
    on_hand_state: dict[tuple, int] = {}
    for snap_date in sundays:
        for p in products:
            for wh_id in warehouse_ids:
                key = (p["product_id"], wh_id)
                prev = on_hand_state.get(key, random.randint(50, 500))
                # simulate weekly demand
                demand_rate = random.randint(5, 50)
                if p["is_pareto"]:
                    demand_rate *= 3
                demand_rate = int(demand_rate * q4_multiplier(snap_date))
                receipt = random.randint(0, int(demand_rate * 1.2))
                on_hand = max(0, prev - demand_rate + receipt)
                on_hand_state[key] = on_hand

                qty_reserved = int(on_hand * random.uniform(0.05, 0.20))
                qty_in_transit = random.randint(0, int(demand_rate * 0.5))
                avg_cost = float(p["unit_cost"]) * random.uniform(0.95, 1.05)
                avg_cost_d = Decimal(str(round(avg_cost, 2)))

                # days_of_cover
                if demand_rate > 0:
                    doc = round(on_hand / demand_rate * 7, 1) if on_hand > 0 else 0.0
                else:
                    doc = None

                # stock_status
                rl = p["reorder_level"]
                if on_hand == 0:
                    status = "STOCKOUT"
                elif on_hand < rl:
                    status = "LOW"
                elif on_hand > rl * 10:
                    status = "OVERSTOCK"
                else:
                    status = "OK"

                snap_rows.append((
                    snap_date, p["product_id"], wh_id,
                    on_hand, qty_reserved, qty_in_transit,
                    avg_cost_d, doc, status
                ))
    # Note: qty_available and stock_value are GENERATED columns, skip them
    copy_rows(cur, "wh.fact_inventory_snapshot",
              ["snapshot_date","product_id","warehouse_id",
               "qty_on_hand","qty_reserved","qty_in_transit",
               "avg_unit_cost","days_of_cover","stock_status"],
              snap_rows)
    print(f"  → {len(snap_rows):,} snapshot rows")

    # ── Stock movements ─────────────────────────────────────────────────────────
    print("Seeding fact_stock_movement …")
    mov_rows = []
    doc_seq = 100000  # document sequence
    dup_docs: list[str] = []  # intentional duplicates

    start_ts = datetime.datetime.combine(YEAR_AGO, datetime.time(6, 0))
    end_ts   = datetime.datetime.combine(TODAY, datetime.time(23, 59))

    def rand_ts() -> datetime.datetime:
        delta_s = int((end_ts - start_ts).total_seconds())
        return start_ts + datetime.timedelta(seconds=random.randint(0, delta_s))

    # Decide target ~ 100k movements
    TARGET = 100_000
    per_product = math.ceil(TARGET / len(products))

    adj_warehouse = 5  # pattern 4: abnormally high negative ADJUSTMENTs here

    for p in products:
        dead_stock = p["is_dead_stock"]
        for _ in range(random.randint(max(1, per_product - 5), per_product + 5)):
            wh_id = random.choice(warehouse_ids)
            ts = rand_ts()
            mov_date = ts.date()

            # Dead-stock products: no ISSUE in last 120 days
            if dead_stock:
                mv_type = random.choice(["RECEIPT", "TRANSFER_IN", "ADJUSTMENT", "RETURN"])
                # ensure ts is older than 120 days for dead stock logic
                if mv_type == "ISSUE":
                    mv_type = "RECEIPT"
            else:
                # Q4 seasonality: more ISSUEs in Q4
                if mov_date.month in (10, 11, 12):
                    mv_type = random.choices(MOV_TYPES, weights=[15, 40, 10, 10, 15, 10])[0]
                else:
                    mv_type = random.choices(MOV_TYPES, weights=[20, 35, 10, 10, 15, 10])[0]

            # Pattern 4: warehouse 5 gets many negative ADJUSTMENTs
            if wh_id == adj_warehouse and random.random() < 0.12:
                mv_type = "ADJUSTMENT"

            qty_sign = 1 if mv_type in ("RECEIPT", "TRANSFER_IN", "RETURN") else -1
            if mv_type == "ADJUSTMENT":
                # WH5: mostly negative (losses); others: mixed
                if wh_id == adj_warehouse:
                    qty_sign = -1 if random.random() < 0.80 else 1
                else:
                    qty_sign = -1 if random.random() < 0.45 else 1

            qty = qty_sign * random.randint(1, 200)
            uc = float(p["unit_cost"]) * random.uniform(0.95, 1.05)
            uc_d = Decimal(str(round(uc, 2)))

            doc_seq += 1
            ref_doc = f"{REF_TYPES.get(mv_type, 'DOC')}-{doc_seq:07d}"

            # Plant a few duplicate reference_doc values (pattern 7)
            if random.random() < 0.005 and dup_docs:
                ref_doc = random.choice(dup_docs)
            elif random.random() < 0.003:
                dup_docs.append(ref_doc)

            ref_type = REF_TYPES.get(mv_type, "GEN")
            batch = f"BTH-{random.randint(10000, 99999)}" if random.random() > 0.3 else None
            cwh = None
            if mv_type in ("TRANSFER_IN", "TRANSFER_OUT"):
                cwh = random.choice([w for w in warehouse_ids if w != wh_id])

            # reason_code: ~1% NULL (pattern 7) for ADJUSTMENT/RETURN
            rc = None
            if mv_type in ("ADJUSTMENT", "RETURN"):
                rc = None if random.random() < 0.01 else random.choice(REASON_CODES)

            mov_rows.append((
                ts, mov_date, p["product_id"], wh_id, mv_type,
                qty, uc_d, ref_doc, ref_type, batch, cwh, rc
            ))

            if len(mov_rows) >= TARGET:
                break
        if len(mov_rows) >= TARGET:
            break

    copy_rows(cur, "wh.fact_stock_movement",
              ["movement_ts","movement_date","product_id","warehouse_id","movement_type",
               "quantity","unit_cost","reference_doc","reference_type","batch_no",
               "counterpart_warehouse_id","reason_code"],
              mov_rows)
    print(f"  → {len(mov_rows):,} movement rows")

    # ── Sales order lines ───────────────────────────────────────────────────────
    print("Seeding fact_sales_order_line …")
    sol_rows = []
    TARGET_SOL = 60_000
    order_seq  = 1_000_000
    customer_pool = list(range(1, 5001))

    # Stockout products (product_ids 191-200 will have open demand + no stock)
    stockout_pids = set(range(191, 201))

    per_p_sol = math.ceil(TARGET_SOL / len(products))

    for p in products:
        for _ in range(random.randint(max(1, per_p_sol - 3), per_p_sol + 3)):
            order_date = rand_date(YEAR_AGO, TODAY)
            order_id   = f"ORD-{order_seq:08d}"
            order_seq += 1

            seg     = random.choice(SEGMENTS)
            channel = random.choice(CHANNELS)
            wh_id   = random.choice(warehouse_ids)
            qty     = random.randint(1, 50)
            up      = float(p["list_price"]) * random.uniform(0.85, 1.05)
            up_d    = Decimal(str(round(up, 2)))

            # Pattern 5: discount spike in Retail channel in August
            if channel == "Retail" and order_date.month == 8:
                disc = round(random.uniform(25, 45), 2)
            elif p["is_pareto"]:
                disc = round(random.uniform(0, 10), 2)
            else:
                disc = round(random.uniform(0, 20), 2)

            line_amount = Decimal(str(round(qty * float(up_d) * (1 - disc / 100), 2)))
            cost_amount = Decimal(str(round(qty * float(p["unit_cost"]) * random.uniform(0.95, 1.05), 2)))

            # Pattern 1: Q4 seasonality – more pareto sales in Q4
            if p["is_pareto"] and order_date.month in (10, 11, 12):
                qty = int(qty * random.uniform(1.8, 3.0))
                line_amount = Decimal(str(round(qty * float(up_d) * (1 - disc / 100), 2)))
                cost_amount = Decimal(str(round(qty * float(p["unit_cost"]), 2)))

            # Pattern 6: stockout products with open (NEW) demand
            if p["product_id"] in stockout_pids:
                status = "NEW"
                ship_date = None
            else:
                weights = [5, 25, 55, 10, 5]
                status = random.choices(ORDER_STATUS, weights=weights)[0]
                # ship_date: ~1% NULL intentional (pattern 7)
                if status in ("SHIPPED", "DELIVERED") and random.random() > 0.01:
                    ship_date = order_date + datetime.timedelta(days=random.randint(1, 14))
                else:
                    ship_date = None

            sol_rows.append((
                order_id, order_date, ship_date,
                random.choice(customer_pool), seg,
                p["product_id"], wh_id, qty, up_d,
                Decimal(str(disc)), line_amount, cost_amount,
                status, channel
            ))

            if len(sol_rows) >= TARGET_SOL:
                break
        if len(sol_rows) >= TARGET_SOL:
            break

    copy_rows(cur, "wh.fact_sales_order_line",
              ["order_id","order_date","ship_date","customer_id","customer_segment",
               "product_id","warehouse_id","quantity","unit_price","discount_pct",
               "line_amount","cost_amount","order_status","sales_channel"],
              sol_rows)
    print(f"  → {len(sol_rows):,} sales order line rows")

    conn.commit()
    print("\n✓ Seed complete. Running sanity checks …\n")

    # ── Sanity checks ───────────────────────────────────────────────────────────
    checks = [
        ("Row counts",
         """SELECT 'dim_product' tbl, COUNT(*) n FROM wh.dim_product
            UNION ALL SELECT 'dim_warehouse', COUNT(*) FROM wh.dim_warehouse
            UNION ALL SELECT 'fact_inventory_snapshot', COUNT(*) FROM wh.fact_inventory_snapshot
            UNION ALL SELECT 'fact_stock_movement', COUNT(*) FROM wh.fact_stock_movement
            UNION ALL SELECT 'fact_sales_order_line', COUNT(*) FROM wh.fact_sales_order_line
            ORDER BY 1"""),
        ("CHECK violations",
         """SELECT 'snapshot stock_status', COUNT(*) FROM wh.fact_inventory_snapshot
              WHERE stock_status NOT IN ('OK','LOW','STOCKOUT','OVERSTOCK')
            UNION ALL
            SELECT 'movement_type', COUNT(*) FROM wh.fact_stock_movement
              WHERE movement_type NOT IN ('RECEIPT','ISSUE','TRANSFER_IN','TRANSFER_OUT','ADJUSTMENT','RETURN')
            UNION ALL
            SELECT 'order_status', COUNT(*) FROM wh.fact_sales_order_line
              WHERE order_status NOT IN ('NEW','SHIPPED','DELIVERED','CANCELLED','RETURNED')"""),
        ("Pareto check (top-20% SKUs should have >70% revenue)",
         """WITH ranked AS (
              SELECT p.product_id, SUM(s.line_amount) rev,
                     ROW_NUMBER() OVER (ORDER BY SUM(s.line_amount) DESC) rn
              FROM wh.fact_sales_order_line s JOIN wh.dim_product p USING(product_id) GROUP BY 1
            ), totals AS (SELECT SUM(rev) total_rev, COUNT(*) total_skus FROM ranked)
            SELECT
              ROUND(100.0 * SUM(CASE WHEN rn <= total_skus*0.2 THEN rev ELSE 0 END) / MAX(total_rev), 1) top20pct_share
            FROM ranked, totals"""),
        ("Dead stock check (SKU-0181..0190 no ISSUE >=120 days)",
         """SELECT COUNT(*) dead_stock_with_no_issue_120d
            FROM wh.dim_product p
            WHERE p.sku BETWEEN 'SKU-0181' AND 'SKU-0190'
              AND NOT EXISTS (
                SELECT 1 FROM wh.fact_stock_movement m
                WHERE m.product_id = p.product_id
                  AND m.movement_type = 'ISSUE'
                  AND m.movement_date >= CURRENT_DATE - 120)"""),
        ("WH5 negative ADJUSTMENTs vs others",
         """SELECT warehouse_id,
                   COUNT(*) FILTER(WHERE movement_type='ADJUSTMENT' AND quantity<0) neg_adj
            FROM wh.fact_stock_movement GROUP BY 1 ORDER BY 2 DESC LIMIT 4"""),
        ("August Retail discount spike",
         """SELECT sales_channel, EXTRACT(MONTH FROM order_date) mo, ROUND(AVG(discount_pct),2) avg_disc
            FROM wh.fact_sales_order_line
            WHERE sales_channel='Retail'
            GROUP BY 1,2 ORDER BY 3 DESC LIMIT 3"""),
    ]

    all_pass = True
    for name, sql in checks:
        print(f"--- {name} ---")
        cur.execute(sql)
        rows = cur.fetchall()
        for row in rows:
            print(" ", row)
        print()

    cur.close()
    conn.close()
    print("✓ Sanity checks done.")


if __name__ == "__main__":
    main()
