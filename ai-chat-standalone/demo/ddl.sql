-- ============================================================
-- Manufacturing Inventory & Sales Data Warehouse DDL
-- Schema: wh
-- PostgreSQL 16
-- ============================================================

CREATE SCHEMA IF NOT EXISTS wh;

-- ============================================================
-- dim_product
-- ============================================================
CREATE TABLE IF NOT EXISTS wh.dim_product (
    product_id      SERIAL PRIMARY KEY,
    sku             TEXT NOT NULL UNIQUE,
    product_name    TEXT NOT NULL,
    category        TEXT NOT NULL,
    subcategory     TEXT NOT NULL,
    brand           TEXT NOT NULL,
    uom             TEXT NOT NULL,
    unit_cost       NUMERIC(12,2) NOT NULL,
    list_price      NUMERIC(12,2) NOT NULL,
    reorder_level   INT NOT NULL,
    lead_time_days  INT NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  wh.dim_product IS 'Product master dimension: one row per SKU.';
COMMENT ON COLUMN wh.dim_product.product_id      IS 'Surrogate primary key.';
COMMENT ON COLUMN wh.dim_product.sku             IS 'Stock-keeping unit code, business key.';
COMMENT ON COLUMN wh.dim_product.product_name    IS 'Human-readable product name.';
COMMENT ON COLUMN wh.dim_product.category        IS 'Top-level merchandise category (e.g. Electronics, Consumables).';
COMMENT ON COLUMN wh.dim_product.subcategory     IS 'Second-level category.';
COMMENT ON COLUMN wh.dim_product.brand           IS 'Brand or manufacturer name.';
COMMENT ON COLUMN wh.dim_product.uom             IS 'Unit of measure (EA, BOX, KG, etc.).';
COMMENT ON COLUMN wh.dim_product.unit_cost       IS 'Standard landed cost per UOM, USD.';
COMMENT ON COLUMN wh.dim_product.list_price      IS 'Published list price per UOM, USD.';
COMMENT ON COLUMN wh.dim_product.reorder_level   IS 'Qty on-hand threshold that triggers a replenishment order.';
COMMENT ON COLUMN wh.dim_product.lead_time_days  IS 'Average supplier lead time in calendar days.';
COMMENT ON COLUMN wh.dim_product.is_active       IS 'FALSE when the SKU is discontinued.';
COMMENT ON COLUMN wh.dim_product.created_at      IS 'When the product record was first added.';

-- ============================================================
-- dim_warehouse
-- ============================================================
CREATE TABLE IF NOT EXISTS wh.dim_warehouse (
    warehouse_id    SERIAL PRIMARY KEY,
    warehouse_code  TEXT NOT NULL UNIQUE,
    warehouse_name  TEXT NOT NULL,
    city            TEXT NOT NULL,
    state           TEXT NOT NULL,
    country         TEXT NOT NULL,
    region          TEXT NOT NULL,
    capacity_units  INT NOT NULL,
    manager_name    TEXT NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    opened_date     DATE NOT NULL
);

COMMENT ON TABLE  wh.dim_warehouse IS 'Warehouse/distribution-centre master dimension.';
COMMENT ON COLUMN wh.dim_warehouse.warehouse_id   IS 'Surrogate primary key.';
COMMENT ON COLUMN wh.dim_warehouse.warehouse_code IS 'Short alpha code, business key.';
COMMENT ON COLUMN wh.dim_warehouse.warehouse_name IS 'Full site name.';
COMMENT ON COLUMN wh.dim_warehouse.city           IS 'City where the warehouse is located.';
COMMENT ON COLUMN wh.dim_warehouse.state          IS 'State or province.';
COMMENT ON COLUMN wh.dim_warehouse.country        IS 'ISO-3166 country name.';
COMMENT ON COLUMN wh.dim_warehouse.region         IS 'Business region grouping (Northeast, South, Midwest, West).';
COMMENT ON COLUMN wh.dim_warehouse.capacity_units IS 'Maximum storable units at any time.';
COMMENT ON COLUMN wh.dim_warehouse.manager_name   IS 'Site manager full name.';
COMMENT ON COLUMN wh.dim_warehouse.is_active      IS 'FALSE when the site is closed.';
COMMENT ON COLUMN wh.dim_warehouse.opened_date    IS 'Date the warehouse became operational.';

-- ============================================================
-- fact_inventory_snapshot  (weekly grain)
-- ============================================================
CREATE TABLE IF NOT EXISTS wh.fact_inventory_snapshot (
    snapshot_id     BIGSERIAL PRIMARY KEY,
    snapshot_date   DATE NOT NULL,
    product_id      INT NOT NULL REFERENCES wh.dim_product(product_id),
    warehouse_id    INT NOT NULL REFERENCES wh.dim_warehouse(warehouse_id),
    qty_on_hand     INT NOT NULL DEFAULT 0,
    qty_reserved    INT NOT NULL DEFAULT 0,
    qty_available   INT NOT NULL GENERATED ALWAYS AS (qty_on_hand - qty_reserved) STORED,
    qty_in_transit  INT NOT NULL DEFAULT 0,
    avg_unit_cost   NUMERIC(12,2) NOT NULL,
    stock_value     NUMERIC(14,2) NOT NULL GENERATED ALWAYS AS (qty_on_hand * avg_unit_cost) STORED,
    days_of_cover   NUMERIC(8,1),
    stock_status    TEXT NOT NULL CHECK (stock_status IN ('OK','LOW','STOCKOUT','OVERSTOCK'))
);

COMMENT ON TABLE  wh.fact_inventory_snapshot IS 'Weekly point-in-time inventory snapshot for every product×warehouse combination.';
COMMENT ON COLUMN wh.fact_inventory_snapshot.snapshot_id    IS 'Surrogate PK (bigserial).';
COMMENT ON COLUMN wh.fact_inventory_snapshot.snapshot_date  IS 'Week-ending date of the snapshot (grain: weekly, Sunday).';
COMMENT ON COLUMN wh.fact_inventory_snapshot.product_id     IS 'FK to dim_product.';
COMMENT ON COLUMN wh.fact_inventory_snapshot.warehouse_id   IS 'FK to dim_warehouse.';
COMMENT ON COLUMN wh.fact_inventory_snapshot.qty_on_hand    IS 'Total physical units counted in the warehouse.';
COMMENT ON COLUMN wh.fact_inventory_snapshot.qty_reserved   IS 'Units committed to open sales orders.';
COMMENT ON COLUMN wh.fact_inventory_snapshot.qty_available  IS 'Computed: qty_on_hand - qty_reserved.';
COMMENT ON COLUMN wh.fact_inventory_snapshot.qty_in_transit IS 'Units en-route from supplier or from another warehouse.';
COMMENT ON COLUMN wh.fact_inventory_snapshot.avg_unit_cost  IS 'Weighted-average cost per unit at the snapshot date, USD.';
COMMENT ON COLUMN wh.fact_inventory_snapshot.stock_value    IS 'Computed: qty_on_hand * avg_unit_cost, USD.';
COMMENT ON COLUMN wh.fact_inventory_snapshot.days_of_cover  IS 'Estimated days until stock-out based on recent demand rate. NULL when demand is zero.';
COMMENT ON COLUMN wh.fact_inventory_snapshot.stock_status   IS 'Rule-based classification: OK / LOW / STOCKOUT / OVERSTOCK.';

CREATE INDEX ix_fis_product_id    ON wh.fact_inventory_snapshot(product_id);
CREATE INDEX ix_fis_warehouse_id  ON wh.fact_inventory_snapshot(warehouse_id);
CREATE INDEX ix_fis_snapshot_date ON wh.fact_inventory_snapshot(snapshot_date);
CREATE UNIQUE INDEX uix_fis_snap  ON wh.fact_inventory_snapshot(snapshot_date, product_id, warehouse_id);

-- ============================================================
-- fact_stock_movement
-- ============================================================
CREATE TABLE IF NOT EXISTS wh.fact_stock_movement (
    movement_id              BIGSERIAL PRIMARY KEY,
    movement_ts              TIMESTAMPTZ NOT NULL,
    movement_date            DATE NOT NULL,
    product_id               INT NOT NULL REFERENCES wh.dim_product(product_id),
    warehouse_id             INT NOT NULL REFERENCES wh.dim_warehouse(warehouse_id),
    movement_type            TEXT NOT NULL CHECK (movement_type IN ('RECEIPT','ISSUE','TRANSFER_IN','TRANSFER_OUT','ADJUSTMENT','RETURN')),
    quantity                 INT NOT NULL,
    unit_cost                NUMERIC(12,2) NOT NULL,
    total_value              NUMERIC(14,2) NOT NULL GENERATED ALWAYS AS (quantity * unit_cost) STORED,
    reference_doc            TEXT,
    reference_type           TEXT,
    batch_no                 TEXT,
    counterpart_warehouse_id INT REFERENCES wh.dim_warehouse(warehouse_id),
    reason_code              TEXT
);

COMMENT ON TABLE  wh.fact_stock_movement IS 'Every stock movement transaction; ISSUE/RECEIPT/TRANSFER/ADJUSTMENT/RETURN. quantity is signed (negative for outflows).';
COMMENT ON COLUMN wh.fact_stock_movement.movement_id              IS 'Surrogate PK.';
COMMENT ON COLUMN wh.fact_stock_movement.movement_ts              IS 'Exact timestamp the movement was recorded in the WMS.';
COMMENT ON COLUMN wh.fact_stock_movement.movement_date            IS 'Calendar date of movement_ts (denormalised for partitioning/filtering).';
COMMENT ON COLUMN wh.fact_stock_movement.product_id               IS 'FK to dim_product.';
COMMENT ON COLUMN wh.fact_stock_movement.warehouse_id             IS 'FK to dim_warehouse (the origin/receiving warehouse).';
COMMENT ON COLUMN wh.fact_stock_movement.movement_type            IS 'Transaction type: RECEIPT | ISSUE | TRANSFER_IN | TRANSFER_OUT | ADJUSTMENT | RETURN.';
COMMENT ON COLUMN wh.fact_stock_movement.quantity                 IS 'Signed quantity (positive = stock increase, negative = stock decrease).';
COMMENT ON COLUMN wh.fact_stock_movement.unit_cost                IS 'Cost per unit at the time of the movement, USD.';
COMMENT ON COLUMN wh.fact_stock_movement.total_value              IS 'Computed: quantity * unit_cost, USD.';
COMMENT ON COLUMN wh.fact_stock_movement.reference_doc            IS 'Source document number (PO, SO, Transfer ID, etc.). A few intentional duplicates exist for testing.';
COMMENT ON COLUMN wh.fact_stock_movement.reference_type           IS 'Category of reference document (PO / SO / TO / ADJ).';
COMMENT ON COLUMN wh.fact_stock_movement.batch_no                 IS 'Manufacturing or supplier batch/lot number.';
COMMENT ON COLUMN wh.fact_stock_movement.counterpart_warehouse_id IS 'For TRANSFER_IN/OUT: the other warehouse in the transfer. NULL otherwise.';
COMMENT ON COLUMN wh.fact_stock_movement.reason_code              IS 'Reason for ADJUSTMENT or RETURN. ~1% NULLs intentional.';

CREATE INDEX ix_fsm_product_id    ON wh.fact_stock_movement(product_id);
CREATE INDEX ix_fsm_warehouse_id  ON wh.fact_stock_movement(warehouse_id);
CREATE INDEX ix_fsm_movement_date ON wh.fact_stock_movement(movement_date);
CREATE INDEX ix_fsm_movement_type ON wh.fact_stock_movement(movement_type);
CREATE INDEX ix_fsm_cwh_id        ON wh.fact_stock_movement(counterpart_warehouse_id);

-- ============================================================
-- fact_sales_order_line
-- ============================================================
CREATE TABLE IF NOT EXISTS wh.fact_sales_order_line (
    order_line_id    BIGSERIAL PRIMARY KEY,
    order_id         TEXT NOT NULL,
    order_date       DATE NOT NULL,
    ship_date        DATE,
    customer_id      INT NOT NULL,
    customer_segment TEXT NOT NULL,
    product_id       INT NOT NULL REFERENCES wh.dim_product(product_id),
    warehouse_id     INT NOT NULL REFERENCES wh.dim_warehouse(warehouse_id),
    quantity         INT NOT NULL,
    unit_price       NUMERIC(12,2) NOT NULL,
    discount_pct     NUMERIC(5,2) NOT NULL DEFAULT 0,
    line_amount      NUMERIC(14,2) NOT NULL,
    cost_amount      NUMERIC(14,2) NOT NULL,
    order_status     TEXT NOT NULL CHECK (order_status IN ('NEW','SHIPPED','DELIVERED','CANCELLED','RETURNED')),
    sales_channel    TEXT NOT NULL
);

COMMENT ON TABLE  wh.fact_sales_order_line IS 'One row per sales order line item; grain is order_id + line position.';
COMMENT ON COLUMN wh.fact_sales_order_line.order_line_id    IS 'Surrogate PK.';
COMMENT ON COLUMN wh.fact_sales_order_line.order_id         IS 'Source system order header identifier.';
COMMENT ON COLUMN wh.fact_sales_order_line.order_date       IS 'Date the order was placed.';
COMMENT ON COLUMN wh.fact_sales_order_line.ship_date        IS 'Date goods shipped. NULL when not yet shipped (~1% intentional).';
COMMENT ON COLUMN wh.fact_sales_order_line.customer_id      IS 'Customer reference (not a FK to a dim here; customer dim not in scope).';
COMMENT ON COLUMN wh.fact_sales_order_line.customer_segment IS 'Customer classification: Enterprise, SMB, Retail, Government.';
COMMENT ON COLUMN wh.fact_sales_order_line.product_id       IS 'FK to dim_product.';
COMMENT ON COLUMN wh.fact_sales_order_line.warehouse_id     IS 'FK to dim_warehouse (fulfilling warehouse).';
COMMENT ON COLUMN wh.fact_sales_order_line.quantity         IS 'Number of units ordered.';
COMMENT ON COLUMN wh.fact_sales_order_line.unit_price       IS 'Gross list price at time of order, USD.';
COMMENT ON COLUMN wh.fact_sales_order_line.discount_pct     IS 'Percentage discount applied (0-100). Spike in Retail/Online channel in one month planted.';
COMMENT ON COLUMN wh.fact_sales_order_line.line_amount      IS 'Net revenue: quantity * unit_price * (1 - discount_pct/100), USD.';
COMMENT ON COLUMN wh.fact_sales_order_line.cost_amount      IS 'COGS: quantity * unit_cost, USD.';
COMMENT ON COLUMN wh.fact_sales_order_line.order_status     IS 'Order lifecycle state: NEW | SHIPPED | DELIVERED | CANCELLED | RETURNED.';
COMMENT ON COLUMN wh.fact_sales_order_line.sales_channel    IS 'Sales channel: Direct, Online, Distributor, Retail.';

CREATE INDEX ix_fsol_product_id   ON wh.fact_sales_order_line(product_id);
CREATE INDEX ix_fsol_warehouse_id ON wh.fact_sales_order_line(warehouse_id);
CREATE INDEX ix_fsol_order_date   ON wh.fact_sales_order_line(order_date);
CREATE INDEX ix_fsol_order_id     ON wh.fact_sales_order_line(order_id);
CREATE INDEX ix_fsol_order_status ON wh.fact_sales_order_line(order_status);
