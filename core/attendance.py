import os
import traceback
from core.database import get_db

DBs_NAME = os.getenv("DBs_NAME")

def get_users_for_enrollment(branch, join_year):
    """Search users from the main users table based on branch and year."""
    conn = get_db(DBs_NAME)
    try:
        cursor = conn.cursor(dictionary=True)
        # We ensure join_year is int. We only filter by branch and year.
        # Ownership (created_by) is NOT used here because we are searching the 'users' table.
        query = "SELECT id, name, username FROM users WHERE branch = %s AND join_year = %s AND is_admin = 0"
        cursor.execute(query, (branch, int(join_year)))
        return cursor.fetchall()
    except Exception as e:
        print(f"Error in get_users_for_enrollment: {e}")
        return []
    finally:
        conn.close()

def create_subject_and_enroll(admin_id, name, branch, year, semester, student_ids):
    """Creates subject and links selected students to it."""
    conn = get_db(DBs_NAME)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO attendance_subjects (subject_name, branch, join_year, semester, created_by)
            VALUES (%s, %s, %s, %s, %s)
        """, (name, branch, year, semester, admin_id))
        subject_id = cursor.lastrowid

        for s_id in student_ids:
            cursor.execute("""
                INSERT INTO attendance_enrollments (subject_id, user_id)
                VALUES (%s, %s)
            """, (subject_id, s_id))
        
        conn.commit()
        return True
    except:
        conn.rollback()
        traceback.print_exc()
        return False
    finally: conn.close()

def get_all_subjects_with_counts(user_id, is_super_admin):
    """Fetches all subjects (Active and Archived) for the Create Subject page list."""
    conn = get_db(DBs_NAME)
    try:
        cursor = conn.cursor(dictionary=True)
        # Select is_archived so the frontend knows which ones to show as archived
        base_query = """
            SELECT s.*, COUNT(e.user_id) as student_count 
            FROM attendance_subjects s
            LEFT JOIN attendance_enrollments e ON s.id = e.subject_id
        """
        if is_super_admin:
            cursor.execute(base_query + " GROUP BY s.id ORDER BY s.is_archived ASC, s.created_at DESC")
        else:
            cursor.execute(base_query + " WHERE s.created_by = %s GROUP BY s.id ORDER BY s.is_archived ASC, s.created_at DESC", (user_id,))
        return cursor.fetchall()
    except: return []
    finally: conn.close()
    
def get_subjects_hierarchical(user_id, is_super_admin):
    """ONLY returns subjects that are NOT archived for the Mark Attendance page."""
    conn = get_db(DBs_NAME)
    try:
        cursor = conn.cursor(dictionary=True)
        # Added AND is_archived = 0
        if is_super_admin:
            cursor.execute("SELECT id, subject_name, branch, semester FROM attendance_subjects WHERE is_archived = 0")
        else:
            cursor.execute("SELECT id, subject_name, branch, semester FROM attendance_subjects WHERE created_by = %s AND is_archived = 0", (user_id,))
        # ... (rest of the formatting logic remains the same)
        rows = cursor.fetchall()
        data = {}
        for r in rows:
            b, s = r['branch'], str(r['semester'])
            if b not in data: data[b] = {}
            if s not in data[b]: data[b][s] = []
            data[b][s].append({'id': r['id'], 'subject_name': r['subject_name']})
        return data
    except: return {}
    finally: conn.close()
    
def archive_subject(subject_id, user_id, is_super_admin):
    """Marks a subject as archived. Regular admins can only archive their own."""
    conn = get_db(DBs_NAME)
    try:
        cursor = conn.cursor()
        if is_super_admin:
            cursor.execute("UPDATE attendance_subjects SET is_archived = 1 WHERE id = %s", (subject_id,))
        else:
            cursor.execute("UPDATE attendance_subjects SET is_archived = 1 WHERE id = %s AND created_by = %s", (subject_id, user_id))
        conn.commit()
        return cursor.rowcount > 0
    except: return False
    finally: conn.close()
    
def set_archive_status(subject_id, status, user_id, is_super_admin):
    """
    Sets is_archived to 1 (Archive) or 0 (Activate).
    Regular admins can only modify their own subjects.
    """
    conn = get_db(DBs_NAME)
    try:
        cursor = conn.cursor()
        if is_super_admin:
            cursor.execute("UPDATE attendance_subjects SET is_archived = %s WHERE id = %s", (status, subject_id))
        else:
            cursor.execute("UPDATE attendance_subjects SET is_archived = %s WHERE id = %s AND created_by = %s", (status, subject_id, user_id))
        conn.commit()
        return cursor.rowcount > 0
    except: return False
    finally: conn.close()    
   
def delete_subject(subject_id, user_id, is_super_admin):
    """Prevents deletion if the subject is archived."""
    conn = get_db(DBs_NAME)
    try:
        cursor = conn.cursor(dictionary=True)
        # Check if subject is archived first
        cursor.execute("SELECT is_archived FROM attendance_subjects WHERE id = %s", (subject_id,))
        res = cursor.fetchone()
        if res and res['is_archived'] == 1:
            return False # Hard block: Cannot delete archived data
            
        if is_super_admin:
            cursor.execute("DELETE FROM attendance_subjects WHERE id = %s", (subject_id,))
        else:
            cursor.execute("DELETE FROM attendance_subjects WHERE id = %s AND created_by = %s", (subject_id, user_id))
        conn.commit()
        return cursor.rowcount > 0
    except: return False
    finally: conn.close()

# --- OTHER UTILITY FUNCTIONS ---

def mark_student_attendance(subject_id, date, present_user_ids, all_eligible_user_ids):
    conn = get_db(DBs_NAME)
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO attendance_sessions (subject_id, class_date) VALUES (%s, %s)", (subject_id, date))
        session_id = cursor.lastrowid
        for u_id in all_eligible_user_ids:
            is_p = 1 if str(u_id) in present_user_ids else 0
            cursor.execute("INSERT INTO attendance_records (session_id, user_id, is_present) VALUES (%s, %s, %s)", (session_id, u_id, is_p))
        conn.commit()
        return True
    except: return False
    finally: conn.close()

def get_user_attendance_stats(user_id):
    conn = get_db(DBs_NAME)
    try:
        cursor = conn.cursor(dictionary=True)
        query = """
            SELECT s.semester, s.subject_name, s.id as subject_id, s.is_archived,
                (SELECT COUNT(*) FROM attendance_sessions sess WHERE sess.subject_id = s.id) as total_classes,
                (SELECT COUNT(*) FROM attendance_records rec 
                 JOIN attendance_sessions sess2 ON rec.session_id = sess2.id 
                 WHERE sess2.subject_id = s.id AND rec.user_id = %s AND rec.is_present = 1) as attended_classes
            FROM attendance_subjects s
            JOIN attendance_enrollments e ON s.id = e.subject_id
            WHERE e.user_id = %s
            ORDER BY s.semester ASC
        """
        cursor.execute(query, (user_id, user_id))
        rows = cursor.fetchall()
        semesters = {}
        for r in rows:
            sem = str(r['semester'])
            if sem not in semesters: semesters[sem] = {'subjects': [], 'overall_attended': 0, 'overall_total': 0, 'overall_perc': 0}
            r['percentage'] = round((r['attended_classes'] / r['total_classes'] * 100), 1) if r['total_classes'] > 0 else 0
            semesters[sem]['subjects'].append(r)
            semesters[sem]['overall_total'] += r['total_classes']
            semesters[sem]['overall_attended'] += r['attended_classes']
        for sem in semesters:
            t, a = semesters[sem]['overall_total'], semesters[sem]['overall_attended']
            semesters[sem]['overall_perc'] = round((a / t * 100), 1) if t > 0 else 0
        # Sort by semester number descending (numeric, not string) so template gets correct order
        semesters = dict(sorted(semesters.items(), key=lambda x: int(x[0]), reverse=True))
        return semesters
    except: return {}
    finally: conn.close()

def get_admin_student_list(branch, join_year, semester):
    """Gets list of students with their total percentage for a specific semester batch."""
    conn = get_db(DBs_NAME)
    try:
        cursor = conn.cursor(dictionary=True)
        # Query specifically calculates stats for subjects in the target semester for that batch
        cursor.execute("""
            SELECT 
                u.id, u.name as display_name, u.username,
                (SELECT COUNT(sess.id) 
                 FROM attendance_sessions sess 
                 JOIN attendance_subjects sub ON sess.subject_id = sub.id 
                 WHERE sub.branch = %s AND sub.join_year = %s AND sub.semester = %s) as total_possible,
                (SELECT COUNT(rec.id) 
                 FROM attendance_records rec 
                 JOIN attendance_sessions sess2 ON rec.session_id = sess2.id 
                 JOIN attendance_subjects sub2 ON sess2.subject_id = sub2.id 
                 WHERE sub2.branch = %s AND sub2.join_year = %s AND sub2.semester = %s 
                 AND rec.user_id = u.id AND rec.is_present = 1) as total_attended
            FROM users u
            WHERE u.branch = %s AND u.join_year = %s AND u.is_admin = 0
            ORDER BY u.name ASC
        """, (branch, join_year, semester, branch, join_year, semester, branch, join_year))
        
        students = cursor.fetchall()
        for s in students:
            h = s['total_possible']
            a = s['total_attended']
            s['overall_perc'] = round((a / h * 100), 1) if h > 0 else 0
        return students
    except:
        return []
    finally:
        conn.close()

def get_semester_rankings(branch, join_year, semester):
    """Calculates rankings based on logic: same % = same rank, then alphabetical."""
    students = get_admin_student_list(branch, join_year, semester)
    
    # FIX: Changed x['name'] to x['display_name'] to match the dictionary keys
    students.sort(key=lambda x: (-x['overall_perc'], x['display_name']))
    
    # Dense ranking: 1,1,2,2 — tied students get same rank,
    # next group always increments by 1 regardless of ties above
    curr_rank, last_perc = 0, -1
    for s in students:
        if s['overall_perc'] != last_perc:
            curr_rank += 1
        s['rank'] = curr_rank
        last_perc = s['overall_perc']
    return students
    
def get_subjects_for_admin(branch, join_year, semester, user_id, is_super_admin):
    """
    Fetches subjects based on filters.
    If not super_admin, only returns subjects created by the specific user_id.
    """
    conn = get_db(DBs_NAME)
    try:
        cursor = conn.cursor(dictionary=True)
        if is_super_admin:
            # Super Admin sees everything matching the search
            query = """
                SELECT * FROM attendance_subjects 
                WHERE branch = %s AND join_year = %s AND semester = %s
            """
            cursor.execute(query, (branch, join_year, semester))
        else:
            # Regular Admin sees only their own subjects matching the search
            query = """
                SELECT * FROM attendance_subjects 
                WHERE branch = %s AND join_year = %s AND semester = %s AND created_by = %s
            """
            cursor.execute(query, (branch, join_year, semester, user_id))
            
        return cursor.fetchall()
    except:
        return []
    finally:
        conn.close()
        
       