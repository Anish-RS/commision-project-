from flask import Flask, jsonify, render_template, request, redirect, url_for, flash
from db import get_connection
from psycopg2.extras import RealDictCursor
from datetime import date, timedelta, datetime, time, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
import traceback
from collections import defaultdict

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "secret123")

@app.route("/")
def home():
    return render_template("home.html")

def to_decimal(x):
    return Decimal(x or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

@app.context_processor
def inject_now():
    return {"now": datetime.now}

def recompute_cash_in_hand_for_date(target_date):
    """
    Recompute totals and closing for target_date and store in cash_in_hand table using Postgres.
    """
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    # date boundaries
    start = datetime.combine(target_date, time.min)
    end = start + timedelta(days=1)

    # receipts (transactions)
    cursor.execute("""
        SELECT COALESCE(t.amount,0) AS amount
        FROM transactions t
        WHERE t.tx_type='receipt' AND t.tx_date >= %s AND t.tx_date < %s
    """, (start, end))
    receipts = cursor.fetchall()
    total_receipts = sum(to_decimal(r["amount"]) for r in receipts) if receipts else to_decimal(0)

    # payments (transactions)
    cursor.execute("""
        SELECT COALESCE(t.amount,0) AS amount
        FROM transactions t
        WHERE t.tx_type='payment' AND t.tx_date >= %s AND t.tx_date < %s
    """, (start, end))
    payments = cursor.fetchall()
    total_payments = sum(to_decimal(p["amount"]) for p in payments) if payments else to_decimal(0)

    # supplier bill extras (transport, paid) for that bill_date (these are payments)
    cursor.execute("""
        SELECT COALESCE(SUM(b.transport),0) AS transport_sum, COALESCE(SUM(b.paid),0) AS paid_sum
        FROM supplier_bills b
        WHERE b.bill_date = %s
    """, (target_date,))
    srow = cursor.fetchone() or {}
    transport_sum = to_decimal(srow.get("transport_sum") or 0)
    paid_sum = to_decimal(srow.get("paid_sum") or 0)

    # labour sum (treat as payments / money out)
    cursor.execute("""
        SELECT COALESCE(SUM(amount),0) AS labour_sum
        FROM labour_entries
        WHERE entry_date = %s
    """, (target_date,))
    lr = cursor.fetchone() or {}
    labour_sum = to_decimal(lr.get("labour_sum") or 0)

    # aggregate payments
    total_payments += transport_sum + paid_sum + labour_sum

    # get yesterday closing to use as opening for target_date
    yesterday = target_date - timedelta(days=1)
    cursor.execute("SELECT closing FROM cash_in_hand WHERE cdate = %s", (yesterday,))
    r = cursor.fetchone()
    opening = to_decimal(r["closing"]) if r and r.get("closing") is not None else to_decimal(0)

    closing = opening + total_receipts - total_payments

    # Postgres implementation of Upsert atomic statement (Assumes 'cdate' has a UNIQUE constraint)
    upsert_sql = """
        INSERT INTO cash_in_hand (cdate, opening, total_receipts, total_payments, closing)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (cdate) 
        DO UPDATE SET 
            opening = EXCLUDED.opening,
            total_receipts = EXCLUDED.total_receipts,
            total_payments = EXCLUDED.total_payments,
            closing = EXCLUDED.closing
    """
    cursor.execute(upsert_sql, (target_date, opening, total_receipts, total_payments, closing))

    conn.commit()
    cursor.close()
    conn.close()

    return {
        "opening": opening,
        "receipts": total_receipts,
        "payments": total_payments,
        "closing": closing
    }

@app.route("/customers/add", methods=["GET", "POST"])
def add_customer():
    if request.method == "POST":
        name = request.form["name"]
        phone = request.form.get("phone")
        address = request.form.get("address")
        opening_balance = request.form.get("opening_balance", 0)

        conn = get_connection()
        cursor = conn.cursor()
        sql = "INSERT INTO customers (name, phone, address, opening_balance) VALUES (%s, %s, %s, %s)"
        cursor.execute(sql, (name, phone, address, opening_balance))
        conn.commit()
        cursor.close()
        conn.close()

        flash("Customer added successfully!", "success")
        return redirect(url_for("add_customer"))

    return render_template("add_customer.html")

@app.route("/suppliers/add", methods=["GET", "POST"])
def add_supplier():
    if request.method == "POST":
        name = request.form["name"]
        phone = request.form.get("phone")
        address = request.form.get("address")
        opening_balance = request.form.get("opening_balance", 0)

        conn = get_connection()
        cursor = conn.cursor()
        sql = "INSERT INTO suppliers (name, phone, address, opening_balance) VALUES (%s, %s, %s, %s)"
        cursor.execute(sql, (name, phone, address, opening_balance))
        conn.commit()
        cursor.close()
        conn.close()

        flash("Supplier added successfully!", "success")
        return redirect(url_for("add_supplier"))

    return render_template("add_supplier.html")

@app.route("/bills/supplier/add", methods=["GET", "POST"])
def add_supplier_bill():
    if request.method == "POST":
        conn = get_connection()
        cursor = conn.cursor()

        raw_supplier_id = request.form.get("supplier_id")
        if not raw_supplier_id:
            flash("Please select a supplier before creating the bill.", "danger")
            return redirect(url_for("add_supplier_bill"))

        try:
            supplier_id = int(raw_supplier_id)
        except ValueError:
            flash("Invalid supplier id.", "danger")
            return redirect(url_for("add_supplier_bill"))

        bill_date = request.form.get("bill_date") or date.today().isoformat()

        try:
            commission = float(request.form.get("commission") or 0)
            labour = float(request.form.get("labour") or 0)
            transport = float(request.form.get("transport") or 0)
            paid = float(request.form.get("paid") or 0)
        except ValueError:
            flash("Numeric values must be valid numbers.", "danger")
            return redirect(url_for("add_supplier_bill"))

        customer_ids = request.form.getlist("customer_id[]")
        quantities = request.form.getlist("quantity[]")
        rates = request.form.getlist("rate[]")

        items = []
        total_amount = 0.0

        for cid, q, r in zip(customer_ids, quantities, rates):
            if not cid:
                continue
            try:
                qty = float(q or 0)
                rate = float(r or 0)
            except ValueError:
                qty, rate = 0.0, 0.0
            if qty == 0 and rate == 0:
                continue
            amount = qty * rate
            total_amount += amount
            items.append({
                "customer_id": int(cid),
                "quantity": qty,
                "rate": rate,
                "amount": amount
            })

        bill_balance = total_amount - commission - labour - transport - paid

        try:
            cursor.execute("SELECT balance FROM suppliers WHERE supplier_id = %s", (supplier_id,))
            row = cursor.fetchone()
            old_supplier_balance = float(row[0]) if row and row[0] is not None else 0.0
            new_supplier_balance = old_supplier_balance - bill_balance

            previous_owed = abs(old_supplier_balance) if old_supplier_balance < 0 else 0.0
            resulting_owed = abs(new_supplier_balance) if new_supplier_balance < 0 else 0.0

            sql_header = """
                INSERT INTO supplier_bills
                (supplier_id, bill_date, total_amount, commission, transport, labour, paid, balance, old_balance, final_balance)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING bill_id
            """
            cursor.execute(sql_header, (
                supplier_id, bill_date, total_amount, commission, transport, labour, paid, bill_balance,
                old_supplier_balance, new_supplier_balance
            ))
            bill_id = cursor.fetchone()[0]

            cursor.execute("UPDATE suppliers SET balance = %s WHERE supplier_id = %s", (new_supplier_balance, supplier_id))

            sql_item = "INSERT INTO supplier_bill_items (bill_id, customer_id, quantity, rate, amount) VALUES (%s, %s, %s, %s, %s)"
            sql_cb = """
                INSERT INTO customer_bills
                (customer_id, bill_date, quantity, rate, amount, commission, net_amount, supplier_bill_id, old_balance, final_balance)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """

            for it in items:
                cid = it["customer_id"]
                qty = it["quantity"]
                rate = it["rate"]
                amount = it["amount"]

                cursor.execute(sql_item, (bill_id, cid, qty, rate, amount))

                cursor.execute("SELECT balance FROM customers WHERE customer_id = %s", (cid,))
                crow = cursor.fetchone()
                cust_old = float(crow[0]) if crow and crow[0] is not None else 0.0
                cust_new = cust_old + amount

                cursor.execute(sql_cb, (cid, bill_date, qty, rate, amount, 0, amount, bill_id, cust_old, cust_new))
                cursor.execute("UPDATE customers SET balance = %s WHERE customer_id = %s", (cust_new, cid))

            conn.commit()
        except Exception as e:
            conn.rollback()
            app.logger.exception("Failed creating supplier bill")
            flash(f"Database error: {e}", "danger")
            return redirect(url_for("add_supplier_bill"))
        finally:
            cursor.close()
            conn.close()

        flash(f"Supplier bill #{bill_id} successfully created.", "success")
        return redirect(url_for("add_supplier_bill"))

    # GET
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT supplier_id AS id, name, balance FROM suppliers ORDER BY name ASC")
    suppliers = cursor.fetchall()
    cursor.execute("SELECT customer_id AS id, name FROM customers ORDER BY name ASC")
    customers = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template("supplier_bill.html", today=date.today().isoformat(), suppliers=suppliers, customers=customers)

@app.route("/ledger")
def ledger():
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT supplier_id, name, balance FROM suppliers WHERE balance <> 0 ORDER BY name")
    suppliers = cursor.fetchall()
    cursor.execute("SELECT customer_id, name, balance FROM customers WHERE balance <> 0 ORDER BY name")
    customers = cursor.fetchall()
    cursor.close()
    conn.close()

    supplier_credit, supplier_debit = [], []
    supplier_credit_total, supplier_debit_total = 0.0, 0.0
    for s in suppliers:
        bal = float(s["balance"] or 0.0)
        if bal > 0:
            supplier_credit.append({"id": s["supplier_id"], "name": s["name"], "amount": bal})
            supplier_credit_total += bal
        else:
            supplier_debit.append({"id": s["supplier_id"], "name": s["name"], "amount": abs(bal)})
            supplier_debit_total += abs(bal)

    customer_credit, customer_debit = [], []
    customer_credit_total, customer_debit_total = 0.0, 0.0
    for c in customers:
        bal = float(c["balance"] or 0.0)
        if bal > 0:
            customer_credit.append({"id": c["customer_id"], "name": c["name"], "amount": bal})
            customer_credit_total += bal
        else:
            customer_debit.append({"id": c["customer_id"], "name": c["name"], "amount": abs(bal)})
            customer_debit_total += abs(bal)

    return render_template(
        "ledger.html", supplier_credit=supplier_credit, supplier_debit=supplier_debit,
        supplier_credit_total=supplier_credit_total, supplier_debit_total=supplier_debit_total,
        customer_credit=customer_credit, customer_debit=customer_debit,
        customer_credit_total=customer_credit_total, customer_debit_total=customer_debit_total
    )

@app.route("/api/supplier/<int:supplier_id>/balance")
def get_supplier_balance(supplier_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT balance FROM suppliers WHERE supplier_id = %s", (supplier_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    old_balance = float(row[0]) if row else 0.0
    return {"old_balance": old_balance}

@app.route("/tally", methods=["GET", "POST"])
def tally():
    supplierData, customerData = [], []
    totalSupplier, totalCustomer, difference = 0, 0, 0
    from_date, to_date = "", ""

    if request.method == "POST":
        from_date = request.form.get("from_date")
        to_date = request.form.get("to_date")

        if not from_date or not to_date:
            flash("Please select both FROM and TO dates.", "danger")
            return render_template("tally.html")

        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        to_dt = datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1)

        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT supplier_id, total_amount AS amount FROM supplier_bills WHERE bill_date >= %s AND bill_date < %s", (from_dt, to_dt))
        supplierData = cursor.fetchall()
        cursor.execute("SELECT customer_id, amount FROM customer_bills WHERE bill_date >= %s AND bill_date < %s", (from_dt, to_dt))
        customerData = cursor.fetchall()
        cursor.close()
        conn.close()

        totalSupplier = sum(x["amount"] for x in supplierData)
        totalCustomer = sum(x["amount"] for x in customerData)
        difference = totalCustomer - totalSupplier

    return render_template(
        "tally.html", supplierData=supplierData, customerData=customerData,
        totalSupplier=totalSupplier, totalCustomer=totalCustomer, difference=difference,
        from_date=from_date, to_date=to_date
    )

def apply_transaction(entity_type, entity_id, tx_type, amount, note, tx_date):
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if entity_type == "customer":
            cursor.execute("SELECT customer_id, name, balance FROM customers WHERE customer_id = %s", (entity_id,))
            row = cursor.fetchone()
            if not row: return False, "Customer not found", None
            old_bal = float(row["balance"] or 0.0)
            new_bal = old_bal - amount if tx_type == "receipt" else old_bal + amount
            cursor.execute("UPDATE customers SET balance = %s WHERE customer_id = %s", (new_bal, entity_id))

        elif entity_type == "supplier":
            cursor.execute("SELECT supplier_id, name, balance FROM suppliers WHERE supplier_id = %s", (entity_id,))
            row = cursor.fetchone()
            if not row: return False, "Supplier not found", None
            old_bal = float(row["balance"] or 0.0)
            new_bal = old_bal - amount if tx_type == "receipt" else old_bal + amount
            cursor.execute("UPDATE suppliers SET balance = %s WHERE supplier_id = %s", (new_bal, entity_id))
        else:
            return False, "Invalid entity type", None

        cursor.execute("""
            INSERT INTO transactions (entity_type, entity_id, tx_type, amount, note, tx_date)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (entity_type, entity_id, tx_type, amount, note, tx_date))
        conn.commit()
        return True, "Transaction applied", {"old_balance": old_bal, "new_balance": new_bal, "entity": row}
    except Exception as e:
        conn.rollback()
        return False, str(e), None
    finally:
        cursor.close()
        conn.close()

def get_entity_id_from_raw(conn, raw_value, entity_type):
    raw = (raw_value or "").strip()
    if not raw: return None, None
    try:
        eid = int(raw)
        if eid > 0: return eid, None
    except (ValueError, TypeError):
        pass

    m = re.search(r'\b(\d+)\b', raw)
    if m:
        try: return int(m.group(1)), None
        except: pass

    cur = conn.cursor(cursor_factory=RealDictCursor)
    if entity_type == "customer":
        cur.execute("SELECT customer_id AS id, name FROM customers WHERE name ILIKE %s ORDER BY name LIMIT 1", (f"%{raw}%",))
    else:
        cur.execute("SELECT supplier_id AS id, name FROM suppliers WHERE name ILIKE %s ORDER BY name LIMIT 1", (f"%{raw}%",))
    row = cur.fetchone()
    cur.close()
    if row: return int(row["id"]), row["name"]
    return None, None

@app.route("/transactions/receipt", methods=["GET", "POST"])
def receipt_page():
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    if request.method == "POST":
        entity_type = request.form.get("entity_type")
        raw_input = (request.form.get("entity_id") or request.form.get("entity_name") or "").strip()
        entity_id, _ = get_entity_id_from_raw(conn, raw_input, entity_type)

        if not entity_id:
            flash("Entity not found.", "warning")
            cursor.close(); conn.close()
            return redirect(url_for("receipt_page"))

        amount = float(request.form.get("amount") or 0)
        note = request.form.get("note")
        tx_date = request.form.get("tx_date")

        ok, msg, data = apply_transaction(entity_type, entity_id, "receipt", amount, note, tx_date)
        if not ok: flash(f"Error: {msg}", "danger")
        else: flash(f"Receipt saved. New balance: {data['new_balance']}", "success")
        cursor.close(); conn.close()
        return redirect(url_for("receipt_page"))

    cursor.execute("SELECT customer_id AS id, name FROM customers ORDER BY name ASC")
    customers = cursor.fetchall()
    cursor.execute("SELECT supplier_id AS id, name FROM suppliers ORDER BY name ASC")
    suppliers = cursor.fetchall()
    cursor.close(); conn.close()
    return render_template("receipt.html", customers=customers, suppliers=suppliers)

@app.route("/transactions/payment", methods=["GET", "POST"])
def payment_page():
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    if request.method == "POST":
        entity_type = request.form.get("entity_type")
        raw_input = (request.form.get("entity_id") or request.form.get("entity_name") or "").strip()
        entity_id, _ = get_entity_id_from_raw(conn, raw_input, entity_type)

        if not entity_id:
            flash("Entity not found.", "warning")
            cursor.close(); conn.close()
            return redirect(url_for("payment_page"))

        amount = float(request.form.get("amount") or 0)
        note = request.form.get("note")
        tx_date = request.form.get("tx_date")

        ok, msg, data = apply_transaction(entity_type, entity_id, "payment", amount, note, tx_date)
        if not ok: flash(f"Error: {msg}", "danger")
        else: flash(f"Payment saved. New balance: {data['new_balance']}", "success")
        cursor.close(); conn.close()
        return redirect(url_for("payment_page"))

    cursor.execute("SELECT customer_id AS id, name FROM customers ORDER BY name ASC")
    customers = cursor.fetchall()
    cursor.execute("SELECT supplier_id AS id, name FROM suppliers ORDER BY name ASC")
    suppliers = cursor.fetchall()
    cursor.close(); conn.close()
    return render_template("payment.html", customers=customers, suppliers=suppliers)

@app.route("/cash_in_hand")
def cash_in_hand():
    today = date.today()
    today_start = datetime.combine(today, time.min)
    tomorrow_start = today_start + timedelta(days=1)
    yesterday = today - timedelta(days=1)

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("""
        SELECT t.amount, COALESCE(c.name, s.name) AS name
        FROM transactions t
        LEFT JOIN customers c ON (t.entity_type='customer' AND t.entity_id=c.customer_id)
        LEFT JOIN suppliers s ON (t.entity_type='supplier' AND t.entity_id=s.supplier_id)
        WHERE t.tx_type='receipt' AND t.tx_date >= %s AND t.tx_date < %s
        ORDER BY t.tx_id DESC
    """, (today_start, tomorrow_start))
    todays_receipts = cursor.fetchall()

    cursor.execute("""
        SELECT t.amount, COALESCE(c.name, s.name) AS name
        FROM transactions t
        LEFT JOIN customers c ON (t.entity_type='customer' AND t.entity_id=c.customer_id)
        LEFT JOIN suppliers s ON (t.entity_type='supplier' AND t.entity_id=s.supplier_id)
        WHERE t.tx_type='payment' AND t.tx_date >= %s AND t.tx_date < %s
        ORDER BY t.tx_id DESC
    """, (today_start, tomorrow_start))
    todays_payments = cursor.fetchall()

    cursor.execute("""
        SELECT s.name, b.transport, b.paid
        FROM supplier_bills b
        JOIN suppliers s ON b.supplier_id = s.supplier_id
        WHERE b.bill_date = %s
    """, (today,))
    supplier_bill_entries = cursor.fetchall()

    supplier_extra = []
    for row in supplier_bill_entries:
        tr = row.get("transport") or 0
        pd = row.get("paid") or 0
        if float(tr) != 0: supplier_extra.append({"name": f"{row['name']} (transport)", "amount": float(tr)})
        if float(pd) != 0: supplier_extra.append({"name": f"{row['name']} (paid)", "amount": float(pd)})

    cursor.execute("SELECT amount, note FROM labour_entries WHERE entry_date = %s", (today,))
    labour_rows = cursor.fetchall()
    labour_entries = [{"name": "Labour" + (f" - {lr['note']}" if lr['note'] else ""), "amount": float(lr['amount'] or 0)} for lr in labour_rows]

    all_payments = todays_payments + supplier_extra + labour_entries
    total_receipts = sum(float(r.get("amount") or 0) for r in todays_receipts)
    total_payments = sum(float(p.get("amount") or 0) for p in all_payments)

    cursor.execute("SELECT closing FROM cash_in_hand WHERE cdate = %s", (yesterday,))
    row = cursor.fetchone()
    opening = float(row["closing"]) if row and row.get("closing") is not None else 0.0
    closing = opening + total_receipts - total_payments

    upsert_sql = """
        INSERT INTO cash_in_hand (cdate, opening, total_receipts, total_payments, closing)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (cdate) 
        DO UPDATE SET opening=%s, total_receipts=%s, total_payments=%s, closing=%s
    """
    cursor.execute(upsert_sql, (today, opening, total_receipts, total_payments, closing, opening, total_receipts, total_payments, closing))
    conn.commit()
    cursor.close(); conn.close()

    return render_template("cash_in_hand.html", todays_receipts=todays_receipts, all_payments=all_payments, total_receipts=total_receipts, total_payments=total_payments, opening=opening, closing=closing)

@app.route("/find", methods=["GET"])
def find_page():
    q = request.args.get("q", "").strip()
    mode = request.args.get("mode", "supplier")
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    if mode == "supplier":
        if q:
            cursor.execute("SELECT supplier_id AS id, name, balance FROM suppliers WHERE name ILIKE %s ORDER BY name ASC", ("%" + q + "%",))
        else:
            cursor.execute("SELECT supplier_id AS id, name, balance FROM suppliers ORDER BY name ASC")
    else:
        if q:
            cursor.execute("SELECT customer_id AS id, name, balance FROM customers WHERE name ILIKE %s ORDER BY name ASC", ("%" + q + "%",))
        else:
            cursor.execute("SELECT customer_id AS id, name, balance FROM customers ORDER BY name ASC")
            
    results = cursor.fetchall()
    cursor.close(); conn.close()
    return render_template("find.html", mode=mode, q=q, results=results)

@app.route("/bills/supplier/search")
def supplier_bill_search():
    name = request.args.get("name", "").strip()
    bill_date = request.args.get("bill_date", "").strip()
    bill_no = request.args.get("bill_no", "").strip()

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    results = []

    if bill_no:
        cursor.execute("""
            SELECT b.*, s.name AS supplier_name FROM supplier_bills b 
            JOIN suppliers s ON b.supplier_id = s.supplier_id WHERE b.bill_id = %s
        """, (int(bill_no),))
        row = cursor.fetchone()
        if row: results = [row]
    else:
        sql = "SELECT b.*, s.name AS supplier_name FROM supplier_bills b JOIN suppliers s ON b.supplier_id = s.supplier_id WHERE 1=1"
        params = []
        if name:
            sql += " AND s.name ILIKE %s"
            params.append(f"%{name}%")
        if bill_date:
            sql += " AND b.bill_date = %s"
            params.append(bill_date)
        cursor.execute(sql, params)
        results = cursor.fetchall()

    for r in results:
        # Changed CAST(... AS VARCHAR) to Postgres-supported CAST(... AS VARCHAR)
        cursor.execute("""
            SELECT it.*, c.name AS customer_name FROM supplier_bill_items it
            LEFT JOIN customers c ON it.customer_id = c.customer_id
            WHERE it.bill_id = %s ORDER BY COALESCE(c.name, CAST(it.customer_id AS VARCHAR))
        """, (r["bill_id"],))
        r["items"] = cursor.fetchall()

    cursor.close(); conn.close()
    return render_template("supplier_bill_search.html", results=results)

@app.route("/bills/customer/search", methods=["GET", "POST"])
def customer_bill_search():

    if request.method == "POST":
        # Your existing search code should be here
        pass
    return render_template("customer_bill_search.html")

if __name__ == "__main__":
    app.run(debug=True)

# ==== ROUTES MERGED FROM OLD APP ====

@app.route("/bills/supplier/edit/<int:bill_id>", methods=["GET","POST"])
def supplier_bill_edit(bill_id):
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    # fetch bill header, items, supplier old/final balances
    cursor.execute("SELECT * FROM supplier_bills WHERE bill_id = %s", (bill_id,))
    bill = cursor.fetchone()
    if not bill:
        cursor.close(); conn.close()
        flash("Bill not found", "danger")
        return redirect(url_for("supplier_bill_search"))

    cursor.execute("SELECT * FROM supplier_bill_items WHERE bill_id = %s", (bill_id,))
    items = cursor.fetchall()

    # fetch customer bills referencing this supplier bill
    cursor.execute("SELECT * FROM customer_bills WHERE supplier_bill_id = %s", (bill_id,))
    cust_bills = cursor.fetchall()

    if request.method == "POST":
        try:
            # start transaction only if not already started
           

            # --- collect new header values ---
            commission = to_decimal(request.form.get("commission") or 0)
            labour = to_decimal(request.form.get("labour") or 0)
            transport = to_decimal(request.form.get("transport") or 0)
            paid = to_decimal(request.form.get("paid") or 0)
            bill_date_str = request.form.get("bill_date") or bill["bill_date"]

            # convert bill_date_str to date object
            if isinstance(bill_date_str, str):
                new_bill_date = datetime.strptime(bill_date_str, "%Y-%m-%d").date()
            elif isinstance(bill_date_str, date):
                new_bill_date = bill_date_str
            else:
                new_bill_date = bill["bill_date"]

            old_bill_date = bill["bill_date"] if isinstance(bill["bill_date"], date) else datetime.strptime(str(bill["bill_date"]), "%Y-%m-%d").date()

            # collect new line items (arrays)
            customer_ids = request.form.getlist("customer_id[]")
            quantities = request.form.getlist("quantity[]")
            rates = request.form.getlist("rate[]")

            # build new items list and compute new total_amount
            new_items = []
            new_total_amount = to_decimal(0)
            for cid, q, r in zip(customer_ids, quantities, rates):
                if not cid:
                    continue
                qty = to_decimal(q or 0)
                rate = to_decimal(r or 0)
                amount = (qty * rate).quantize(Decimal("0.01"))
                new_items.append({"customer_id": int(cid), "quantity": qty, "rate": rate, "amount": amount})
                new_total_amount += amount

            # compute new bill_balance using your formula
            new_bill_balance = (new_total_amount - commission - labour - transport - paid).quantize(Decimal("0.01"))

            # --- handle supplier balance update ---
            old_final_signed = to_decimal(bill.get("final_balance") or 0)
            old_header_old_balance = to_decimal(bill.get("old_balance") or 0)

            total_due_positive = (abs(old_header_old_balance) + new_bill_balance).quantize(Decimal("0.01"))
            new_final_signed = - total_due_positive

            delta_supplier_signed = new_final_signed - old_final_signed

            # update supplier_bills header
            cursor.execute("""
                UPDATE supplier_bills
                SET bill_date=%s, total_amount=%s, commission=%s, transport=%s, labour=%s, paid=%s, balance=%s, final_balance=%s
                WHERE bill_id=%s
            """, (new_bill_date, float(new_total_amount), float(commission), float(transport), float(labour), float(paid), float(new_bill_balance), float(new_final_signed), bill_id))

            # update supplier running balance by delta
            cursor.execute("UPDATE suppliers SET balance = balance + %s WHERE supplier_id = %s", (float(delta_supplier_signed), bill["supplier_id"]))

            # --- items & customer bills & customer balances ---
            cursor.execute("SELECT customer_id, SUM(amount) as total_amount FROM supplier_bill_items WHERE bill_id = %s GROUP BY customer_id", (bill_id,))
            old_customer_sums = {row["customer_id"]: to_decimal(row["total_amount"]) for row in cursor.fetchall()}

            # delete old child rows
            cursor.execute("DELETE FROM supplier_bill_items WHERE bill_id = %s", (bill_id,))
            cursor.execute("DELETE FROM customer_bills WHERE supplier_bill_id = %s", (bill_id,))

            # insert new items and customer bills and update customers' balances
            for it in new_items:
                cid = it["customer_id"]
                qty = it["quantity"]
                rate = it["rate"]
                amount = it["amount"]

                cursor.execute("INSERT INTO supplier_bill_items (bill_id, customer_id, quantity, rate, amount) VALUES (%s,%s,%s,%s,%s)",
                               (bill_id, cid, float(qty), float(rate), float(amount)))

                old_amt = old_customer_sums.get(cid, to_decimal(0))
                new_amt = amount
                delta_cust = new_amt - old_amt

                cursor.execute("""
                    INSERT INTO customer_bills (customer_id, bill_date, quantity, rate, amount,
                        commission, net_amount, supplier_bill_id, old_balance, final_balance)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (cid, new_bill_date, float(qty), float(rate), float(amount), 0.0, float(amount), bill_id, 0.0, 0.0))

                cursor.execute("UPDATE customers SET balance = balance + %s WHERE customer_id = %s", (float(delta_cust), cid))

            conn.commit()

        except Exception as e:
            conn.rollback()
            cursor.close()
            conn.close()
            flash("Error updating supplier bill: " + str(e), "danger")
            return redirect(url_for("supplier_bill_edit", bill_id=bill_id))

        finally:
            # ensure cursor/conn closed if still open
            try:
                cursor.close()
                conn.close()
            except:
                pass

        # recompute cash_in_hand for old date, new date, and today if needed
        try:
            recompute_cash_in_hand_for_date(old_bill_date)
        except Exception:
            pass
        try:
            recompute_cash_in_hand_for_date(new_bill_date)
        except Exception:
            pass
        if new_bill_date != date.today():
            recompute_cash_in_hand_for_date(date.today())

        flash("Supplier bill updated successfully", "success")
        return redirect(url_for("supplier_bill_search"))

    # GET -> render edit form
    cursor.close()
    conn.close()
    return render_template("supplier_bill_edit.html", bill=bill, items=items, cust_bills=cust_bills)


@app.route("/bills/customer/edit/<int:bill_id>", methods=["GET","POST"])
def customer_bill_edit(bill_id):
    conn = get_connection(); cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM customer_bills WHERE bill_id = %s", (bill_id,))
    bill = cursor.fetchone()
    if not bill:
        cursor.close(); conn.close()
        flash("Not found","danger")
        return redirect(url_for("customer_bill_search"))

    if request.method == "POST":
        try:
            # start transaction only if not already started
            

            qty = to_decimal(request.form.get("quantity") or 0)
            rate = to_decimal(request.form.get("rate") or 0)
            new_amount = (qty * rate).quantize(Decimal("0.01"))
            old_amount = to_decimal(bill["amount"])
            delta = new_amount - old_amount

            cursor.execute("UPDATE customer_bills SET quantity=%s, rate=%s, amount=%s WHERE bill_id=%s",
                           (float(qty), float(rate), float(new_amount), bill_id))
            cursor.execute("UPDATE customers SET balance = balance + %s WHERE customer_id = %s", (float(delta), bill["customer_id"]))

            conn.commit()
        except Exception as e:
            conn.rollback()
            cursor.close()
            conn.close()
            flash("Error updating customer bill: " + str(e), "danger")
            return redirect(url_for("customer_bill_edit", bill_id=bill_id))
        finally:
            try:
                cursor.close(); conn.close()
            except:
                pass

        # recompute cash_in_hand for bill date and today
        bill_date_val = bill["bill_date"]
        if isinstance(bill_date_val, str):
            bill_date_val = datetime.strptime(bill_date_val, "%Y-%m-%d").date()
        recompute_cash_in_hand_for_date(bill_date_val)
        if bill_date_val != date.today():
            recompute_cash_in_hand_for_date(date.today())

        flash("Customer bill updated","success")
        return redirect(url_for("customer_bill_search"))

    cursor.close(); conn.close()
    return render_template("customer_bill_edit.html", bill=bill)

@app.route("/labour/add", methods=["GET", "POST"])
def add_labour():
    today = date.today()  # fixed date (only today)

    if request.method == "POST":
        amount = float(request.form.get("amount") or 0)
        note = request.form.get("note") or ""

        conn = get_connection()
        cursor = conn.cursor()

        # Always insert using today's date (no user input)
        cursor.execute("""
            INSERT INTO labour_entries (entry_date, amount, note)
            VALUES (%s, %s, %s)
        """, (today, amount, note))

        conn.commit()
        cursor.close()
        conn.close()

        # recompute cash in hand for today only
        try:
            recompute_cash_in_hand_for_date(today)
        except Exception:
            pass

        flash("Labour entry saved for today", "success")
        return redirect(url_for("add_labour"))

    # GET
    return render_template("labour_add.html", today=today.isoformat())

from datetime import date, datetime


@app.route("/profit-loss", methods=["GET"])
def profit_loss():
    # optional date range params: ?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD
    from_str = request.args.get("from_date")
    to_str = request.args.get("to_date")

    today = date.today()

    try:
        from_date = datetime.strptime(from_str, "%Y-%m-%d").date() if from_str else today
    except Exception:
        from_date = today

    try:
        to_date = datetime.strptime(to_str, "%Y-%m-%d").date() if to_str else today
    except Exception:
        to_date = today

    # ensure from_date <= to_date
    if from_date > to_date:
        from_date, to_date = to_date, from_date

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    # 1) total commission from supplier_bills for that period -> profit
    cursor.execute("""
        SELECT COALESCE(SUM(commission),0) AS total_comm
        FROM supplier_bills
        WHERE bill_date BETWEEN %s AND %s
    """, (from_date, to_date))
    row = cursor.fetchone()
    total_commission = float(row["total_comm"]) if row and row.get("total_comm") is not None else 0.0

    # 2) total labour (from labour_entries) for that period -> loss
    cursor.execute("""
        SELECT COALESCE(SUM(amount),0) AS total_labour
        FROM labour_entries
        WHERE entry_date BETWEEN %s AND %s
    """, (from_date, to_date))
    row = cursor.fetchone()
    total_labour = float(row["total_labour"]) if row and row.get("total_labour") is not None else 0.0

    # 3) labour from supplier_bills (if you record it there)
    cursor.execute("""
        SELECT COALESCE(SUM(labour),0) AS supplier_labour
        FROM supplier_bills
        WHERE bill_date BETWEEN %s AND %s
    """, (from_date, to_date))
    row2 = cursor.fetchone()
    supplier_labour = float(row2["supplier_labour"]) if row2 and row2.get("supplier_labour") is not None else 0.0

    # profit side = commission + supplier_labour (as you defined)
    profit = total_commission + supplier_labour

    # net = profit - loss
    net = profit - total_labour

    cursor.close()
    conn.close()

    return render_template(
        "profit_loss.html",
        from_date=from_date,
        to_date=to_date,
        total_commission=total_commission,
        total_labour=total_labour,
        supplier_labour=supplier_labour,
        profit=profit,
        net=net
    )


@app.route("/bills/supplier/delete/<int:bill_id>", methods=["POST"])
def supplier_bill_delete(bill_id):
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # start transaction if not already in one
        

        # fetch header
        cursor.execute("SELECT * FROM supplier_bills WHERE bill_id = %s", (bill_id,))
        bill = cursor.fetchone()
        if not bill:
            conn.rollback()
            cursor.close(); conn.close()
            flash("Supplier bill not found", "danger")
            return redirect(url_for("supplier_bill_search"))

        supplier_id = bill["supplier_id"]
        bill_date = bill["bill_date"]

        # store header signed values (use to_decimal helper)
        old_header_old_signed = to_decimal(bill.get("old_balance") or 0)   # signed (snapshot before bill)
        new_final_signed = to_decimal(bill.get("final_balance") or 0)     # signed (snapshot after bill)
        # note: some rows store `balance` (bill_balance) too: bill_balance = bill.get("balance")

        # --- 1) Per-customer amounts for this supplier bill ---
        cursor.execute("""
            SELECT customer_id, SUM(amount) AS total_amount
            FROM supplier_bill_items
            WHERE bill_id = %s
            GROUP BY customer_id
        """, (bill_id,))
        per_customer = cursor.fetchall()  # list of dicts

        # For each customer: subtract the amount that was previously added when bill was created
        for row in per_customer:
            cid = row["customer_id"]
            amt = to_decimal(row["total_amount"] or 0)
            # Subtract the amount from customer's running balance (reverse creation)
            cursor.execute("UPDATE customers SET balance = balance - %s WHERE customer_id = %s", (float(amt), cid))

        # --- 2) Reverse supplier's running balance by applying delta: old - new ---
        # When the bill was created, supplier.balance changed by (new_final_signed - old_header_old_signed).
        # To undo, apply (old_header_old_signed - new_final_signed).
        delta_supplier_to_apply = (old_header_old_signed - new_final_signed).quantize(Decimal("0.01"))

        cursor.execute("UPDATE suppliers SET balance = balance + %s WHERE supplier_id = %s", (float(delta_supplier_to_apply), supplier_id))

        # --- 3) Delete dependent rows (items and customer_bills) then the header ---
        cursor.execute("DELETE FROM supplier_bill_items WHERE bill_id = %s", (bill_id,))
        cursor.execute("DELETE FROM customer_bills WHERE supplier_bill_id = %s", (bill_id,))
        cursor.execute("DELETE FROM supplier_bills WHERE bill_id = %s", (bill_id,))

        conn.commit()

    except Exception as e:
        conn.rollback()
        cursor.close(); conn.close()
        flash("Error deleting supplier bill: " + str(e), "danger")
        return redirect(url_for("supplier_bill_search"))
    finally:
        try:
            cursor.close(); conn.close()
        except:
            pass

    # recompute cash_in_hand for affected date(s)
    try:
        if isinstance(bill_date, str):
            bd = datetime.strptime(bill_date, "%Y-%m-%d").date()
        else:
            bd = bill_date
        recompute_cash_in_hand_for_date(bd)
    except Exception:
        pass
    # also recompute today (deleting can affect today's cash)
    recompute_cash_in_hand_for_date(date.today())

    flash(f"Supplier bill #{bill_id} deleted and balances adjusted.", "success")
    return redirect(url_for("supplier_bill_search"))

@app.route("/bills/customer/delete/<int:bill_id>", methods=["POST"])
def customer_bill_delete(bill_id):
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    try:
        

        # fetch customer bill
        cursor.execute("SELECT * FROM customer_bills WHERE bill_id = %s", (bill_id,))
        cb = cursor.fetchone()
        if not cb:
            conn.rollback(); cursor.close(); conn.close()
            flash("Customer bill not found", "danger")
            return redirect(url_for("customer_bill_search"))

        cust_id     = cb["customer_id"]
        bill_date   = cb["bill_date"]
        amount      = to_decimal(cb["amount"] or 0)
        supplier_bill_id = cb.get("supplier_bill_id")

        # 1️⃣ Reverse customer balance (always)
        cursor.execute(
            "UPDATE customers SET balance = balance - %s WHERE customer_id = %s",
            (float(amount), cust_id)
        )

        # 2️⃣ If the bill came from a supplier bill → reverse supplier side
        if supplier_bill_id:

            # Fetch supplier bill
            cursor.execute("SELECT * FROM supplier_bills WHERE bill_id = %s", (supplier_bill_id,))
            sb = cursor.fetchone()

            old_total_amount = to_decimal(sb["total_amount"])
            old_balance_signed = to_decimal(sb["final_balance"])

            # New total amount after removing customer amount
            new_total_amount = old_total_amount - amount

            commission = to_decimal(sb["commission"])
            labour     = to_decimal(sb["labour"])
            transport  = to_decimal(sb["transport"])
            paid       = to_decimal(sb["paid"])

            # compute new bill_balance
            new_bill_balance = new_total_amount - commission - labour - transport - paid

            # supplier balance is negative (we owe), so reversing means:
            # supplier.balance += amount
            cursor.execute(
                "UPDATE suppliers SET balance = balance + %s WHERE supplier_id = %s",
                (float(amount), sb["supplier_id"])
            )

            # update supplier bill header
            cursor.execute("""
                UPDATE supplier_bills
                SET total_amount=%s,
                    balance=%s,
                    final_balance=%s
                WHERE bill_id=%s
            """, (
                float(new_total_amount),
                float(new_bill_balance),
                float(new_bill_balance) * -1,  # signed
                supplier_bill_id
            ))

            # delete supplier bill item(s) belonging to this customer bill
            cursor.execute("""
                DELETE FROM supplier_bill_items 
                WHERE bill_id=%s AND customer_id=%s
            """, (supplier_bill_id, cust_id))

        # 3️⃣ Finally delete the customer bill
        cursor.execute("DELETE FROM customer_bills WHERE bill_id = %s", (bill_id,))

        conn.commit()

    except Exception as e:
        conn.rollback()
        flash("Error deleting customer bill: " + str(e), "danger")
        return redirect(url_for("customer_bill_search"))

    finally:
        cursor.close()
        conn.close()

    # 4️⃣ Recompute cash in hand
    try:
        if isinstance(bill_date, str):
            bd = datetime.strptime(bill_date, "%Y-%m-%d").date()
        else:
            bd = bill_date
        recompute_cash_in_hand_for_date(bd)
    except:
        pass

    recompute_cash_in_hand_for_date(date.today())

    flash(f"Customer bill #{bill_id} deleted successfully.", "success")
    return redirect(url_for("customer_bill_search"))

@app.route("/accounts/adjust", methods=["GET", "POST"])
def adjust_account():
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    if request.method == "POST":
        entity_type = request.form.get("entity_type")
        entity_id = int(request.form.get("entity_id") or 0)

        try:
            amount = float(request.form.get("amount") or 0)
        except ValueError:
            amount = -1

        note = request.form.get("note") or ""
        if amount < 0:
            flash("Amount must be zero or positive.", "danger")
            cursor.close(); conn.close()
            return redirect(url_for("adjust_account"))

        if entity_type == "supplier":
            cursor.execute("SELECT supplier_id, name, balance FROM suppliers WHERE supplier_id = %s", (entity_id,))
            row = cursor.fetchone()
            if not row:
                flash("Supplier not found.", "danger")
                cursor.close(); conn.close()
                return redirect(url_for("adjust_account"))

            # ONLY update supplier balance
            cursor.execute(
                "UPDATE suppliers SET balance = balance + %s WHERE supplier_id = %s",
                (amount, entity_id)
            )

        elif entity_type == "customer":
            cursor.execute("SELECT customer_id, name, balance FROM customers WHERE customer_id = %s", (entity_id,))
            row = cursor.fetchone()
            if not row:
                flash("Customer not found.", "danger")
                cursor.close(); conn.close()
                return redirect(url_for("adjust_account"))

            # ONLY update customer balance
            cursor.execute(
                "UPDATE customers SET balance = balance + %s WHERE customer_id = %s",
                (amount, entity_id)
            )

        else:
            flash("Invalid entity type.", "danger")
            cursor.close(); conn.close()
            return redirect(url_for("adjust_account"))

        conn.commit()
        cursor.close(); conn.close()

        flash(f"Account updated for {entity_type} #{entity_id} (₹{amount:.2f}).", "success")
        return redirect(url_for("adjust_account"))

    # GET: build lists for dropdown
    cursor.execute("SELECT supplier_id AS id, name, balance FROM suppliers ORDER BY name ASC")
    suppliers = cursor.fetchall()

    cursor.execute("SELECT customer_id AS id, name, balance FROM customers ORDER BY name ASC")
    customers = cursor.fetchall()

    cursor.close(); conn.close()

    return render_template("adjust_account.html", suppliers=suppliers, customers=customers)



@app.route("/account-summary", methods=["GET"])
def account_summary():
    # get customers for dropdown
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT customer_id AS id, name FROM customers ORDER BY name ASC")
    customers = cursor.fetchall()

    raw_cid = (request.args.get("customer_id") or "").strip()
    from_str = request.args.get("from_date")
    to_str = request.args.get("to_date")

    today = date.today()
    try:
        from_date = datetime.strptime(from_str, "%Y-%m-%d").date() if from_str else today
    except Exception:
        from_date = today
    try:
        to_date = datetime.strptime(to_str, "%Y-%m-%d").date() if to_str else today
    except Exception:
        to_date = today

    if from_date > to_date:
        from_date, to_date = to_date, from_date

    purchases = []
    receipts = []
    sum_purchases = 0.0
    sum_receipts = 0.0
    current_balance = None

    customer_id = None
    customer_name = None

    if raw_cid:
        # 1) try direct integer
        try:
            customer_id = int(raw_cid)
        except (ValueError, TypeError):
            customer_id = None

        # 2) if not integer, try to extract first digits from the string (e.g. "govi (ID: 1)")
        if customer_id is None:
            m = re.search(r'\b(\d+)\b', raw_cid)
            if m:
                try:
                    customer_id = int(m.group(1))
                except:
                    customer_id = None

        # 3) if still not found, treat raw_cid as a name and search the customers table (first match)
        if customer_id is None:
            # safer: search by name part only if it's not obviously numeric garbage
            name_search = raw_cid
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT customer_id, name, balance FROM customers WHERE name LIKE %s ORDER BY name LIMIT 1", (f"%{name_search}%",))
            cr = cur.fetchone()
            cur.close()
            if cr:
                customer_id = cr["customer_id"]
                customer_name = cr["name"]

        # 4) if we have an id, fetch the canonical name & balance (if not already from step 3)
        if customer_id and not customer_name:
            cur = conn.cursor(dictionary=True)
            cur.execute("SELECT customer_id, name, balance FROM customers WHERE customer_id = %s", (customer_id,))
            cr = cur.fetchone()
            cur.close()
            if cr:
                customer_name = cr["name"]
            else:
                # invalid id supplied -> clear to show helpful message
                customer_id = None
                customer_name = None
                flash("Customer not found. Please select a valid customer.", "warning")

    # If we resolved a customer_id, fetch purchases & receipts
    if customer_id:
        cursor.execute("""
            SELECT bill_id, bill_date, quantity, rate, amount, supplier_bill_id
            FROM customer_bills
            WHERE customer_id = %s
              AND bill_date BETWEEN %s AND %s
            ORDER BY bill_date ASC
        """, (customer_id, from_date, to_date))
        rows = cursor.fetchall()
        for r in rows:
            amt = float(r.get("amount") or 0.0)
            purchases.append({
                "bill_id": r.get("bill_id"),
                "date": r.get("bill_date"),
                "quantity": r.get("quantity"),
                "rate": r.get("rate"),
                "amount": amt,
                "supplier_bill_id": r.get("supplier_bill_id")
            })
            sum_purchases += amt

        # receipts (transactions)
        cursor.execute("""
            SELECT tx_id, tx_date, amount, note, tx_type
            FROM transactions
            WHERE entity_type = 'customer'
              AND entity_id = %s
              AND tx_type = 'receipt'
              AND DATE(tx_date) BETWEEN %s AND %s
            ORDER BY tx_date ASC
        """, (customer_id, from_date, to_date))
        rows = cursor.fetchall()
        for r in rows:
            amt = float(r.get("amount") or 0.0)
            receipts.append({
                "tx_id": r.get("tx_id"),
                "date": r.get("tx_date"),
                "amount": amt,
                "note": r.get("note")
            })
            sum_receipts += amt

        # current stored balance
        cursor.execute("SELECT balance FROM customers WHERE customer_id = %s", (customer_id,))
        crow = cursor.fetchone()
        current_balance = float(crow.get("balance")) if crow and crow.get("balance") is not None else 0.0

    cursor.close()
    conn.close()

    net = sum_receipts - sum_purchases

    # For the template, pass a helpful selected_customer:
    # prefer numeric id (for comparing with option values), but also pass a displayed name if available.
    selected_customer = customer_id if customer_id is not None else raw_cid

    return render_template("account_summary.html",
                           customers=customers,
                           selected_customer=selected_customer,
                           from_date=from_date,
                           to_date=to_date,
                           purchases=purchases,
                           receipts=receipts,
                           sum_purchases=sum_purchases,
                           sum_receipts=sum_receipts,
                           net=net,
                           current_balance=current_balance)
# ---------------- Supplier: Print single bill ----------------

@app.route("/bills/supplier/print/<int:bill_id>")
def supplier_bill_print(bill_id):
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    # 1) Fetch full bill + supplier name
    cursor.execute("""
        SELECT b.*,
               s.name AS supplier_name
        FROM supplier_bills b
        JOIN suppliers s ON b.supplier_id = s.supplier_id
        WHERE b.bill_id = %s
    """, (bill_id,))
    bill = cursor.fetchone()

    if not bill:
        cursor.close(); conn.close()
        flash("Supplier bill not found", "danger")
        return redirect(url_for("supplier_bill_search"))

    # 2) Normalize numeric fields
    numeric_fields = [
        "total_amount", "commission", "transport",
        "labour", "paid", "balance",
        "old_balance", "final_balance"
    ]
    for fld in numeric_fields:
        try:
            bill[fld] = float(bill.get(fld) or 0.0)
        except:
            bill[fld] = 0.0

    # 3) Fetch items (with customer name)
    cursor.execute("""
        SELECT it.*, c.name AS customer_name
        FROM supplier_bill_items it
        LEFT JOIN customers c ON it.customer_id = c.customer_id
        WHERE it.bill_id = %s
        ORDER BY COALESCE(c.name, CAST(it.customer_id AS VARCHAR))
    """, (bill_id,))
    items = cursor.fetchall() or []

    # Normalize item numeric fields
    for it in items:
        for f in ("quantity", "rate", "amount"):
            try:
                it[f] = float(it.get(f) or 0.0)
            except:
                it[f] = 0.0

    # 4) Fetch current supplier balance
    cursor.execute("SELECT balance FROM suppliers WHERE supplier_id = %s",
                   (bill["supplier_id"],))
    sb = cursor.fetchone()
    current_balance = float(sb["balance"]) if sb and sb["balance"] is not None else 0.0

    cursor.close()
    conn.close()

    return render_template("supplier_bill_print.html",
                           bill=bill,
                           items=items,
                           current_balance=current_balance)
# ---------------- Supplier: Print search results (multiple) ----------------

@app.route("/bills/supplier/print_search", methods=["POST"])
def supplier_bill_print_search():
    bill_no = request.form.get("bill_no")
    name = request.form.get("name")
    bill_date = request.form.get("bill_date")

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    # 1) Fetch matching bills
    results = []

    if bill_no:
        cursor.execute("""
            SELECT b.*, s.name AS supplier_name
            FROM supplier_bills b
            JOIN suppliers s ON b.supplier_id = s.supplier_id
            WHERE b.bill_id = %s
        """, (int(bill_no),))
        row = cursor.fetchone()
        if row:
            results = [row]
    else:
        sql = """
            SELECT b.*, s.name AS supplier_name
            FROM supplier_bills b
            JOIN suppliers s ON b.supplier_id = s.supplier_id
            WHERE 1=1
        """
        params = []
        if name:
            sql += " AND s.name LIKE %s"
            params.append(f"%{name}%")
        if bill_date:
            sql += " AND b.bill_date = %s"
            params.append(bill_date)

        cursor.execute(sql, params)
        results = cursor.fetchall()

    printable = []

    # 2) For each bill → fetch items + customer name + current balance
    for r in results:

        # Items WITH customer name
        cursor.execute("""
            SELECT it.*, c.name AS customer_name
            FROM supplier_bill_items it
            LEFT JOIN customers c ON it.customer_id = c.customer_id
            WHERE it.bill_id = %s
            ORDER BY COALESCE(c.name, CAST(it.customer_id AS VARCHAR))
        """, (r["bill_id"],))
        items = cursor.fetchall() or []

        # Normalize numeric values
        for it in items:
            for f in ("quantity", "rate", "amount"):
                try:
                    it[f] = float(it.get(f) or 0.0)
                except:
                    it[f] = 0.0

        cursor.execute("SELECT balance FROM suppliers WHERE supplier_id = %s",
                       (r["supplier_id"],))
        sb = cursor.fetchone()
        current_balance = float(sb["balance"]) if sb and sb["balance"] is not None else 0.0

        # Add to printable list
        printable.append({
            "bill": r,
            "items": items,
            "current_balance": current_balance
        })

    cursor.close()
    conn.close()

    return render_template("supplier_bill_print_many.html",
                           printable=printable)

# ---------------- Customer: Print single bill ----------------
'''@app.route("/bills/customer/print/<int:bill_id>")
def customer_bill_print(bill_id):
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("""SELECT cb.*, c.name AS customer_name FROM customer_bills cb
                      JOIN customers c ON cb.customer_id = c.customer_id
                      WHERE cb.bill_id = %s""", (bill_id,))
    bill = cursor.fetchone()
    if not bill:
        cursor.close(); conn.close()
        flash("Customer bill not found", "danger")
        return redirect(url_for("customer_bill_search"))

    cursor.close()
    conn.close()

    # get current customer balance
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT balance FROM customers WHERE customer_id = %s", (bill["customer_id"],))
    cb = cursor.fetchone()
    current_balance = float(cb["balance"]) if cb and cb.get("balance") is not None else 0.0
    cursor.close(); conn.close()

    return render_template("customer_bill_print.html", bill=bill, current_balance=current_balance)


# ---------------- Customer: Print search results (multiple) ----------------



@app.route("/bills/customer/print/<int:bill_id>")
def customer_bill_print(bill_id):
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("""SELECT cb.*, c.name AS customer_name FROM customer_bills cb
                      JOIN customers c ON cb.customer_id = c.customer_id
                      WHERE cb.bill_id = %s""", (bill_id,))
    bill = cursor.fetchone()
    if not bill:
        cursor.close(); conn.close()
        flash("Customer bill not found", "danger")
        return redirect(url_for("customer_bill_search"))

    cursor.close()
    conn.close()

    # get current customer balance
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT balance FROM customers WHERE customer_id = %s", (bill["customer_id"],))
    cb = cursor.fetchone()
    current_balance = float(cb["balance"]) if cb and cb.get("balance") is not None else 0.0
    cursor.close(); conn.close()

    return render_template("customer_bill_print.html", bill=bill, current_balance=current_balance)


# ---------------- Customer: Print search results (multiple) ----------------



@app.route("/bills/customer/print_search", methods=["POST"])
def customer_bill_print_search():
    bill_no = request.form.get("bill_no")
    name = request.form.get("name")
    bill_date = request.form.get("bill_date")  # expecting YYYY-MM-DD

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    # 1) If bill_no provided -> reuse single-bill printer (optional)
    if bill_no:
        try:
            bid = int(bill_no)
            cursor.execute("""
                SELECT cb.*, c.name AS customer_name
                FROM customer_bills cb
                JOIN customers c ON cb.customer_id = c.customer_id
                WHERE cb.bill_id = %s
            """, (bid,))
            single = cursor.fetchone()
            if single:
                cursor.execute("SELECT balance FROM customers WHERE customer_id = %s", (single["customer_id"],))
                row = cursor.fetchone()
                current_balance = float(row["balance"]) if row and row.get("balance") is not None else 0.0
                cursor.close(); conn.close()
                return render_template("customer_bill_print.html", bill=single, current_balance=current_balance)
        except ValueError:
            pass  # if bill_no not integer, continue to search by name/date

    # 2) Resolve customer_id from name (or numeric id)
    customer_id = None
    customer_name = None
    current_balance = 0.0

    if name:
        # if user typed a numeric id, prefer exact id match
        try:
            maybe_id = int(name)
            cursor.execute("SELECT customer_id, name, balance FROM customers WHERE customer_id = %s", (maybe_id,))
            r = cursor.fetchone()
            if r:
                customer_id = r["customer_id"]
                customer_name = r["name"]
                current_balance = float(r["balance"] or 0.0)
        except ValueError:
            # search by name (first match)
            cursor.execute("SELECT customer_id, name, balance FROM customers WHERE name LIKE %s ORDER BY name LIMIT 1", (f"%{name}%",))
            r = cursor.fetchone()
            if r:
                customer_id = r["customer_id"]
                customer_name = r["name"]
                current_balance = float(r["balance"] or 0.0)

    if not customer_id:
        cursor.close(); conn.close()
        flash("Customer not found for printing", "danger")
        return redirect(url_for("customer_bill_search"))

    # 3) Determine date range (for now we treat bill_date as single day)
    if bill_date:
        try:
            period_from = datetime.strptime(bill_date, "%Y-%m-%d").date()
        except Exception:
            period_from = date.today()
        period_to = period_from
    else:
        period_from = date.today()
        period_to = date.today()

    # 4) Fetch customer bills for that customer & period, ordered ascending
    cursor.execute("""
        SELECT bill_id, customer_id, bill_date, quantity, rate, amount, supplier_bill_id, old_balance, final_balance
        FROM customer_bills
        WHERE customer_id = %s
          AND bill_date BETWEEN %s AND %s
        ORDER BY bill_date ASC, bill_id ASC
    """, (customer_id, period_from, period_to))
    purchases = cursor.fetchall() or []

    # 5) Compute opening from earliest bill's old_balance (if any)
    if purchases:
        earliest = purchases[0]
        # Use the stored old_balance column (should be snapshot before first bill)
        opening = float(earliest.get("old_balance") or 0.0)
    else:
        # No purchases in period -> opening is the current balance (or 0)
        # You could alternatively calculate opening as current - (purchases - receipts) but with no purchases this equals current.
        opening = current_balance

    # 6) Compute totals (net purchases)
    net = sum(float(p.get("amount") or 0.0) for p in purchases)

    # 7) Compute receipts (transactions of type 'receipt') within same period for this customer
    cursor.execute("""
        SELECT COALESCE(SUM(amount),0) AS total_receipts
        FROM transactions
        WHERE entity_type='customer' AND entity_id = %s
          AND tx_type = 'receipt'
          AND DATE(tx_date) BETWEEN %s AND %s
    """, (customer_id, period_from, period_to))
    tr = cursor.fetchone()
    sum_receipts = float(tr["total_receipts"]) if tr and tr.get("total_receipts") is not None else 0.0

    # 8) Ensure current_balance is fresh (if not loaded earlier)
    if current_balance == 0.0:
        cursor.execute("SELECT balance FROM customers WHERE customer_id = %s", (customer_id,))
        crow = cursor.fetchone()
        current_balance = float(crow["balance"]) if crow and crow.get("balance") is not None else 0.0

    cursor.close(); conn.close()

    # 9) Render a consolidated customer statement (single page)
    return render_template(
        "customer_bill_consolidated_print.html",
        customer_id=customer_id,
        customer_name=customer_name,
        from_date=period_from,
        to_date=period_to,
        purchases=purchases,
        opening=opening,
        net=net,
        current_balance=current_balance,
        sum_receipts=sum_receipts
    )'''

from datetime import datetime, date
from flask import request, flash, redirect, url_for, render_template

@app.route("/transactions/edit", methods=["GET"])
def transactions_edit_page():
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    # fetch customers & suppliers for the datalist/dropdown
    cursor.execute("SELECT customer_id AS id, name FROM customers ORDER BY name")
    customers = cursor.fetchall()
    cursor.execute("SELECT supplier_id AS id, name FROM suppliers ORDER BY name")
    suppliers = cursor.fetchall()

    cursor.close(); conn.close()
    return render_template("edit_transaction.html", customers=customers, suppliers=suppliers)


from datetime import datetime, date, time, timezone
import traceback

# zoneinfo fallback: go safe if zoneinfo is unavailable
try:
    from zoneinfo import ZoneInfo
    def get_zone(tzname):
        return ZoneInfo(tzname)
except Exception:
    # If zoneinfo is missing (old Python or no tzdb), fall back to fixed offset for IST
    class FixedOffset:
        def __init__(self, offset_minutes):
            self._offset = timezone(timedelta(minutes=offset_minutes))
        def __repr__(self): return "FixedOffset(+05:30)"
    from datetime import timedelta
    def get_zone(tzname):
        if tzname == "Asia/Kolkata":
            return timezone(timedelta(minutes=330))
        raise RuntimeError("zoneinfo not available and tzname != Asia/Kolkata")


@app.route("/transactions/list", methods=["GET"])
def transactions_list():
    entity_type = request.args.get("entity_type")
    entity_id = request.args.get("entity_id")
    tx_type = request.args.get("tx_type")

    if not entity_type or not entity_id or not tx_type:
        return jsonify({"error": "missing_parameters"}), 400

    try:
        entity_id = int(entity_id)
    except ValueError:
        return jsonify({"error": "invalid_entity_id"}), 400

    try:
        # compute IST window for "today in IST"
        ist = get_zone("Asia/Kolkata")
        today_ist = datetime.now(ist).date()
        start_ist = datetime.combine(today_ist, time.min).replace(tzinfo=ist)
        end_ist   = datetime.combine(today_ist, time.max).replace(tzinfo=ist)

        # convert to UTC
        start_utc = start_ist.astimezone(timezone.utc)
        end_utc   = end_ist.astimezone(timezone.utc)

        # Convert to naive UTC datetimes (no tzinfo) formatted for MySQL
        # Many MySQL connectors accept strings like 'YYYY-MM-DD HH:MM:SS'
        start_sql = start_utc.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        end_sql   = end_utc.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")

        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        # Use BETWEEN with formatted strings to avoid tz-aware datetime issues
        cursor.execute("""
            SELECT tx_id, tx_type, entity_type, entity_id, amount, note, tx_date
            FROM transactions
            WHERE entity_type = %s
              AND entity_id = %s
              AND tx_type = %s
              AND tx_date BETWEEN %s AND %s
            ORDER BY tx_date DESC, tx_id DESC
            LIMIT 500
        """, (entity_type, entity_id, tx_type, start_sql, end_sql))
        rows = cursor.fetchall()
        cursor.close(); conn.close()

        # Normalize output
        for r in rows:
            dt = r.get("tx_date")
            if isinstance(dt, datetime):
                # If connector returned naive, assume UTC; attach 'Z' to mark UTC
                if dt.tzinfo is None:
                    r["tx_date"] = dt.replace(tzinfo=timezone.utc).isoformat()
                else:
                    r["tx_date"] = dt.astimezone(timezone.utc).isoformat()
            elif isinstance(dt, date):
                r["tx_date"] = datetime.combine(dt, time.min).isoformat()
            else:
                r["tx_date"] = None
            r["amount"] = float(r.get("amount") or 0.0)

        return jsonify({"transactions": rows})

    except Exception as exc:
        # log full traceback to stderr so your server logs capture it
        tb = traceback.format_exc()
        print("ERROR in /transactions/list:", tb, flush=True)
        # return a helpful message to the client (avoid leaking sensitive internals)
        return jsonify({"error": "internal_server_error", "message": str(exc)}), 500


@app.route("/transactions/edit/<int:tx_id>", methods=["GET","POST"])
def transactions_edit_one(tx_id):
    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("SELECT * FROM transactions WHERE tx_id = %s", (tx_id,))
    tx = cursor.fetchone()
    if not tx:
        cursor.close(); conn.close()
        return jsonify({"error": "tx_not_found"}), 404

    # GET: return tx as before
    if request.method == "GET":
        out = dict(tx)
        try:
            if isinstance(out.get("tx_date"), (datetime,)):
                out["tx_date"] = out["tx_date"].isoformat()
        except Exception:
            pass
        out["amount"] = float(out.get("amount") or 0.0)
        cursor.close(); conn.close()
        return jsonify({"tx": out})

    # POST -> update
    try:
        data = request.get_json() or {}
        new_amount = float(data.get("amount") or 0.0)
        new_date = data.get("tx_date")  # may be None, date-only "YYYY-MM-DD", or ISO datetime
        new_note = data.get("note") or None

        old_amount = float(tx.get("amount") or 0.0)
        entity_type = tx.get("entity_type")
        entity_id = int(tx.get("entity_id"))
        tx_type = tx.get("tx_type")  # 'receipt' or 'payment'

        delta_amount = new_amount - old_amount

        # update balances (your existing logic)
        if entity_type == "customer":
            if tx_type == "receipt":
                balance_delta = -delta_amount
            else:  # payment to customer
                balance_delta = delta_amount
            cursor.execute("UPDATE customers SET balance = balance + %s WHERE customer_id = %s", (balance_delta, entity_id))

        elif entity_type == "supplier":
            if tx_type == "receipt":
                balance_delta = -delta_amount
            else:
                balance_delta = delta_amount
            cursor.execute("UPDATE suppliers SET balance = balance + %s WHERE supplier_id = %s", (balance_delta, entity_id))
        else:
            cursor.close(); conn.close()
            return jsonify({"error":"invalid_entity_type"}), 400

        # -------- Date handling: interpret incoming date/time as IST and store naive IST DATETIME --------
        parsed_for_db = None  # will become string 'YYYY-MM-DD HH:MM:SS' or None if not changing
        if new_date:
            try:
                ist = ZoneInfo("Asia/Kolkata")

                # Case 1: client sent only date e.g. "2025-12-11"
                if isinstance(new_date, str) and len(new_date) == 10 and new_date.count('-') == 2:
                    # Try to preserve existing time-of-day from the current tx if available
                    orig_dt = tx.get("tx_date")
                    if isinstance(orig_dt, datetime):
                        tpart = orig_dt.time()
                    else:
                        # fallback to midday to avoid boundary oddities
                        tpart = time(hour=12, minute=0, second=0)

                    # build IST-aware datetime with same time-of-day
                    dt_ist = datetime.strptime(new_date, "%Y-%m-%d").replace(
                        hour=tpart.hour, minute=tpart.minute, second=tpart.second, tzinfo=ist
                    )

                else:
                    # Client sent a full ISO datetime string (maybe with or without tz)
                    dt = datetime.fromisoformat(new_date)
                    if dt.tzinfo is None:
                        # interpret as IST (client provided local time without tz)
                        dt_ist = dt.replace(tzinfo=ist)
                    else:
                        # convert any timezone to IST
                        dt_ist = dt.astimezone(ist)

                # We want to store the IST wall-time exactly in MySQL DATETIME (which stores no tz);
                # therefore remove tzinfo and format as SQL datetime string.
                dt_naive_ist = dt_ist.replace(tzinfo=None)
                parsed_for_db = dt_naive_ist.strftime("%Y-%m-%d %H:%M:%S")

            except Exception as e:
                # If parsing fails, don't modify the date; continue (or report error)
                parsed_for_db = None

        # Perform UPDATE: include tx_date only if parsed_for_db is set
        if parsed_for_db:
            cursor.execute("""
                UPDATE transactions
                SET amount = %s, tx_date = %s, note = %s
                WHERE tx_id = %s
            """, (new_amount, parsed_for_db, new_note, tx_id))
        else:
            cursor.execute("""
                UPDATE transactions
                SET amount = %s, note = %s
                WHERE tx_id = %s
            """, (new_amount, new_note, tx_id))

        conn.commit()
        cursor.close(); conn.close()
        return jsonify({"status":"ok", "balance_delta": balance_delta})

    except Exception as e:
        try: conn.rollback()
        except: pass
        tb = traceback.format_exc()
        print("ERROR in transactions_edit_one:", tb, flush=True)
        cursor.close(); conn.close()
        return jsonify({"error":"exception","message": str(e)}), 500
if __name__ == "__main__":
    app.run(debug=True, use_reloader=False)
