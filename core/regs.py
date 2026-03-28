import os
import traceback
from core.database import get_db

DBs_NAME = os.getenv("DBs_NAME")

def get_user_status(username):
    """Checks if username exists in main or waiting tables."""
    conn = get_db(DBs_NAME)
    if not conn: return "error"
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM users WHERE username = %s", (username,))
        if cursor.fetchone(): return "exists"
        
        cursor.execute("SELECT 1 FROM waiting_users WHERE username = %s", (username,))
        if cursor.fetchone(): return "waiting"
        
        return "none"
    except Exception as e:
        print(f"[REGS ERROR] Status check failed: {e}")
        return "error"
    finally:
        conn.close()

def process_registration(form, bcrypt):
    """Handles data validation and insertion into waiting list."""
    u_name = form.get('username') # College Reg No
    u_pass = form.get('password')
    u_year = int(form.get('join_year', 0))
    u_branch = form.get('branch')
    full_name = form.get('name')

    # 1. Strict Validation
    if u_year <= 2022:
        return {"status": "error", "msg": "Joining year must be 2023 or above."}
    
    # 2. Check Duplicates
    status = get_user_status(u_name)
    if status == "exists": return {"status": "error", "msg": "You already have an account."}
    if status == "waiting": return {"status": "error", "msg": "Wait for approval."}

    # 3. Secure Hashing and DB Insertion
    try:
        hashed_pw = bcrypt.generate_password_hash(u_pass).decode('utf-8')
        conn = get_db(DBs_NAME)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO waiting_users (username, password, join_year, branch, name)
            VALUES (%s, %s, %s, %s, %s)
        """, (u_name, hashed_pw, u_year, u_branch, full_name))
        conn.commit()
        conn.close()
        return {"status": "success", "msg": "Registration submitted for approval."}
    except Exception as e:
        print(f"[REGS ERROR] DB Insertion failed: {e}")
        return {"status": "error", "msg": "System error during registration."}

def fetch_waiting_list():
    """Returns all users waiting for approval."""
    conn = get_db(DBs_NAME)
    if not conn: return []
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM waiting_users ORDER BY created_at DESC")
        res = cursor.fetchall()
        conn.close()
        return res
    except: return []

def handle_admin_action(id, action):
    """Admin logic for Approval or Decline."""
    conn = get_db(DBs_NAME)
    try:
        cursor = conn.cursor(dictionary=True)
        if action == "approve":
            cursor.execute("SELECT * FROM waiting_users WHERE id = %s", (id,))
            u = cursor.fetchone()
            if u:
                cursor.execute("""
                    INSERT INTO users (username, password, join_year, branch, name, is_admin)
                    VALUES (%s, %s, %s, %s, %s, 0)
                """, (u['username'], u['password'], u['join_year'], u['branch'], u['name']))
        
        cursor.execute("DELETE FROM waiting_users WHERE id = %s", (id,))
        conn.commit()
        return True
    except Exception as e:
        print(f"[REGS ERROR] Admin action failed: {e}")
        return False
    finally:
        conn.close()

def process_password_change(user_id, current_p, new_p, bcrypt):
    """Processes password update with verification."""
    conn = get_db(DBs_NAME)
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT password FROM users WHERE id = %s", (user_id,))
        user = cursor.fetchone()
        
        if user and bcrypt.check_password_hash(user['password'], current_p):
            new_hash = bcrypt.generate_password_hash(new_p).decode('utf-8')
            cursor.execute("UPDATE users SET password = %s WHERE id = %s", (new_hash, user_id))
            conn.commit()
            conn.close()
            return {"status": "success"}
        
        conn.close()
        return {"status": "error", "msg": "Incorrect current password."}
    except Exception as e:
        print(f"[REGS ERROR] Pwd change failed: {e}")
        return {"status": "error", "msg": "Server error."}