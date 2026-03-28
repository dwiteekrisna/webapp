from dotenv import load_dotenv
import os
# LOAD DOTENV IMMEDIATELY AT THE TOP
load_dotenv("/home/ubuntu/web/webapp/.env")

from flask import Flask, render_template, request, jsonify, redirect, url_for, Response, stream_with_context, make_response, send_from_directory
from flask_bcrypt import Bcrypt
import json, traceback
import asyncio 

# Essential Base Import
from core.database import get_db
# Import your new Async AI Engine
from core.ai_engine import get_ai_response_stream

# ======================================================
# FAULT TOLERANT DYNAMIC IMPORTS
# ======================================================
def safe_import(module_name, elements):
    try:
        mod = __import__(module_name, fromlist=elements)
        return mod, True
    except Exception as e:
        print(f"[CRITICAL] Module {module_name} failed: {e}")
        return None, False

auth, AUTH_READY = safe_import('core.auth', ['get_current_user', 'create_user_session'])
regs, REGS_READY = safe_import('core.regs', ['get_user_status', 'process_registration', 'fetch_waiting_list', 'handle_admin_action', 'process_password_change'])
home_mod, HOME_READY = safe_import('core.home', ['get_home_notices', 'handle_post_notice', 'handle_edit_notice', 'handle_delete_notice'])
ai_mod, AI_READY = safe_import('core.ai_engine', ['get_ai_response_stream'])
att, ATT_READY = safe_import('core.attendance', ['create_attendance_subject', 'get_subjects_for_admin', 'mark_student_attendance', 'get_user_attendance_stats', 'get_admin_student_list', 'get_semester_rankings'])

app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET_KEY")
bcrypt = Bcrypt(app)
DBs_NAME = os.getenv("DBs_NAME")
# ... (rest of the code remains the same)

@app.context_processor
def inject_ready_vars():
    return dict(AI_STATUS=AI_READY, REGS_STATUS=REGS_READY)

# ======================================================
# ROUTES
# ======================================================

@app.route('/sw.js')
def sw(): return send_from_directory('.', 'sw.js', mimetype='application/javascript')

@app.route('/manifest.json')
def manifest(): return send_from_directory('.', 'manifest.json', mimetype='application/json')

@app.route('/')
def index(): return redirect(url_for('login'))

@app.route('/chat', methods=['GET', 'POST'])
def chat():
    # 1. Fetch user from sessions
    user = auth.get_current_user()
    if not user: 
        return redirect(url_for('login'))

    if request.method == 'POST':
        if not AI_READY: 
            return jsonify({"response": "Lacebit AI is currently offline."}), 503
        
        try:
            msg = request.form.get('message', '')
            hist = json.loads(request.form.get('history', '[]'))
            img = request.files.get('image')
            
            img_bytes = img.read() if img else None
            img_type = img.content_type if img else None
            
            # ─── THE ULTIMATE NAME FIX ───
            # First, try the key that we know works in your HTML template
            u_name = user.get('display_name') or user.get('name')
            
            # Safety Backup: If the session dictionary is missing the name,
            # query the 'users' table directly using the user_id we definitely have.
            if not u_name or str(u_name).strip() in ["", "Student"]:
                try:
                    conn = get_db(DBs_NAME)
                    cursor = conn.cursor(dictionary=True)
                    cursor.execute("SELECT name FROM users WHERE id = %s", (user['user_id'],))
                    db_user = cursor.fetchone()
                    conn.close()
                    if db_user and db_user['name']:
                        u_name = db_user['name']
                except:
                    u_name = "Student"
            # ──────────────────────────────

            def generate():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                # Pass the corrected u_name to the engine
                gen = ai_mod.get_ai_response_stream(msg, hist, img_bytes, img_type, u_name)
                
                try:
                    while True:
                        try:
                            chunk = loop.run_until_complete(gen.__anext__())
                            if chunk: 
                                yield chunk.encode('utf-8')
                        except StopAsyncIteration:
                            break
                        except Exception as e:
                            print(f"[STREAM ERROR] {e}")
                            break
                except GeneratorExit:
                    pass
                finally:
                    try:
                        loop.run_until_complete(gen.aclose())
                    except:
                        pass
                    loop.close()

            response = Response(stream_with_context(generate()), mimetype='text/plain')
            response.headers['X-Accel-Buffering'] = 'no'
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            return response

        except Exception as e:
            print(f"[ROUTE ERROR]: {traceback.format_exc()}")
            return "Server Error", 500

    return render_template('chat.html', user=user)
    
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u_name = request.form.get('username', '').strip()
        u_pass = request.form.get('password', '')
        
        # Use existing logic from auth.py
        user, error_msg = auth.validate_user_login(u_name, u_pass, bcrypt)
        
        if user:
            sid = auth.create_user_session(user['id'], user['is_admin'], user['name'])
            if sid:
                resp = make_response(redirect(url_for('home')))
                resp.set_cookie('session_id', sid, max_age=72460*60, httponly=True, samesite='Lax')
                return resp
            return redirect(url_for('login', error="Session Error"))
        
        # Redirect to the same page with the error as a URL parameter
        return redirect(url_for('login', error=error_msg))

    # GET Part: Read error from URL parameters
    error = request.args.get('error')
    return render_template('login.html', error=error)
   

