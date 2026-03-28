import uuid
import os
from datetime import datetime, timedelta
from flask import request
from core.database import get_db

DBs_NAME = os.getenv("DBs_NAME")

def get_current_user():
    sid = request.cookies.get('session_id')
    if not sid: return None
    conn = get_db(DBs_NAME)
    if not conn: return None
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM sessions WHERE session_id = %s", (sid,))
        session_record = cursor.fetchone()
        if not session_record:
            cursor.close()
            conn.close()
            return None
        if session_record['expires_at'] < datetime.now():
            cursor.execute("DELETE FROM sessions WHERE session_id = %s", (sid,))
            conn.commit()
            cursor.close()
            conn.close()
            return None
        cursor.execute("SELECT id as user_id, username, name as display_name, is_admin FROM users WHERE id = %s", (session_record['user_id'],))
        user = cursor.fetchone()
        cursor.close()
        conn.close()
        return user
    except:
        return None

def validate_user_login(username, password, bcrypt):
    """
    Handles logic for:
    1. Uppercase username check.
    2. Password verification.
    3. Waiting list check.
    Returns: (user_dict, error_message)
    """
    u_name = username.strip().upper()
    conn = get_db(DBs_NAME)
    if not conn: return None, "Database Error"
    
    try:
        cursor = conn.cursor(dictionary=True)
        
        # 1. Check active users
        cursor.execute("SELECT * FROM users WHERE username = %s", (u_name,))
        user = cursor.fetchone()
        
        if user:
            if bcrypt.check_password_hash(user['password'], password):
                return user, None
            else:
                return None, "Invalid Credentials"
        
        # 2. Check waiting table (assuming table name is 'waiting_users')
        cursor.execute("SELECT * FROM waiting_users WHERE username = %s", (u_name,))
        waiting = cursor.fetchone()
        
        if waiting:
            return None, "Your account is currently in the waiting process."
            
        return None, "Invalid Credentials"
    except Exception as e:
        return None, "Login Error"
    finally:
        conn.close()

def create_user_session(user_id, is_admin, name):
    sid = str(uuid.uuid4())
    expiry = datetime.now() + timedelta(days=7)
    conn = get_db(DBs_NAME)
    try:
        cursor = conn.cursor()
        
        # Algorithm: Delete existing sessions for this user to prevent multiple device login
        cursor.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        
        cursor.execute("INSERT INTO sessions (user_id, session_id, is_admin, name, expires_at) VALUES (%s, %s, %s, %s, %s)", 
                       (user_id, sid, is_admin, name, expiry))
        conn.commit()
        cursor.close()
        conn.close()
        return sid
    except:
        return None

def delete_session(sid):
    if not sid: return
    conn = get_db(DBs_NAME)
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sessions WHERE session_id = %s", (sid,))
        conn.commit()
        cursor.close()
        conn.close()
    except:
        pass