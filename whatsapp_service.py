import os
import uuid
import hashlib
import requests
from datetime import datetime, timedelta

from psycopg2.extras import RealDictCursor

from db import get_connection


ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
APP_BASE_URL = os.getenv("APP_BASE_URL")


def generate_token():
    return str(uuid.uuid4())


def _statement_lock_key(customer_id, statement_date):
    """
    Deterministic 64-bit signed key for a Postgres advisory lock, scoped
    to one customer + one statement date. Two processes/requests trying
    to send a statement to the same customer for the same date will
    contend for the same key.
    """

    raw = f"whatsapp_statement:{customer_id}:{statement_date}"
    digest = hashlib.sha256(raw.encode()).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


# -------------------------------
# Statement Link Generator
# -------------------------------
def get_statement_link(customer_id, statement_date):

    conn = get_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("""
        SELECT token
        FROM statement_tokens
        WHERE customer_id=%s
        AND statement_date=%s
        AND expires_at > NOW()
    """, (customer_id, statement_date))

    row = cursor.fetchone()

    if row:
        token = row["token"]

    else:

        token = generate_token()

        cursor.execute("""
            INSERT INTO statement_tokens
            (
                token,
                customer_id,
                statement_date,
                expires_at
            )
            VALUES
            (
                %s,
                %s,
                %s,
                %s
            )
        """,
        (
            token,
            customer_id,
            statement_date,
            datetime.now() + timedelta(days=15)
        ))

        conn.commit()

    cursor.close()
    conn.close()

    return f"{APP_BASE_URL}/s/{token}"

def build_whatsapp_message(
    customer_name,
    statement_date,
    purchase_total,
    purchase_entries,
    statement_link
):
    """
    Build the WhatsApp message body.
    """

    return f"""🍌 S. Govindhan Banana Commission Agent

Vanakkam {customer_name} 🙏

Thank you for your business with us.

━━━━━━━━━━━━━━━━━━━━

📅 Statement Date
{statement_date}

🧺 Purchase Summary
₹{purchase_total:,.2f}

📦 Purchase Entries
{purchase_entries}

━━━━━━━━━━━━━━━━━━━━

📄 Tap below to view your complete purchase statement.

{statement_link}

Thank you for being a valued customer.
Wishing you a successful day ahead. 🌿
"""


# ============================================================
# Database Helper Functions
# ============================================================

def get_customer_details(customer_id):
    """
    Returns customer details required for WhatsApp.
    """

    conn = get_connection()

    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        cursor.execute("""
            SELECT
                customer_id,
                name,
                phone,
                balance,
                opening_balance
            FROM customers
            WHERE customer_id = %s
        """, (customer_id,))

        customer = cursor.fetchone()

        return customer

    finally:
        cursor.close()
        conn.close()


def get_purchase_summary(customer_id, statement_date):
    """
    Returns purchase summary for one customer on one day.
    """

    conn = get_connection()

    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        cursor.execute("""
            SELECT
                COUNT(*) AS purchase_entries,
                COALESCE(SUM(amount),0) AS purchase_total
            FROM customer_bills
            WHERE customer_id = %s
              AND bill_date = %s
        """, (customer_id, statement_date))

        row = cursor.fetchone()

        return {
            "purchase_entries": int(row["purchase_entries"]),
            "purchase_total": float(row["purchase_total"])
        }

    finally:
        cursor.close()
        conn.close()


def get_customers_for_date(statement_date):
    """
    Returns all customers having purchases on a date.
    """

    conn = get_connection()

    try:

        cursor = conn.cursor(cursor_factory=RealDictCursor)

        cursor.execute("""
            SELECT DISTINCT
                c.customer_id,
                c.name,
                c.phone
            FROM customers c
            INNER JOIN customer_bills cb
                    ON cb.customer_id = c.customer_id
            WHERE cb.bill_date = %s
            ORDER BY c.name
        """, (statement_date,))

        return cursor.fetchall()

    finally:
        cursor.close()
        conn.close()

# ============================================================
# WhatsApp Cloud API
# ============================================================

GRAPH_API_VERSION = "v23.0"

GRAPH_API_URL = (
    f"https://graph.facebook.com/{GRAPH_API_VERSION}"
)


