"""
Backend API cho Hệ thống Điểm danh IoT với Face Recognition & Anti-Spoofing
- Face Recognition: face_recognition library (128D embeddings)
- Anti-Spoofing: MobileNetV2 custom trained model
- Database: MongoDB
- APIs: Student, Class, Session, Attendance Management
"""
from fastapi import FastAPI, UploadFile, File, HTTPException, Form, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional
import uvicorn
import numpy as np
import cv2
import io
import os
import threading
import time
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytz

# Timezone Việt Nam (UTC+7)
VN_TZ = pytz.timezone('Asia/Ho_Chi_Minh')

def get_vn_time():
    """Lấy thời gian hiện tại theo timezone Việt Nam (UTC+7)"""
    return datetime.now(VN_TZ)

# Import database functions
from database import (
    test_connection,
    # Student
    create_student, add_student_embedding, get_student_by_id, get_all_students, delete_student, get_all_embeddings,
    # Class
    create_class, get_all_classes, get_class_by_id, delete_class,
    # Class Members
    add_student_to_class, remove_student_from_class, get_class_students, get_student_classes,
    # Session
    create_session, end_session, get_session_by_id, get_class_sessions, is_session_locked,
    # Attendance
    mark_attendance, get_session_attendances, check_attendance_status, get_attendance_report, update_attendance_status,
    # Spoof
    log_spoof_attempt, get_session_spoof_attempts, mark_spoof_reviewed
)

# Import face recognition utilities
from face_utils import (
    detect_face, get_face_embedding, align_face, get_face_landmarks, 
    load_antispoof_model, check_liveness
)

app = FastAPI(
    title="IoT Face Attendance System",
    description="Hệ thống điểm danh khuôn mặt cho IoT với ESP32",
    version="2.0"
)

# Mount static files
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Create directories for images
os.makedirs("images/attendance", exist_ok=True)  # Lưu ảnh điểm danh thành công
os.makedirs("images/spoof", exist_ok=True)        # Lưu ảnh giả mạo
app.mount("/images", StaticFiles(directory="images"), name="images")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===================================
# GLOBAL STATE
# ===================================

# Current active session
CURRENT_SESSION_ID = None
CURRENT_CLASS_ID = None

# Anti-Spoof Model
ANTISPOOF_MODEL = None
BASE_DIR = Path(__file__).resolve().parent.parent
MODEL_PATH = BASE_DIR / "training_anti_spoof" / "antispoof_model.pth"

# Anti-Spoof Toggle - Cờ bật/tắt chống giả mạo
ANTI_SPOOF_ENABLED = True  # Mặc định BẬT

# Recognition Result (for ESP32 LCD to poll)
RECOGNITION_LOCK = threading.Lock()
LAST_RECOGNITION_RESULT = {"timestamp": 0, "message": "Ready"}

# HTTP Trigger Bridge for ESP32 -> ESP32-CAM
TRIGGER_FLAG = False
TRIGGER_LOCK = threading.Lock()



# ===================================
# AUTO-END SESSION BACKGROUND TASK
# ===================================

def auto_end_session_after_duration(session_id: str, duration_minutes: int):
    """
    Background task: Tự động kết thúc session sau duration_minutes
    """
    time.sleep(duration_minutes * 60)  # Convert to seconds
    
    global CURRENT_SESSION_ID, CURRENT_CLASS_ID
    
    # Kiểm tra session vẫn active
    session = get_session_by_id(session_id)
    if session and session.get("status") == "active":
        print(f"Auto-ending session {session_id} after {duration_minutes} minutes")
        end_session(session_id)
        
        # Clear global state nếu đây là session hiện tại
        if CURRENT_SESSION_ID == session_id:
            CURRENT_SESSION_ID = None
            CURRENT_CLASS_ID = None


# IP of the ESP32-CAM (registered via UDP discovery or HTTP upload)
ESP32_CAM_IP = None


