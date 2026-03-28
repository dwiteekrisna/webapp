import mysql.connector.pooling
import os
import traceback
from dotenv import load_dotenv

load_dotenv("/home/ubuntu/web/webapp/.env")

class DatabaseManager:
    def __init__(self):
        self.pool = None
        try:
            db_config = {
                "host": os.getenv("DB_HOST"),
                "user": os.getenv("DB_USER"),
                "password": os.getenv("DB_PASSWORD"),
                "pool_name": "nexus_pool",
                "pool_size": 20,
                "autocommit": True
            }
            self.pool = mysql.connector.pooling.MySQLConnectionPool(**db_config)
            print("[INFO] Database Pool initialized.")
        except Exception as e:
            print(f"[CRITICAL] Database Pool failed to start: {e}")
            traceback.print_exc()

    def get_connection(self, db_name):
        """Acquires connection and switches DB context safely."""
        if not self.pool:
            print("[ERROR] Database Pool is not available.")
            return None
        try:
            conn = self.pool.get_connection()
            cursor = conn.cursor()
            cursor.execute(f"USE {db_name}")
            cursor.close()
            return conn
        except Exception as e:
            print(f"[ERROR] Failed to switch to DB {db_name}: {e}")
            return None

# Global instance
db_manager = DatabaseManager()

def get_db(db_name):
    return db_manager.get_connection(db_name)