@app.route('/register', methods=['GET', 'POST'])
def register():
    if not REGS_READY: 
        return "Registration service offline", 503

    if request.method == 'POST':
        reg_data = request.form.to_dict()
        if 'username' in reg_data:
            reg_data['username'] = reg_data['username'].strip().upper()

        # Call existing worker logic
        res = regs.process_registration(reg_data, bcrypt)

        if res['status'] == "success":
            # Redirect on success
            return redirect(url_for('register', success='true', msg=res['msg']))
        else:
            # Redirect on error
            return redirect(url_for('register', error=res['msg']))

    # GET Part: Read status from URL parameters
    error = request.args.get('error')
    success = request.args.get('success') == 'true'
    msg = request.args.get('msg')
    
    return render_template('register.html', error=error, success=success, msg=msg)
    
@app.route('/home')
def home():
    user = auth.get_current_user() if AUTH_READY else None
    if not user: return redirect(url_for('login'))
    # If home_mod fails, we return an empty list instead of crashing
    notices = home_mod.get_home_notices(DBs_NAME) if HOME_READY else []
    return render_template('home.html', user=user, notices=notices)
    

# ======================================================
# ATTENDANCE ROUTES
# ======================================================
@app.route('/attendance/search-users', methods=['GET'])
def search_users():
    try:
        user = auth.get_current_user()
        if not user or not user['is_admin']: 
            return jsonify({"status": "error", "msg": "Unauthorized"}), 403
        
        branch = request.args.get('branch')
        year_raw = request.args.get('year')

        if not branch or not year_raw:
            return jsonify({"status": "error", "msg": "Missing parameters"}), 400

        # FIX: Ensure year is an integer before sending to attendance.py
        try:
            year = int(year_raw)
        except ValueError:
            return jsonify({"status": "error", "msg": "Invalid year format"}), 400

        # Note: Students are public to all admins (no created_by check here)
        users = att.get_users_for_enrollment(branch, year)
        
        # Always return a list
        return jsonify(users if users is not None else [])

    except Exception as e:
        print(f"[CRITICAL ERROR] Scan Students: {e}")
        traceback.print_exc()
        return jsonify({"status": "error", "msg": "Internal Server Error"}), 500
        
@app.route('/attendance/search-subjects', methods=['GET'])
def search_subjects():
    user = auth.get_current_user()
    if not user or not user['is_admin']:
        return jsonify({"status": "error", "msg": "Unauthorized"}), 403

    # Define Super Admin status
    is_super_admin = (user['username'] == 'admin')

    branch = request.args.get('branch')
    year = request.args.get('year')
    sem = request.args.get('semester')

    # Ensure year is handled as int
    try:
        year = int(year)
    except:
        return jsonify({"status": "error", "msg": "Invalid year"}), 400

    # Call the updated function with ownership parameters
    results = att.get_subjects_for_admin(branch, year, sem, user['user_id'], is_super_admin)
    
    return jsonify({"status": "success", "subjects": results})
    
    
