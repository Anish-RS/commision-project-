import os
import uuid
import requests
from datetime import datetime, timedelta

from psycopg2.extras import RealDictCursor

from db import get_connection


ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
APP_BASE_URL = os.getenv("APP_BASE_URL")


def generate_token():
    return str(uuid.uuid4())


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
    statement_link,
    recipient_number
):
    """
    Creates the payload required by the WhatsApp Cloud API.
    """

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
                        },

                        {
                            "type": "text",
                            "text": statement_link
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
    phone_number,
    statement_date,
    purchase_total,
    purchase_entries,
    status,
    response,
    message_id=None
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
                phone_number,
                statement_date,
                purchase_total,
                purchase_entries,
                status,
                message_id,
                response
            )
            VALUES
            (
                %s,%s,%s,%s,%s,%s,%s,%s
            )
        """, (

            customer_id,
            phone_number,
            statement_date,
            purchase_total,
            purchase_entries,
            status,
            message_id,
            str(response)

        ))

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

            AND status='SUCCESS'

        """, (

            customer_id,
            statement_date

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

