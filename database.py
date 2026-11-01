import os
from psycopg2 import pool
from contextlib import contextmanager

DATABASE_URL = os.getenv("DATABASE_URL")

db_pool = pool.SimpleConnectionPool(1, 10, DATABASE_URL)

@contextmanager
def get_db():
    conn = db_pool.getconn()
    try:
        yield conn
    finally:
        db_pool.putconn(conn)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id SERIAL PRIMARY KEY,
                    tool_name VARCHAR(50),
                    target VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS rat_nodes (
                    id SERIAL PRIMARY KEY,
                    device_type VARCHAR(20),
                    ip_address VARCHAR(50),
                    status VARCHAR(20),
                    connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()