@app.route('/attendance/create-subject', methods=['GET', 'POST'])
def create_subject_page():
    user = auth.get_current_user()
    if not user or not user['is_admin']: return redirect('/login')

    # Logic for Super Admin check
    is_super_admin = (user['username'] == 'admin')

    if request.method == 'POST':
        name = request.form.get('subject_name')
        branch = request.form.get('branch')
        year = request.form.get('join_year')
        sem = request.form.get('semester')
        student_ids = request.form.getlist('student_ids')
        if att.create_subject_and_enroll(user['user_id'], name, branch, year, sem, student_ids):
            return jsonify({"status": "success"})
        return jsonify({"status": "error", "msg": "Failed"}), 500

    # Pass is_super_admin and user_id to filter the list
    subjects = att.get_all_subjects_with_counts(user['user_id'], is_super_admin)
    return render_template('create_subject.html', user=user, subjects=subjects)

@app.route('/attendance/mark', methods=['GET', 'POST'])
def att_mark_route():
    user = auth.get_current_user()
    if not user or not user['is_admin']: return redirect('/login')

    is_super_admin = (user['username'] == 'admin')

    if request.method == 'POST':
        subj_id = request.form.get('subject_id')
        date = request.form.get('date')
        present_ids = request.form.getlist('student_ids')
        
        conn = get_db(DBs_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM attendance_enrollments WHERE subject_id = %s", (subj_id,))
        all_eligible = [str(r[0]) for r in cursor.fetchall()]
        conn.close()

        att.mark_student_attendance(subj_id, date, present_ids, all_eligible)
        return jsonify({"status": "success"})

    # Filter dropdowns based on who created the subject
    subjects_json = att.get_subjects_hierarchical(user['user_id'], is_super_admin)
    return render_template('mark_attendance.html', user=user, subjects_json=subjects_json)

@app.route('/attendance/delete-subject/<int:subject_id>', methods=['POST'])
def delete_subject_route(subject_id):
    user = auth.get_current_user()
    if not user or not user['is_admin']: return jsonify({"status": "error"}), 403
    
    is_super_admin = (user['username'] == 'admin')
    
    if att.delete_subject(subject_id, user['user_id'], is_super_admin):
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "msg": "You do not have permission to delete this subject"}), 403
    
@app.route('/attendance/archive-subject/<int:subject_id>', methods=['POST'])
def att_archive_subject(subject_id):
    user = auth.get_current_user()
    if not user or not user['is_admin']: return jsonify({"status": "error"}), 403
    
    is_super_admin = (user['username'] == 'admin')
    if att.archive_subject(subject_id, user['user_id'], is_super_admin):
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "msg": "Failed to archive subject"}), 500

@app.route('/attendance/toggle-archive/<int:subject_id>', methods=['POST'])
def att_toggle_archive(subject_id):
    user = auth.get_current_user()
    if not user or not user['is_admin']: return jsonify({"status": "error"}), 403
    
    is_super_admin = (user['username'] == 'admin')
    # Get status from request: 1 for archive, 0 for activate
    status = request.form.get('status', type=int)
    
    if att.set_archive_status(subject_id, status, user['user_id'], is_super_admin):
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "msg": "Action failed"}), 500    
    
@app.route('/attendance/students', methods=['GET'])
def get_enrolled_students():
    user = auth.get_current_user()
    if not user or not user['is_admin']: return jsonify({"status": "error"}), 403
    
    subj_id = request.args.get('subject_id')
    if not subj_id:
        return jsonify({"status": "success", "students": []})

    conn = get_db(DBs_NAME)
    cursor = conn.cursor(dictionary=True)
    # This query finds students linked to the specific subject via enrollment table
    cursor.execute("""
        SELECT u.id, u.name as display_name, u.username 
        FROM users u
        JOIN attendance_enrollments e ON u.id = e.user_id
        WHERE e.subject_id = %s
    """, (subj_id,))
    students = cursor.fetchall()
    conn.close()
    return jsonify({"status": "success", "students": students})

# FIX: Changed path to '/attendance' to match your Sidebar link
@app.route('/attendance')
def att_my_stats():
    user = auth.get_current_user()
    if not user: return redirect('/login')
    stats = att.get_user_attendance_stats(user['user_id'])
    return render_template('user_attendance.html', stats=stats, user=user)

# FIX: Match the ranking call from user_attendance.html
@app.route('/attendance/rank', methods=['GET'])
def att_rank_view():
    user = auth.get_current_user()
    if not user: return jsonify({"status": "error"}), 401
    
    sem = request.args.get('semester')
    
    # FIX: Fetch branch/join_year from DB because it's not in the session cookie
    conn = get_db(DBs_NAME)
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT branch, join_year FROM users WHERE id = %s", (user['user_id'],))
    db_user = cursor.fetchone()
    conn.close()
    
    if not db_user: return jsonify({"status": "error", "msg": "User data not found"}), 404
    
    rankings = att.get_semester_rankings(db_user['branch'], db_user['join_year'], sem)
    return jsonify({"status": "success", "students": rankings})