def start_udp_broadcast_listener():
    import socket
    def listener():
        global ESP32_CAM_IP
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Cho phép sử dụng lại địa chỉ/cổng để tránh bị lỗi "Address already in use"
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", 12345))
            print("✓ UDP Broadcast Server listening on port 12345 for auto-discovery")
            while True:
                data, addr = sock.recvfrom(1024)
                message = data.decode("utf-8", errors="ignore").strip()
                if message.startswith("WHERE_IS_THE_SERVER"):
                    print(f"[UDP Broadcast] Received discovery request from {addr[0]}:{addr[1]} ({message})")
                    sock.sendto("I_AM_THE_SERVER".encode("utf-8"), addr)
                    
                    if message == "WHERE_IS_THE_SERVER_CAM":
                        ESP32_CAM_IP = addr[0]
                        print(f"[UDP Broadcast] Registered ESP32-CAM IP: {ESP32_CAM_IP}")
        except Exception as e:
            print(f"✗ UDP Broadcast Server error: {e}")
        finally:
            sock.close()

    thread = threading.Thread(target=listener, daemon=True)
    thread.start()


# ===================================
# STARTUP
# ===================================

@app.on_event("startup")
def startup():
    """Khởi động: kết nối DB và load model"""
    if test_connection():
        print("✓ Database connected")
    else:
        print("✗ Database connection failed")
    
    global ANTISPOOF_MODEL
    if MODEL_PATH.exists():
        ANTISPOOF_MODEL = load_antispoof_model(str(MODEL_PATH))
        if ANTISPOOF_MODEL:
            print("✓ Anti-Spoofing Model loaded")
        else:
            print("⚠ Anti-Spoofing Model failed to load")
    else:
        print(f"⚠ Model file not found: {MODEL_PATH}")
    
    # Khởi động UDP broadcast auto-discovery
    start_udp_broadcast_listener()


# ===================================
# ROOT & HEALTH
# ===================================

@app.get("/")
def root():
    return {
        "message": "IoT Face Attendance System API",
        "version": "2.0",
        "docs": "/docs",
        "ui": "/static/index.html"
    }

@app.get("/api/health")
def health_check():
    return {
        "status": "healthy",
        "timestamp": get_vn_time().isoformat(),
        "session_active": CURRENT_SESSION_ID is not None
    }


# ===================================
# PYDANTIC MODELS
# ===================================

class StudentCreate(BaseModel):
    name: str
    student_id: str
    email: Optional[str] = None
    phone: Optional[str] = None

class ClassCreate(BaseModel):
    class_name: str
    class_code: str
    teacher: Optional[str] = None
    description: Optional[str] = None

class SessionCreate(BaseModel):
    class_id: str
    session_name: str


# ===================================
# STUDENT MANAGEMENT API
# ===================================

