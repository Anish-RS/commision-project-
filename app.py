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
        # Changed CAST(... AS CHAR) to Postgres-supported CAST(... AS VARCHAR)
        cursor.execute("""
            SELECT it.*, c.name AS customer_name FROM supplier_bill_items it
            LEFT JOIN customers c ON it.customer_id = c.customer_id
            WHERE it.bill_id = %s ORDER BY COALESCE(c.name, CAST(it.customer_id AS VARCHAR))
        """, (r["bill_id"],))
        r["items"] = cursor.fetchall()

    cursor.close(); conn.close()
    return render_template("supplier_bill_search.html", results=results)
    
@app.route("/bills/customer/search")
def customer_bill_search():
    return render_template("customer_bill_search.html")

if __name__ == "__main__":
    app.run(debug=True)