@app.route('/attendance/subject-rank/<int:subject_id>', methods=['GET'])
def att_subject_rank(subject_id):
    user = auth.get_current_user()
    if not user or not user['is_admin']: return jsonify({"status": "error"}), 401

    conn = get_db(DBs_NAME)
    cursor = conn.cursor(dictionary=True)
    # Calculate ranking for one specific subject only
    cursor.execute("""
        SELECT u.id, u.name as display_name, u.username,
            (SELECT COUNT(*) FROM attendance_sessions s WHERE s.subject_id = %s) as total_classes,
            (SELECT COUNT(*) FROM attendance_records r
             JOIN attendance_sessions s2 ON r.session_id = s2.id
             WHERE s2.subject_id = %s AND r.user_id = u.id AND r.is_present = 1) as attended_classes
        FROM users u
        JOIN attendance_enrollments e ON u.id = e.user_id
        WHERE e.subject_id = %s
    """, (subject_id, subject_id, subject_id))
    students = cursor.fetchall()
    conn.close()
    
    for s in students:
        t = s['total_classes']
        s['overall_perc'] = round((s['attended_classes'] / t * 100), 1) if t > 0 else 0
    
    # Sort by percentage Desc, then Name Asc
    students.sort(key=lambda x: (-x['overall_perc'], x['display_name']))
    return jsonify({"status": "success", "students": students})
# --- RESTORED NOTICE BOARD ROUTES ---

@app.route('/post-notice', methods=['POST'])
def post_notice():
    user = auth.get_current_user()
    if not user or not user['is_admin'] or not HOME_READY:
        return redirect(url_for('login'))
    home_mod.handle_post_notice(DBs_NAME, user, request.form, request.files)
    return redirect(url_for('home'))

@app.route('/edit-notice/<int:id>', methods=['POST'])
def edit_notice(id):
    user = auth.get_current_user()
    if not user or not user['is_admin'] or not HOME_READY:
        return redirect(url_for('login'))
    home_mod.handle_edit_notice(DBs_NAME, id, request.form, request.files)
    return redirect(url_for('home'))

@app.route('/delete-notice/<int:id>', methods=['POST'])
def delete_notice(id):
    user = auth.get_current_user()
    if not user or not user['is_admin'] or not HOME_READY:
        return redirect(url_for('login'))
    home_mod.handle_delete_notice(DBs_NAME, id)
    return redirect(url_for('home'))

# --- END OF NOTICE BOARD ROUTES ---

@app.route('/waiting')
def waiting():
    user = auth.get_current_user() if AUTH_READY else None
    if not user or not user['is_admin']: return redirect(url_for('home'))
    pending = regs.fetch_waiting_list() if REGS_READY else []
    return render_template('waiting.html', user=user, pending=pending)

@app.route('/approve-user/<int:id>', methods=['POST'])
def approve(id):
    user = auth.get_current_user()
    if user and user['is_admin'] and REGS_READY: regs.handle_admin_action(id, "approve")
    return redirect(url_for('waiting'))

@app.route('/decline-user/<int:id>', methods=['POST'])
def decline(id):
    user = auth.get_current_user()
    if user and user['is_admin'] and REGS_READY: regs.handle_admin_action(id, "decline")
    return redirect(url_for('waiting'))

@app.route('/change-password', methods=['POST'])
def change_pass():
    user = auth.get_current_user()
    if not user or not REGS_READY: return jsonify({"error": "Unauthorized"}), 401
    res = regs.process_password_change(user['user_id'], request.form.get('current_password'), request.form.get('new_password'), bcrypt)
    return jsonify(res), (200 if res['status'] == "success" else 400)


# --- RESTORED LOGOUT ROUTE ---
@app.route('/logout')
def logout():
    sid = request.cookies.get('session_id')
    if sid and AUTH_READY: auth.delete_session(sid)
    resp = make_response(redirect(url_for('login')))
    resp.set_cookie('session_id', '', expires=0)
    return resp

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
    