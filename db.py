import os
import psycopg2
from psycopg2.extras import RealDictCursor

def get_connection():
    # Automatically tracks Vercel Postgres URL or fallback Neon DATABASE_URL environment variables
    db_url = os.environ.get("POSTGRES_URL") or os.environ.get("DATABASE_URL")
    
    if not db_url:
        raise RuntimeError(
            "Database connection URL string not found in environment variables. "
            "Please configure POSTGRES_URL or DATABASE_URL in Vercel settings."
        )
        
    # Standard connection initialization for PostgreSQL
    return psycopg2.connect(db_url)