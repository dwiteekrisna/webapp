import os
import uuid
import traceback
from core.database import get_db

# Constants for File Handling
UPLOAD_FOLDER = '/home/ubuntu/web/webapp/static/announcement'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'docx', 'txt'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_home_notices(db_name):
    """Fetches all notices from the database."""
    try:
        conn = get_db(db_name)
        if not conn: return []
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM notices ORDER BY created_at DESC")
        notices = cursor.fetchall()
        conn.close()
        return notices
    except Exception:
        print("[ERROR] Failed to fetch notices:")
        traceback.print_exc()
        return []

def handle_post_notice(db_name, user, form, files):
    """Logic for creating a notice with optional attachments."""
    title = form.get('title')
    content = form.get('content')
    file = files.get('attachment')
    
    # FIX: Robust naming logic to avoid KeyError
    creator_name = user.get('display_name') or user.get('name') or "Admin"
    
    file_url = None
    file_type = None

    if file and file.filename != '' and allowed_file(file.filename):
        try:
            ext = file.filename.rsplit('.', 1)[1].lower()
            new_filename = f"{uuid.uuid4().hex}.{ext}"
            if not os.path.exists(UPLOAD_FOLDER):
                os.makedirs(UPLOAD_FOLDER)
            file.save(os.path.join(UPLOAD_FOLDER, new_filename))
            file_url = f"/static/announcement/{new_filename}"
            file_type = ext
        except Exception:
            traceback.print_exc()

    try:
        conn = get_db(db_name)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO notices (title, content, file_path, file_type, created_by) 
            VALUES (%s, %s, %s, %s, %s)
        """, (title, content, file_url, file_type, creator_name))
        conn.commit()
        conn.close()
        return True
    except Exception:
        traceback.print_exc()
        return False
        
def handle_edit_notice(db_name, id, form, files):
    """
    Updates title and content of an existing notice.
    Manages attachment removal and replacement including disk cleanup.
    """
    title = form.get('title')
    content = form.get('content')
    remove_attachment = form.get('remove_attachment') == '1'
    new_file = files.get('attachment')
    
    if not title or not content:
        return False

    try:
        conn = get_db(db_name)
        cursor = conn.cursor(dictionary=True)

        # 1. Fetch current file info to handle deletion
        cursor.execute("SELECT file_path, file_type FROM notices WHERE id = %s", (id,))
        current_notice = cursor.fetchone()
        
        updated_path = current_notice['file_path'] if current_notice else None
        updated_type = current_notice['file_type'] if current_notice else None

        # 2. Handle 'Remove Attachment' request
        if remove_attachment and updated_path:
            full_path = f"/home/ubuntu/web/webapp{updated_path}"
            if os.path.exists(full_path):
                os.remove(full_path)
            updated_path = None
            updated_type = None

        # 3. Handle 'Replace/Add New Attachment' request
        if new_file and new_file.filename != '' and allowed_file(new_file.filename):
            # Delete old file if one existed before replacing
            if updated_path:
                full_path = f"/home/ubuntu/web/webapp{updated_path}"
                if os.path.exists(full_path):
                    os.remove(full_path)

            ext = new_file.filename.rsplit('.', 1)[1].lower()
            new_filename = f"{uuid.uuid4().hex}.{ext}"
            new_file.save(os.path.join(UPLOAD_FOLDER, new_filename))
            updated_path = f"/static/announcement/{new_filename}"
            updated_type = ext

        # 4. Perform the Update
        cursor.execute("""
            UPDATE notices 
            SET title = %s, content = %s, file_path = %s, file_type = %s 
            WHERE id = %s
        """, (title, content, updated_path, updated_type, id))
        
        conn.commit()
        conn.close()
        print(f"[SUCCESS] Notice {id} updated with file management logic.")
        return True
    except Exception:
        print(f"[ERROR] Failed to edit notice {id}:")
        traceback.print_exc()
        return False

def handle_delete_notice(db_name, id):
    """Deletes notice from DB and removes associated file from disk."""
    try:
        conn = get_db(db_name)
        cursor = conn.cursor(dictionary=True)
        
        # Find file to delete from VPS disk
        cursor.execute("SELECT file_path FROM notices WHERE id = %s", (id,))
        notice = cursor.fetchone()
        
        if notice and notice['file_path']:
            full_path = f"/home/ubuntu/web/webapp{notice['file_path']}"
            if os.path.exists(full_path):
                os.remove(full_path)
        
        # Delete DB entry
        cursor.execute("DELETE FROM notices WHERE id = %s", (id,))
        conn.commit()
        conn.close()
        return True
    except Exception:
        traceback.print_exc()
        return False