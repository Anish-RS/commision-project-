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
            datetime.utcnow() + timedelta(days=15)
        ))

        conn.commit()

    cursor.close()
    conn.close()

    return f"{APP_BASE_URL}/s/{token}"