def build_template_payload(
    customer_name,
    statement_date,
    purchase_total,
    purchase_entries,
    token,
    recipient_number
):

    return {

        "messaging_product": "whatsapp",

        "to": recipient_number,

        "type": "template",

        "template": {

            "name": os.getenv("WHATSAPP_TEMPLATE_NAME"),

            "language": {

                "code": "en"

            },

            "components": [

                {

                    "type": "body",

                    "parameters": [

                        {
                            "type": "text",
                            "text": customer_name
                        },

                        {
                            "type": "text",
                            "text": statement_date
                        },

                        {
                            "type": "text",
                            "text": f"{purchase_total:.2f}"
                        },

                        {
                            "type": "text",
                            "text": str(purchase_entries)
                        }

                    ]

                },

                {

                    "type": "button",

                    "sub_type": "url",

                    "index": "0",

                    "parameters": [

                        {

                            "type": "text",

                            "text": token

                        }

                    ]

                }

            ]

        }

    }



def send_template_message(payload):
    """
    Sends template message to WhatsApp Cloud API.
    """

    url = (
        f"{GRAPH_API_URL}/"
        f"{PHONE_NUMBER_ID}"
        "/messages"
    )

    headers = {

        "Authorization":
            f"Bearer {ACCESS_TOKEN}",

        "Content-Type":
            "application/json"

    }

    try:

        response = requests.post(

            url,

            headers=headers,

            json=payload,

            timeout=30

        )

        response.raise_for_status()

        data = response.json()

        message_id = None

        if (
            "messages" in data and
            len(data["messages"]) > 0
        ):

            message_id = data["messages"][0]["id"]

        return {

            "success": True,

            "message_id": message_id,

            "response": data

        }

    except requests.exceptions.HTTPError:

        try:

            error = response.json()

        except Exception:

            error = response.text

        return {

            "success": False,

            "reason": error

        }

    except requests.exceptions.Timeout:

        return {

            "success": False,

            "reason": "Connection Timeout"

        }

    except Exception as ex:

        return {

            "success": False,

            "reason": str(ex)

        }


def verify_whatsapp_configuration():
    """
    Checks if the WhatsApp configuration is complete.
    """

    missing = []

    if not ACCESS_TOKEN:
        missing.append(
            "WHATSAPP_ACCESS_TOKEN"
        )

    if not PHONE_NUMBER_ID:
        missing.append(
            "WHATSAPP_PHONE_NUMBER_ID"
        )

    if not APP_BASE_URL:
        missing.append(
            "APP_BASE_URL"
        )

    if not os.getenv("WHATSAPP_TEMPLATE_NAME"):
        missing.append(
            "WHATSAPP_TEMPLATE_NAME"
        )

    if len(missing):

        return {

            "success": False,

            "missing": missing

        }

    return {

        "success": True

    }


# ============================================================
# WhatsApp Logging
# ============================================================

def log_whatsapp_message(
    customer_id,
    statement_date,
    purchase_total,
    purchase_entries,
    status,
    message_type="PURCHASE_STATEMENT",
    whatsapp_message_id=None,
    error_message=None
):
    """
    Save every WhatsApp send attempt.
    """

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO whatsapp_logs
            (
                customer_id,
                statement_date,
                purchase_total,
                purchase_entries,
                message_type,
                whatsapp_message_id,
                status,
                error_message
            )
            VALUES
            (
                %s,%s,%s,%s,%s,%s,%s,%s
            )
        """, (

            customer_id,
            statement_date,
            purchase_total,
            purchase_entries,
            message_type,
            whatsapp_message_id,
            status,
            error_message

        ))

        conn.commit()

    finally:

        cursor.close()
        conn.close()

SENDING_STALE_MINUTES = 3
"""
How long a 'SENDING' row is trusted before we treat it as abandoned
(e.g. the web worker crashed/restarted mid-send). Kept short since a
real WhatsApp API call normally resolves in a couple of seconds.
"""


def create_sending_log(
    customer_id,
    statement_date,
    purchase_total,
    purchase_entries,
    message_type="PURCHASE_STATEMENT"
):
    """
    Writes a 'SENDING' row BEFORE the WhatsApp API is called, so the
    customer shows as in-progress to every other request/user the
    moment a send starts - not only after it succeeds or fails.
    Returns the new row's id so the same row can be finalized in
    place afterwards (no second row is ever inserted for this attempt).
    """

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO whatsapp_logs
            (
                customer_id,
                statement_date,
                purchase_total,
                purchase_entries,
                message_type,
                status
            )
            VALUES
            (
                %s,%s,%s,%s,%s,'SENDING'
            )
            RETURNING id
        """, (
            customer_id,
            statement_date,
            purchase_total,
            purchase_entries,
            message_type
        ))

        log_id = cursor.fetchone()[0]

        conn.commit()

        return log_id

    finally:

        cursor.close()
        conn.close()