@app.post("/api/students/create")
def api_create_student(student: StudentCreate):
    """Tạo sinh viên mới (chưa có ảnh)"""
    try:
        student_mongo_id = create_student(
            name=student.name,
            student_id=student.student_id,
            email=student.email,
            phone=student.phone
        )
        return {
            "status": "success",
            "message": f"Đã tạo sinh viên {student.name}",
            "student_id": student.student_id,
            "_id": student_mongo_id
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/students/{student_id}/add-photo")
async def api_add_student_photo(student_id: str, file: UploadFile = File(...)):
    """
    Thêm ảnh khuôn mặt cho sinh viên
    Recommend: Upload 3-5 ảnh với các góc độ khác nhau
    """
    # Kiểm tra sinh viên có tồn tại không
    student = get_student_by_id(student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Không tìm thấy sinh viên")
    
    # Đọc ảnh
    contents = await file.read()
    nparr = np.frombuffer(contents, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    
    if img is None:
        raise HTTPException(status_code=400, detail="File ảnh không hợp lệ")
    
    # Detect face
    landmarks = get_face_landmarks(img)
    if not landmarks:
        raise HTTPException(status_code=400, detail="Không phát hiện khuôn mặt trong ảnh")
    
    # Align & extract embedding
    aligned_img = align_face(img, landmarks[0])
    embedding = get_face_embedding(aligned_img)
    
    if embedding is None:
        raise HTTPException(status_code=400, detail="Không thể trích xuất đặc trưng khuôn mặt")
    
    # Lưu embedding vào database
    embedding_list = embedding.tolist()
    success = add_student_embedding(student_id, embedding_list)
    
    if success:
        # Lấy số lượng ảnh hiện tại
        updated_student = get_student_by_id(student_id)
        num_photos = len(updated_student.get("embeddings", []))
        
        return {
            "status": "success",
            "message": f"Đã thêm ảnh {num_photos} cho sinh viên {student['name']}",
            "total_photos": num_photos
        }
    else:
        raise HTTPException(status_code=500, detail="Lỗi khi lưu embedding")


@app.get("/api/students")
def api_get_all_students():
    """Lấy danh sách tất cả sinh viên"""
    students = get_all_students()
    return {"status": "success", "students": students}


@app.get("/api/students/{student_id}")
def api_get_student(student_id: str):
    """Lấy thông tin sinh viên"""
    student = get_student_by_id(student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Không tìm thấy sinh viên")
    return {"status": "success", "student": student}


@app.delete("/api/students/{student_id}")
def api_delete_student(student_id: str):
    """Xóa sinh viên"""
    success = delete_student(student_id)
    if success:
        return {"status": "success", "message": f"Đã xóa sinh viên {student_id}"}
    else:
        raise HTTPException(status_code=404, detail="Không tìm thấy sinh viên")


# ===================================
# CLASS MANAGEMENT API
# ===================================

@app.post("/api/classes/create")
def api_create_class(class_obj: ClassCreate):
    """Tạo lớp học mới"""
    try:
        class_id = create_class(
            class_name=class_obj.class_name,
            class_code=class_obj.class_code,
            teacher=class_obj.teacher,
            description=class_obj.description
        )
        return {
            "status": "success",
            "message": f"Đã tạo lớp {class_obj.class_name}",
            "class_id": class_id
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/classes")
def api_get_all_classes():
    """Lấy danh sách tất cả lớp học"""
    classes = get_all_classes()
    return {"status": "success", "classes": classes}


@app.get("/api/classes/{class_id}")
def api_get_class(class_id: str):
    """Lấy thông tin lớp học"""
    class_obj = get_class_by_id(class_id)
    if not class_obj:
        raise HTTPException(status_code=404, detail="Không tìm thấy lớp học")
    return {"status": "success", "class": class_obj}


@app.delete("/api/classes/{class_id}")
def api_delete_class(class_id: str):
    """Xóa lớp học"""
    success = delete_class(class_id)
    if success:
        return {"status": "success", "message": f"Đã xóa lớp học"}
    else:
        raise HTTPException(status_code=404, detail="Không tìm thấy lớp học")


@app.post("/api/classes/{class_id}/students/{student_id}")
def api_add_student_to_class(class_id: str, student_id: str):
    """Thêm sinh viên vào lớp"""
    # Kiểm tra lớp và sinh viên có tồn tại không
    if not get_class_by_id(class_id):
        raise HTTPException(status_code=404, detail="Không tìm thấy lớp học")
    if not get_student_by_id(student_id):
        raise HTTPException(status_code=404, detail="Không tìm thấy sinh viên")
    
    success = add_student_to_class(class_id, student_id)
    if success:
        return {"status": "success", "message": "Đã thêm sinh viên vào lớp"}
    else:
        raise HTTPException(status_code=400, detail="Sinh viên đã có trong lớp")


@app.delete("/api/classes/{class_id}/students/{student_id}")
def api_remove_student_from_class(class_id: str, student_id: str):
    """Xóa sinh viên khỏi lớp"""
    success = remove_student_from_class(class_id, student_id)
    if success:
        return {"status": "success", "message": "Đã xóa sinh viên khỏi lớp"}
    else:
        raise HTTPException(status_code=404, detail="Sinh viên không có trong lớp")


@app.get("/api/classes/{class_id}/students")
def api_get_class_students(class_id: str):
    """Lấy danh sách sinh viên trong lớp"""
    students = get_class_students(class_id)
    return {"status": "success", "students": students}


# ===================================
# SESSION & ATTENDANCE API
# ===================================

@app.post("/api/sessions/start")
def api_start_session(session: SessionCreate, background_tasks: BackgroundTasks):
    """
    Bắt đầu phiên điểm danh cho lớp
    - Tự động kết thúc sau 15 phút
    """
    global CURRENT_SESSION_ID, CURRENT_CLASS_ID
    
    # Kiểm tra lớp có tồn tại không
    class_obj = get_class_by_id(session.class_id)
    if not class_obj:
        raise HTTPException(status_code=404, detail="Không tìm thấy lớp học")
    
    # Tạo session mới (mặc định 15 phút)
    duration_minutes = 15
    session_id = create_session(session.class_id, session.session_name, duration_minutes)
    CURRENT_SESSION_ID = session_id
    CURRENT_CLASS_ID = session.class_id
    
    # Schedule auto-end session sau 15 phút
    background_tasks.add_task(auto_end_session_after_duration, session_id, duration_minutes)
    
    return {
        "status": "success",
        "message": f"Đã bắt đầu điểm danh lớp {class_obj['class_name']} (15 phút)",
        "session_id": session_id,
        "class_id": session.class_id,
        "duration_minutes": duration_minutes
    }


@app.post("/api/sessions/{session_id}/end")
def api_end_session(session_id: str):
    """Kết thúc phiên điểm danh"""
    global CURRENT_SESSION_ID, CURRENT_CLASS_ID
    
    success = end_session(session_id)
    if success:
        if CURRENT_SESSION_ID == session_id:
            CURRENT_SESSION_ID = None
            CURRENT_CLASS_ID = None
        return {"status": "success", "message": "Đã kết thúc phiên điểm danh"}
    else:
        raise HTTPException(status_code=404, detail="Không tìm thấy session")


@app.get("/api/sessions/current")
def api_get_current_session():
    """Lấy thông tin session hiện tại"""
    global CURRENT_SESSION_ID, CURRENT_CLASS_ID
    
    if CURRENT_SESSION_ID:
        session = get_session_by_id(CURRENT_SESSION_ID)
        return {
            "active": True,
            "session_id": CURRENT_SESSION_ID,
            "class_id": CURRENT_CLASS_ID,
            "session": session
        }
    else:
        return {"active": False, "session_id": None}


@app.get("/api/classes/{class_id}/sessions")
def api_get_class_sessions(class_id: str):
    """Lấy danh sách sessions của lớp"""
    sessions = get_class_sessions(class_id)
    return {"status": "success", "sessions": sessions}


@app.get("/api/sessions/{session_id}/attendances")
def api_get_session_attendances(session_id: str):
    """Lấy danh sách sinh viên đã điểm danh"""
    attendances = get_session_attendances(session_id)
    return {"status": "success", "attendances": attendances}


@app.get("/api/sessions/{session_id}/report")
def api_get_attendance_report(session_id: str):
    """Báo cáo điểm danh: có mặt + vắng mặt"""
    session = get_session_by_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Không tìm thấy session")
    
    report = get_attendance_report(session["class_id"], session_id)
    return {"status": "success", "report": report}


# === NEW: Edit Attendance ===
class AttendanceEdit(BaseModel):
    student_id: str
    status: str  # "present" hoặc "absent"


@app.put("/api/sessions/{session_id}/attendance")
def api_edit_attendance(session_id: str, edit: AttendanceEdit):
    """
    Sửa trạng thái điểm danh (chỉ trong 15 phút đầu)
    """
    # Kiểm tra session có bị lock không
    if is_session_locked(session_id):
        raise HTTPException(
            status_code=403, 
            detail="Đã hết thời gian chỉnh sửa điểm danh (15 phút)"
        )
    
    # Update attendance
    success = update_attendance_status(session_id, edit.student_id, edit.status, edited_by="teacher")
    
    if success:
        return {
            "status": "success",
            "message": f"Đã cập nhật trạng thái thành '{edit.status}'"
        }
    else:
        raise HTTPException(status_code=400, detail="Không thể cập nhật")


@app.get("/api/sessions/{session_id}/lock-status")
def api_get_session_lock_status(session_id: str):
    """Kiểm tra session có bị khóa (không cho sửa) không"""
    session = get_session_by_id(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Không tìm thấy session")
    
    is_locked = is_session_locked(session_id)
    now = get_vn_time().replace(tzinfo=None)  # Remove timezone for comparison
    lock_time = session.get("lock_time")
    
    if lock_time and now < lock_time:
        remaining_seconds = int((lock_time - now).total_seconds())
    else:
        remaining_seconds = 0
    
    return {
        "status": "success",
        "is_locked": is_locked,
        "lock_time": lock_time.isoformat() if lock_time else None,
        "remaining_seconds": remaining_seconds
    }


@app.get("/api/sessions/{session_id}/spoof-attempts")
def api_get_spoof_attempts(session_id: str):
    """Lấy danh sách các lần giả mạo trong session"""
    spoofs = get_session_spoof_attempts(session_id)
    return {
        "status": "success",
        "spoof_attempts": spoofs,
        "count": len(spoofs)
    }


@app.put("/api/spoof/{spoof_id}/mark-reviewed")
def api_mark_spoof_reviewed(spoof_id: str):
    """Đánh dấu giảng viên đã xem spoof attempt"""
    success = mark_spoof_reviewed(spoof_id)
    if success:
        return {"status": "success", "message": "Đã đánh dấu đã xem"}
    else:
        raise HTTPException(status_code=404, detail="Không tìm thấy spoof attempt")


# ===================================
# ANTI-SPOOF TOGGLE - Bật/Tắt chống giả mạo
# ===================================

@app.get("/api/anti-spoof/status")
def api_get_anti_spoof_status():
    """Lấy trạng thái hiện tại của chức năng chống giả mạo"""
    global ANTI_SPOOF_ENABLED
    return {
        "status": "success",
        "enabled": ANTI_SPOOF_ENABLED,
        "message": "BẬT" if ANTI_SPOOF_ENABLED else "TẮT"
    }


@app.post("/api/anti-spoof/toggle")
def api_toggle_anti_spoof(request: dict):
    """Bật/tắt chống giả mạo trong quá trình điểm danh"""
    global ANTI_SPOOF_ENABLED
    ANTI_SPOOF_ENABLED = request.get("enabled", True)
    
    status_text = "BẬT" if ANTI_SPOOF_ENABLED else "TẮT"
    
    print(f"\n{'='*60}")
    print(f"⚙️  CHỐNG GIẢ MẠO: {status_text}")
    print(f"{'='*60}\n")
    
    return {
        "status": "success",
        "enabled": ANTI_SPOOF_ENABLED,
        "message": f"Đã {status_text} chống giả mạo"
    }


# ===================================
# FACE RECOGNITION (ESP32)
# ===================================

@app.post("/api/recognize")
async def api_recognize_face(request: Request, file: UploadFile = File(...)):
    """
    Endpoint nhận ảnh từ ESP32-CAM và nhận diện khuôn mặt
    Tự động điểm danh nếu có session active
    """
    global CURRENT_SESSION_ID, ANTISPOOF_MODEL, ANTI_SPOOF_ENABLED, LAST_RECOGNITION_RESULT, ESP32_CAM_IP
    
    # Cập nhật IP của camera từ yêu cầu HTTP này để dự phòng
    if request.client:
        ESP32_CAM_IP = request.client.host
        print(f"[HTTP Request] Updated ESP32-CAM IP from upload: {ESP32_CAM_IP}")
    
    # Kiểm tra có session active không
    if not CURRENT_SESSION_ID:
        with RECOGNITION_LOCK:
            LAST_RECOGNITION_RESULT = {
                "timestamp": get_vn_time().timestamp(),
                "message": "Chua bat dau"
            }
        return JSONResponse(
            status_code=200,
            content={"status": "error", "message": "Chua bat dau"}
        )
    
    # Đọc ảnh
    contents = await file.read()
    nparr = np.frombuffer(contents, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    
    if img is None:
        with RECOGNITION_LOCK:
            LAST_RECOGNITION_RESULT = {
                "timestamp": get_vn_time().timestamp(),
                "message": "Loi anh"
            }
        return JSONResponse(
            status_code=200,
            content={"status": "error", "message": "Loi anh"}
        )
    
    # Detect face
    face_locations = detect_face(img)
    if not face_locations:
        with RECOGNITION_LOCK:
            LAST_RECOGNITION_RESULT = {
                "timestamp": get_vn_time().timestamp(),
                "message": "Ko phat hien"
            }
        return JSONResponse(
            status_code=200,
            content={"status": "failed", "message": "Ko phat hien"}
        )
    
    # Lấy face đầu tiên
    top, right, bottom, left = face_locations[0]
    face_img = img[top:bottom, left:right]
    
    # Anti-Spoofing check (chỉ khi BẬT)
    if ANTI_SPOOF_ENABLED and ANTISPOOF_MODEL:
        is_real, confidence = check_liveness(face_img, ANTISPOOF_MODEL, face_locations[0])
        print(f"Anti-spoofing: is_real={is_real}, confidence={confidence:.3f}")
        
        if not is_real:
            # Lưu ảnh giả mạo
            timestamp_str = get_vn_time().strftime("%Y%m%d_%H%M%S")
            spoof_image_path = f"images/spoof/SPOOF_{timestamp_str}.jpg"
            cv2.imwrite(spoof_image_path, img)
            
            # Log vào database
            log_spoof_attempt(
                session_id=CURRENT_SESSION_ID,
                image_url=f"/{spoof_image_path}",
                confidence=float(confidence)
            )
            
            with RECOGNITION_LOCK:
                LAST_RECOGNITION_RESULT = {
                    "timestamp": get_vn_time().timestamp(),
                    "message": "GIA MAO"
                }
            return JSONResponse(
                status_code=200,
                content={"status": "failed", "message": "GIA MAO", "confidence": confidence}
            )
    elif not ANTI_SPOOF_ENABLED:
        print("⚠️ Anti-spoofing DISABLED - Skipping liveness check")
    
    # Align & extract embedding
    landmarks = get_face_landmarks(img)
    if not landmarks:
        with RECOGNITION_LOCK:
            LAST_RECOGNITION_RESULT = {
                "timestamp": get_vn_time().timestamp(),
                "message": "Ko phat hien"
            }
        return JSONResponse(
            status_code=200,
            content={"status": "failed", "message": "Ko phat hien"}
        )
    
    aligned_img = align_face(img, landmarks[0])
    embedding = get_face_embedding(aligned_img)
    
    if embedding is None:
        with RECOGNITION_LOCK:
            LAST_RECOGNITION_RESULT = {
                "timestamp": get_vn_time().timestamp(),
                "message": "Ko phat hien"
            }
        return JSONResponse(
            status_code=200,
            content={"status": "failed", "message": "Ko phat hien"}
        )
    
    # So sánh với database
    all_embeddings = get_all_embeddings()
    if not all_embeddings:
        with RECOGNITION_LOCK:
            LAST_RECOGNITION_RESULT = {
                "timestamp": get_vn_time().timestamp(),
                "message": "Khong nhan ra"
            }
        return JSONResponse(
            status_code=200,
            content={"status": "failed", "message": "Khong nhan ra"}
        )
    
    # Tìm match
    best_match = None
    best_distance = float('inf')
    THRESHOLD = 0.6  # Cosine distance threshold
    
    for entry in all_embeddings:
        db_embedding = np.array(entry["embedding"])
        # Cosine distance
        distance = 1 - np.dot(embedding, db_embedding) / (
            np.linalg.norm(embedding) * np.linalg.norm(db_embedding)
        )
        
        if distance < best_distance:
            best_distance = distance
            best_match = entry
    
    # Kiểm tra threshold
    if best_distance > THRESHOLD:
        with RECOGNITION_LOCK:
            LAST_RECOGNITION_RESULT = {
                "timestamp": get_vn_time().timestamp(),
                "message": "Khong nhan ra"
            }
        return JSONResponse(
            status_code=200,
            content={"status": "failed", "message": "Khong nhan ra"}
        )
    
    # Nhận diện thành công -> Điểm danh
    student_id = best_match["student_id"]
    student_name = best_match["name"]
    
    # Kiểm tra sinh viên có trong lớp không
    class_students = get_class_students(CURRENT_CLASS_ID)
    class_student_ids = [s["student_id"] for s in class_students]
    
    if student_id not in class_student_ids:
        with RECOGNITION_LOCK:
            LAST_RECOGNITION_RESULT = {
                "timestamp": get_vn_time().timestamp(),
                "message": "Ko thuoc lop"
            }
        return JSONResponse(
            status_code=200,
            content={"status": "failed", "message": f"{student_name} khong thuoc lop nay"}
        )
    
    # Lưu ảnh điểm danh
    timestamp_str = get_vn_time().strftime("%Y%m%d_%H%M%S")
    attendance_image_path = f"images/attendance/{student_id}_{timestamp_str}.jpg"
    cv2.imwrite(attendance_image_path, img)
    image_url = f"/{attendance_image_path}"
    
    # Điểm danh
    is_new = mark_attendance(CURRENT_SESSION_ID, student_id, image_url=image_url, status="present")
    
    if is_new:
        message = student_name
        with RECOGNITION_LOCK:
            LAST_RECOGNITION_RESULT = {
                "timestamp": get_vn_time().timestamp(),
                "message": message
            }
        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message": message,
                "student_id": student_id,
                "new_attendance": True,
                "image_url": image_url
            }
        )
    else:
        # Đã điểm danh rồi
        message = f"{student_name} (Da diem danh)"
        with RECOGNITION_LOCK:
            LAST_RECOGNITION_RESULT = {
                "timestamp": get_vn_time().timestamp(),
                "message": message
            }
        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message": message,
                "student_id": student_id,
                "new_attendance": False,
                "image_url": image_url
            }
        )


@app.get("/api/result/latest")
def api_get_latest_result():
    """
    Endpoint cho ESP32-LCD để lấy kết quả nhận diện mới nhất
    """
    with RECOGNITION_LOCK:
        return LAST_RECOGNITION_RESULT


@app.post("/api/trigger")
def api_post_trigger():
    """
    Endpoint cho Main ESP32 gửi trigger khi phát hiện chuyển động
    """
    global TRIGGER_FLAG
    with TRIGGER_LOCK:
        TRIGGER_FLAG = True
    print("\n[HTTP Bridge] Da nhan tin hieu trigger tu ESP32!")
    return {"status": "success", "message": "Trigger registered"}


@app.get("/api/trigger/check")
def api_check_trigger():
    """
    Endpoint cho ESP32-CAM poll để kiểm tra tín hiệu trigger
    """
    global TRIGGER_FLAG
    with TRIGGER_LOCK:
        has_trigger = TRIGGER_FLAG
        if has_trigger:
            TRIGGER_FLAG = False  # Reset flag ngay sau khi camera lấy đi
    return {"trigger": has_trigger}



@app.get("/api/camera/stream_url")
def api_get_camera_stream_url():
    """
    Lấy URL livestream của camera ESP32-CAM (được phát hiện tự động)
    """
    global ESP32_CAM_IP
    return {
        "status": "success",
        "ip": ESP32_CAM_IP,
        "url": f"http://{ESP32_CAM_IP}:81/stream" if ESP32_CAM_IP else None
    }


@app.get("/api/images/all")
def api_get_all_images():
    """
    Lấy tất cả ảnh đã chụp (attendance + spoof)
    Để hiển thị gallery và so sánh
    """
    all_images = []
    
    # Lấy ảnh attendance (thành công)
    attendance_dir = "images/attendance"
    if os.path.exists(attendance_dir):
        for filename in os.listdir(attendance_dir):
            if filename.endswith(('.jpg', '.jpeg', '.png')):
                filepath = os.path.join(attendance_dir, filename)
                stat = os.stat(filepath)
                
                # Parse filename: {student_id}_{timestamp}.jpg
                try:
                    parts = filename.replace('.jpg', '').split('_')
                    student_id = parts[0]
                    timestamp_str = '_'.join(parts[1:3])  # YYYYMMDD_HHMMSS
                    dt = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
                except:
                    dt = datetime.fromtimestamp(stat.st_mtime)
                    student_id = "Unknown"
                
                all_images.append({
                    "filename": filename,
                    "url": f"/{attendance_dir}/{filename}",
                    "type": "success",
                    "timestamp": dt.isoformat(),
                    "student_id": student_id,
                    "size": stat.st_size
                })
    
    # Lấy ảnh spoof (giả mạo)
    spoof_dir = "images/spoof"
    if os.path.exists(spoof_dir):
        for filename in os.listdir(spoof_dir):
            if filename.endswith(('.jpg', '.jpeg', '.png')):
                filepath = os.path.join(spoof_dir, filename)
                stat = os.stat(filepath)
                
                # Parse filename: SPOOF_{timestamp}.jpg
                try:
                    timestamp_str = filename.replace('SPOOF_', '').replace('.jpg', '')
                    dt = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
                except:
                    dt = datetime.fromtimestamp(stat.st_mtime)
                
                all_images.append({
                    "filename": filename,
                    "url": f"/{spoof_dir}/{filename}",
                    "type": "spoof",
                    "timestamp": dt.isoformat(),
                    "student_id": None,
                    "size": stat.st_size
                })
    
    # Sắp xếp theo thời gian (mới nhất trước)
    all_images.sort(key=lambda x: x['timestamp'], reverse=True)
    
    return {
        "status": "success",
        "total": len(all_images),
        "images": all_images
    }


# ===================================
# RUN SERVER
# ===================================

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        timeout_keep_alive=20,
        limit_concurrency=20,
        limit_max_requests=2000,
        backlog=50
    )