def finalize_whatsapp_log(
    log_id,
    status,
    whatsapp_message_id=None,
    error_message=None
):
    """
    Updates the row created by create_sending_log() in place with the
    final outcome (SUCCESS/FAILED). Keeping it a single row (insert
    once, update once) means a customer never shows two log entries
    for one send attempt.
    """

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            UPDATE whatsapp_logs
            SET status = %s,
                whatsapp_message_id = %s,
                error_message = %s
            WHERE id = %s
        """, (
            status,
            whatsapp_message_id,
            error_message,
            log_id
        ))

        conn.commit()

    finally:

        cursor.close()
        conn.close()


def reap_stale_sending_row(customer_id, statement_date, stale_minutes=SENDING_STALE_MINUTES):
    """
    Flips any abandoned 'SENDING' row for this customer/date (e.g. the
    process died mid-send before it could finalize) over to FAILED, so
    it stops blocking retries and stops showing as in-progress forever.
    Safe to call unconditionally: only rows older than stale_minutes
    are touched, and this only ever runs while the caller holds the
    advisory lock for this customer/date, so there's no race with a
    genuinely in-flight send.
    """

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            UPDATE whatsapp_logs
            SET status = 'FAILED',
                error_message = 'Send timed out (worker likely restarted mid-send)'
            WHERE customer_id = %s
              AND statement_date = %s
              AND status = 'SENDING'
              AND created_at <= NOW() - (%s * INTERVAL '1 minute')
        """, (
            customer_id,
            statement_date,
            stale_minutes
        ))

        conn.commit()

    finally:

        cursor.close()
        conn.close()


def get_in_progress_customer_ids(customer_ids, statement_date, stale_minutes=SENDING_STALE_MINUTES):
    """
    Returns the subset of customer_ids that currently have a *live*
    (non-stale) SENDING row for this statement_date - i.e. a send is
    actively in flight for them right now, in this or another request.
    Used by the preview endpoint so a second user sees "Sending..."
    immediately instead of a plain, selectable checkbox.
    """

    if not customer_ids:
        return set()

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""
            SELECT DISTINCT customer_id
            FROM whatsapp_logs
            WHERE statement_date = %s
              AND customer_id = ANY(%s)
              AND status = 'SENDING'
              AND created_at > NOW() - (%s * INTERVAL '1 minute')
        """, (
            statement_date,
            list(customer_ids),
            stale_minutes
        ))

        return {row[0] for row in cursor.fetchall()}

    finally:

        cursor.close()
        conn.close()


def update_delivery_status(whatsapp_message_id, new_status, error_message=None):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE whatsapp_logs
            SET status = %s,
                error_message = COALESCE(%s, error_message)
            WHERE whatsapp_message_id = %s
        """, (new_status, error_message, whatsapp_message_id))
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def has_statement_been_sent(
    customer_id,
    statement_date
):
    """
    Prevent duplicate statement sending.
    """

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""

            SELECT COUNT(*)

            FROM whatsapp_logs

            WHERE customer_id=%s

            AND statement_date=%s

            AND status IN ('SUCCESS', 'SENT', 'DELIVERED', 'READ')

        """, (

            customer_id,
            statement_date

        ))

        count = cursor.fetchone()[0]

        return count > 0

    finally:

        cursor.close()
        conn.close()

def has_recent_message_to_customer(customer_id, hours=24):
    """
    Returns True if a message was successfully sent to this
    customer within the cooldown window, regardless of which
    statement_date it was for. Prevents accidental repeat sends
    from double-clicks etc.

    NOTE: status is checked against SUCCESS / SENT / DELIVERED / READ,
    not just SUCCESS. update_delivery_status() overwrites the
    status column as WhatsApp's delivery webhooks come in (sent ->
    delivered -> read), so a row logged as SUCCESS at send-time can
    flip to SENT within seconds, then DELIVERED/READ later. SENT
    must be included here or the cooldown silently stops applying
    the moment the "sent" webhook lands. created_at (the original
    send time) is what the cooldown window is measured against;
    only rows that never actually went out (FAILED) are excluded.
    """

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""

            SELECT COUNT(*)

            FROM whatsapp_logs

            WHERE customer_id=%s

            AND status IN ('SUCCESS', 'SENT', 'DELIVERED', 'READ')

            AND created_at > NOW() - (%s * INTERVAL '1 hour')

        """, (

            customer_id,
            hours

        ))

        count = cursor.fetchone()[0]

        return count > 0

    finally:

        cursor.close()
        conn.close()

def mark_retry(
    customer_id,
    statement_date
):
    """
    Removes previous failed attempts.
    """

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute("""

            DELETE

            FROM whatsapp_logs

            WHERE customer_id=%s

            AND statement_date=%s

            AND status='FAILED'

        """, (

            customer_id,
            statement_date

        ))

        conn.commit()

    finally:

        cursor.close()
        conn.close()

def send_purchase_statement(customer_id, statement_date):

    config = verify_whatsapp_configuration()

    if not config["success"]:

        return config

    # ------------------------------------------------------------
    # Acquire a Postgres advisory lock for this (customer, date) pair
    # before doing anything else. This makes the "already sent?" check
    # further down atomic with the eventual send + log, even when two
    # requests (double-click, two tabs, an overlapping bulk-send retry,
    # etc.) hit this function for the same customer/date at the same
    # time. Without this, both requests can pass the "not sent yet"
    # check before either one has written its log row, and the
    # customer ends up getting the WhatsApp message twice.
    # ------------------------------------------------------------

    lock_conn = get_connection()
    lock_key = _statement_lock_key(customer_id, statement_date)

    try:
        lock_cursor = lock_conn.cursor()
        lock_cursor.execute("SELECT pg_advisory_lock(%s)", (lock_key,))

        return _send_purchase_statement_locked(customer_id, statement_date)

    finally:
        try:
            lock_cursor.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
        finally:
            lock_cursor.close()
            lock_conn.close()


def _send_purchase_statement_locked(customer_id, statement_date):
    """
    Original send_purchase_statement body, now only ever run while
    holding the advisory lock for this customer/date.
    """

    # Clear out any abandoned SENDING row for this customer/date before
    # doing the "already sent?" checks below, so a crashed/restarted
    # worker from a previous attempt can't permanently block retries
    # or make this customer look perpetually "in progress".
    reap_stale_sending_row(customer_id, statement_date)

    customer = get_customer_details(customer_id)

    if not customer:

        return {

            "success": False,

            "reason": "Customer not found"

        }

    if not customer.get("phone"):

        return {

            "success": False,

            "reason": "Customer phone number missing"

        }

    if has_statement_been_sent(customer_id, statement_date):

        return {

            "success": False,
            "reason": "Statement already sent"
        }
    if has_recent_message_to_customer(customer_id, hours=12):
        return {
            "success": False,
            "reason": "A message was already sent to this customer in the last 12 hours. Please wait before sending again."
        }

    summary = get_purchase_summary(

        customer_id,

        statement_date

    )

    statement_link = get_statement_link(

        customer_id,

        statement_date

    )

    token = statement_link.split("/")[-1]

    payload = build_template_payload(

        customer_name=customer["name"],

        statement_date=statement_date.strftime("%d-%b-%Y"),

        purchase_total=summary["purchase_total"],

        purchase_entries=summary["purchase_entries"],

        token=token,

        recipient_number="91" + customer["phone"]

    )

    # Mark this customer as SENDING *before* calling the WhatsApp API.
    # This is what other requests (another user's preview poll, or
    # another tab) see immediately, so they can grey this customer out
    # instead of allowing a second send to be queued up behind ours.
    log_id = create_sending_log(
        customer_id,
        statement_date,
        summary["purchase_total"],
        summary["purchase_entries"]
    )

    result = send_template_message(payload)

    print("WhatsApp Payload:", payload)
    print("WhatsApp Result:", result)

    if result["success"]:

        finalize_whatsapp_log(

            log_id,

            "SUCCESS",

            whatsapp_message_id=result["message_id"]

        )

    else:

        finalize_whatsapp_log(

            log_id,

            "FAILED",

            error_message=str(result["reason"])

        )

    return result
