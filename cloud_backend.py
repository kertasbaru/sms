from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from typing import Optional, List, Dict, Any
from datetime import datetime
import sqlite3
from pathlib import Path
import hashlib
import uuid
import os
import json
import base64
import requests
from cryptography.fernet import Fernet
import logging
import sys

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the existing cloud client
try:
    from database.cloud_db import SQLiteCloudClient
except ImportError:
    try:
        from app.database.cloud_db import SQLiteCloudClient
    except ImportError:
        # Fallback
        import sys
        sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from database.cloud_db import SQLiteCloudClient

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="School Recovery Server", 
    description="Separate backend for school account recovery",
    version="2.1.0"
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://localhost:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================
# CONFIGURATION
# ============================================

class Config:
    # MUST match main.py's RECOVERY_SECRET
    RECOVERY_SECRET = os.getenv("RECOVERY_SECRET", "CHANGE_ME_IN_PRODUCTION")
    
    # Database paths
    PROJECT_ROOT = Path(__file__).parent
    LOCAL_DB_PATH = PROJECT_ROOT / "database" / "recovery_school.db"
    
    # Connection settings
    MAX_RETRIES = 3
    RETRY_DELAY = 1  # seconds

config = Config()

# Initialize cloud client
cloud_client = SQLiteCloudClient()

# ============================================
# REQUEST MODELS
# ============================================

class SchoolCheckRequest(BaseModel):
    email: str
    
    @validator('email')
    def validate_email(cls, v):
        if not v or '@' not in v:
            raise ValueError('Invalid email address')
        return v.strip().lower()

class SchoolRecoveryRequest(BaseModel):
    email: str
    school_name: str
    contact: str
    confirm_deactivation: bool = False
    
    @validator('email')
    def validate_email(cls, v):
        if not v or '@' not in v:
            raise ValueError('Invalid email address')
        return v.strip().lower()
    
    @validator('school_name')
    def validate_school_name(cls, v):
        if not v or len(v.strip()) < 2:
            raise ValueError('School name must be at least 2 characters')
        return v.strip()
    
    @validator('contact')
    def validate_contact(cls, v):
        if not v or len(v.strip()) < 6:
            raise ValueError('Contact number must be at least 6 characters')
        return v.strip()

class RecoveryImportRequest(BaseModel):
    school_email: str
    encrypted_backup: str
    
    @validator('school_email')
    def validate_email(cls, v):
        if not v or '@' not in v:
            raise ValueError('Invalid email address')
        return v.strip().lower()

# ============================================
# LOCAL DATABASE FUNCTIONS
# ============================================

def get_local_db_connection():
    """Get connection to local recovery SQLite database"""
    try:
        config.LOCAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(config.LOCAL_DB_PATH))
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        logger.error(f"Error connecting to local database: {e}")
        raise

def initialize_recovery_database():
    """Initialize the local recovery database with necessary tables"""
    try:
        conn = get_local_db_connection()
        cursor = conn.cursor()
        
        # Create users table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recovered_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unique_id TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL UNIQUE,
                email TEXT UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (
                    role IN ('admin', 'teacher', 'ta', 'accountant', 'student')
                ),
                status TEXT NOT NULL DEFAULT 'active' CHECK (
                    status IN ('active', 'suspended', 'disabled')
                ),
                last_login DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                recovered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                cloud_user_id INTEGER,
                original_cloud_data TEXT
            )
        """)
        
        # Create school_info table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recovered_school_info (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                school_name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                phone TEXT,
                address TEXT,
                city TEXT,
                state TEXT,
                country TEXT,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                recovered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                cloud_school_id INTEGER,
                original_cloud_data TEXT
            )
        """)
        
        # Create activation_state table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recovery_activation_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                activated BOOLEAN NOT NULL DEFAULT FALSE,
                activation_code TEXT,
                machine_fingerprint TEXT,
                school_name TEXT,
                activated_at DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                recovered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                CHECK (id = 1)
            )
        """)
        
        # Ensure activation_state has the single row
        cursor.execute("SELECT id FROM recovery_activation_state WHERE id = 1")
        if not cursor.fetchone():
            cursor.execute("""
                INSERT INTO recovery_activation_state 
                (id, activated, created_at, updated_at)
                VALUES (1, FALSE, ?, ?)
            """, (datetime.now().isoformat(), datetime.now().isoformat()))
        
        # Create recovery_attempts table (replacing recovery_logs)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recovery_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                ip_address TEXT,
                timestamp DATETIME NOT NULL,
                success BOOLEAN NOT NULL,
                recovery_type TEXT NOT NULL,
                details TEXT,
                synced_to_cloud BOOLEAN DEFAULT FALSE
            )
        """)
        
        conn.commit()
        conn.close()
        logger.info("✅ Recovery database initialized")
        
        # Try to create recovery_attempts table in cloud
        create_cloud_recovery_table()
        
    except Exception as e:
        logger.error(f"❌ Error initializing recovery database: {e}")
        raise

def create_cloud_recovery_table():
    """Create recovery_attempts table in cloud database"""
    try:
        if cloud_client.check_connection():
            # Check if table exists
            result = cloud_client.execute_query("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='recovery_attempts'
            """)
            
            if not result.get("rows"):
                logger.info("Creating recovery_attempts table in cloud...")
                cloud_client.execute_query("""
                    CREATE TABLE IF NOT EXISTS recovery_attempts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        email TEXT NOT NULL,
                        ip_address TEXT,
                        timestamp DATETIME NOT NULL,
                        success BOOLEAN NOT NULL,
                        recovery_type TEXT NOT NULL,
                        details TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                logger.info("✅ Cloud recovery table initialized")
    except Exception as e:
        logger.error(f"⚠️ Could not create cloud recovery table: {e}")

# Initialize database on startup
initialize_recovery_database()

# ============================================
# UTILITY FUNCTIONS
# ============================================

def hash_password(password: str) -> str:
    """Hash password for storage"""
    return hashlib.sha256(password.encode()).hexdigest()

def log_recovery_attempt(email: str, recovery_type: str, status: str, 
                        details: str = None, ip_address: str = "0.0.0.0"):
    """Log recovery attempts to both local and cloud databases"""
    
    success = 1 if status.lower() == "success" else 0
    
    # Log to local database
    try:
        conn = get_local_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO recovery_attempts 
            (email, ip_address, timestamp, success, recovery_type, details, synced_to_cloud)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            email, 
            ip_address, 
            datetime.now().isoformat(), 
            success, 
            recovery_type, 
            details,
            0  # not synced yet
        ))
        conn.commit()
        conn.close()
        logger.debug(f"Logged to local DB: {email} - {recovery_type} - {status}")
    except Exception as e:
        logger.error(f"Error logging to local DB: {e}")
    
    # Try to log to cloud
    try:
        if cloud_client.check_connection():
            cloud_client.execute_query("""
                INSERT INTO recovery_attempts
                (email, ip_address, timestamp, success, recovery_type, details)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                email, 
                ip_address, 
                datetime.now().isoformat(), 
                success, 
                recovery_type, 
                details
            ))
            logger.debug(f"Logged to cloud DB: {email}")
    except Exception as e:
        logger.error(f"Could not log to cloud: {e}")

def derive_recovery_key(school_email: str) -> bytes:
    """Derive encryption key from school email and secret"""
    raw = f"{school_email}:{config.RECOVERY_SECRET}".encode()
    digest = hashlib.sha256(raw).digest()
    return base64.urlsafe_b64encode(digest[:32])

def create_recovery_blob(school_data: dict, admins: list) -> str:
    """Create encrypted recovery blob matching main app's format"""
    # Create payload
    payload = {
        "schema_version": 1,
        "school": {
            "school_name": school_data.get("school_name"),
            "school_email": school_data.get("school_email"),
            "school_contact": school_data.get("school_contact"),
            "county": school_data.get("county"),
            "region": school_data.get("region"),
            "city": school_data.get("city"),
            "town": school_data.get("town", ""),
            "gps_address": school_data.get("gps_address", ""),
            "manufacture_code": school_data.get("manufacture_code", ""),
            "created_at": school_data.get("created_at")
        },
        "admins": [
            {
                "first_name": admin.get("first_name"),
                "middle_name": admin.get("middle_name", ""),
                "last_name": admin.get("last_name"),
                "contact": admin.get("contact"),
                "email": admin.get("email"),
                "password_hash": admin.get("password_hash"),
                "created_at": admin.get("created_at")
            }
            for admin in admins
        ],
        "issued_at": datetime.now().isoformat()
    }
    
    # Encrypt with Fernet
    key = derive_recovery_key(school_data["school_email"])
    fernet = Fernet(key)
    
    json_str = json.dumps(payload, default=str)
    encrypted = fernet.encrypt(json_str.encode())
    
    return encrypted.decode()

def execute_cloud_query(query: str, params: tuple = None) -> Dict[str, Any]:
    """Execute a query on SQLiteCloud using the cloud client"""
    try:
        if not cloud_client.check_connection():
            cloud_client.connect()
        
        return cloud_client.execute_query(query, params)
    except Exception as e:
        logger.error(f"Cloud query error: {e}")
        return {
            "success": False,
            "error": str(e)
        }

# ============================================
# RECOVERY ENDPOINTS
# ============================================

@app.get("/")
async def root():
    """Root endpoint - server status"""
    return {
        "service": "school-recovery-server",
        "status": "online",
        "version": "2.1.0",
        "timestamp": datetime.now().isoformat(),
        "features": [
            "cloud_recovery",
            "encrypted_blobs",
            "direct_import"
        ]
    }

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "components": {}
    }
    
    # Check cloud connection
    try:
        cloud_online = cloud_client.check_connection()
        health_status["components"]["cloud"] = "connected" if cloud_online else "disconnected"
        if not cloud_online:
            health_status["status"] = "degraded"
    except Exception as e:
        health_status["components"]["cloud"] = f"error: {str(e)}"
        health_status["status"] = "degraded"
    
    # Check local database
    try:
        conn = get_local_db_connection()
        conn.execute("SELECT 1")
        conn.close()
        health_status["components"]["local_db"] = "connected"
    except Exception as e:
        health_status["components"]["local_db"] = f"error: {str(e)}"
        health_status["status"] = "degraded"
    
    return health_status

@app.post("/check-school")
async def check_school_exists(req: SchoolCheckRequest, request: Request):
    """Check if school exists in cloud database"""
    client_ip = request.client.host
    print(f"🔍 [CHECK-SCHOOL] Request received for email: {req.email} from IP: {client_ip}")
    print(f"🔍 [CHECK-SCHOOL] Request data: {req.dict()}")
    
    try:
        # Check cloud connection
        print(f"🔍 [CHECK-SCHOOL] Testing cloud connection...")
        cloud_connected = cloud_client.check_connection()
        print(f"🔍 [CHECK-SCHOOL] Cloud connection result: {cloud_connected}")
        
        if not cloud_connected:
            print(f"❌ [CHECK-SCHOOL] Cloud connection failed")
            log_recovery_attempt(req.email, "check_school", "failed", "Cloud not connected", client_ip)
            raise HTTPException(
                status_code=503, 
                detail="Cannot connect to cloud database. Please check your internet connection."
            )
        
        print(f"✅ [CHECK-SCHOOL] Cloud connection successful")
        
        # Query cloud database for school
        print(f"🔍 [CHECK-SCHOOL] Executing query for email: {req.email}")
        query = """
            SELECT id, school_name, school_email, school_contact, 
                   county, region, city, town, gps_address, 
                   manufacture_code, created_at
            FROM school_installations 
            WHERE school_email = ? 
            LIMIT 1
        """
        
        result = execute_cloud_query(query, (req.email,))
        print(f"🔍 [CHECK-SCHOOL] Query result: {result}")
        
        if result.get("success") and result.get("rows"):
            school = result["rows"][0]
            print(f"✅ [CHECK-SCHOOL] School found in database: {school}")
            
            sanitized_school = {
                "id": school.get("id"),
                "school_name": school.get("school_name"),
                "school_email": school.get("school_email"),
                "school_contact": school.get("school_contact"),
                "county": school.get("county"),
                "region": school.get("region"),
                "city": school.get("city"),
                "created_at": school.get("created_at")
            }
            print(f"✅ [CHECK-SCHOOL] Sanitized school data: {sanitized_school}")
            
            log_recovery_attempt(req.email, "check_school", "success", f"School found: {school.get('school_name')}", client_ip)
            
            response_data = {
                "success": True,
                "exists": True,
                "school": sanitized_school,
                "message": f"School found: {school.get('school_name')}"
            }
            print(f"✅ [CHECK-SCHOOL] Returning success response: {response_data}")
            return response_data
        else:
            print(f"⚠️ [CHECK-SCHOOL] No school found for email: {req.email}")
            log_recovery_attempt(req.email, "check_school", "failed", "School not found", client_ip)
            
            response_data = {
                "success": True,
                "exists": False,
                "message": "No school found with this email address."
            }
            print(f"⚠️ [CHECK-SCHOOL] Returning not found response: {response_data}")
            return response_data
            
    except HTTPException as he:
        print(f"❌ [CHECK-SCHOOL] HTTP Exception: {he.detail}")
        raise
    except Exception as e:
        print(f"❌ [CHECK-SCHOOL] Unexpected error: {str(e)}")
        import traceback
        traceback.print_exc()
        log_recovery_attempt(req.email, "check_school", "error", str(e), client_ip)
        raise HTTPException(status_code=500, detail=f"Failed to check school: {str(e)}")
@app.post("/verify-recovery")
async def verify_school_recovery(req: SchoolRecoveryRequest, request: Request):
    """Verify school recovery details and get admin information"""
    client_ip = request.client.host
    
    try:
        if not cloud_client.check_connection():
            raise HTTPException(status_code=503, detail="Cannot connect to cloud database")
        
        # First, verify school exists
        check_result = await check_school_exists(SchoolCheckRequest(email=req.email), request)
        
        if not check_result.get("exists"):
            return {
                "success": False,
                "message": "School not found. Please check the email address."
            }
        
        school_data = check_result["school"]
        
        # Verify school name and contact match
        if (school_data.get("school_name", "").strip().lower() != req.school_name.strip().lower()):
            return {
                "success": False,
                "message": "School name does not match our records."
            }
        
        if (school_data.get("school_contact", "").strip() != req.contact.strip()):
            return {
                "success": False,
                "message": "Contact number does not match our records."
            }
        
        # Get admin information from cloud
        query = """
            SELECT id, first_name, middle_name, last_name, 
                   contact, email, password_hash, created_at
            FROM admin_table 
            WHERE school_id = ? 
            ORDER BY created_at DESC
        """
        
        result = execute_cloud_query(query, (school_data["id"],))
        
        if not result.get("success"):
            raise HTTPException(status_code=503, detail="Failed to query admin data")
        
        admins = result.get("rows", [])
        
        if not admins:
            return {
                "success": False,
                "message": "No admin accounts found for this school."
            }
        
        # Format admins for response
        formatted_admins = []
        for admin in admins:
            formatted_admins.append({
                "first_name": admin.get("first_name"),
                "last_name": admin.get("last_name"),
                "email": admin.get("email"),
                "contact": admin.get("contact")
            })
        
        log_recovery_attempt(req.email, "verify_recovery", "success", f"Verified: {school_data['school_name']}", client_ip)
        
        return {
            "success": True,
            "verified": True,
            "message": "School verification successful",
            "data": {
                "school": school_data,
                "admin_count": len(admins),
                "admins": formatted_admins
            },
            "warning": "Recovery will deactivate any existing device and require reactivation."
        }
        
    except HTTPException:
        raise
    except Exception as e:
        log_recovery_attempt(req.email, "verify_recovery", "error", str(e), client_ip)
        raise HTTPException(status_code=500, detail=f"Verification failed: {str(e)}")

# @app.post("/perform-recovery")
# async def perform_school_recovery(req: SchoolRecoveryRequest, request: Request):
#     """Perform the complete school recovery process"""
#     client_ip = request.client.host
#     logger.info(f"🔍 /perform-recovery called for email: {req.email} from IP: {client_ip}")
    
#     try:
#         # Input validation
#         if not req.confirm_deactivation:
#             raise HTTPException(
#                 status_code=400, 
#                 detail="You must confirm device deactivation to proceed with recovery."
#             )
        
#         if not cloud_client.check_connection():
#             raise HTTPException(status_code=503, detail="Cannot connect to cloud database")
        
#         # Step 1: Verify school details
#         verify_result = await verify_school_recovery(req, request)
        
#         if not verify_result.get("verified"):
#             return {
#                 "success": False,
#                 "message": verify_result.get("message", "Verification failed")
#             }
        
#         school_data = verify_result["data"]["school"]
#         school_id = school_data.get("id")
        
#         if not school_id:
#             raise HTTPException(status_code=500, detail="School ID not found in verification data")
        
#         # Step 2: Get full admin data (including password hashes)
#         admin_query = """
#             SELECT id, first_name, middle_name, last_name, 
#                    contact, email, password_hash, created_at
#             FROM admin_table 
#             WHERE school_id = ? 
#             ORDER BY created_at DESC
#         """
        
#         admin_result = execute_cloud_query(admin_query, (school_id,))
        
#         if not admin_result.get("success"):
#             raise HTTPException(status_code=503, detail="Failed to query admin data")
        
#         admins = admin_result.get("rows", [])
        
#         if not admins:
#             raise HTTPException(status_code=404, detail="No admin accounts found for this school.")
        
#         # Step 3: Get full school data for blob creation
#         full_school_query = """
#             SELECT * FROM school_installations WHERE id = ?
#         """
#         full_school_result = execute_cloud_query(full_school_query, (school_id,))
        
#         if full_school_result.get("success") and full_school_result.get("rows"):
#             full_school_data = full_school_result["rows"][0]
#         else:
#             full_school_data = school_data
        
#         # Step 4: Create encrypted recovery blob
#         try:
#             encrypted_blob = create_recovery_blob(full_school_data, admins)
#             logger.info(f"✅ Created blob of lengtllh {len(encrypted_blob)}")
#         except Exception as blob_error:
#             logger.error(f"Failed to create blob: {blob_error}")
#             encrypted_blob = None
        
#         # Step 5: Save to local database
#         conn = None
#         try:
#             conn = get_local_db_connection()
#             cursor = conn.cursor()
            
#             # Clear existing recovery data for this school
#             cursor.execute("DELETE FROM recovered_school_info WHERE email = ?", (req.email,))
#             cursor.execute("DELETE FROM recovered_users WHERE email = ?", (req.email,))
            
#             # Save school info
#             school_name = school_data.get("school_name", "").strip()
#             school_email = school_data.get("school_email", "").strip()
#             school_contact = school_data.get("school_contact", "").strip()
            
#             # Prepare address
#             town = full_school_data.get("town", "") if isinstance(full_school_data, dict) else ""
#             city = full_school_data.get("city", "") if isinstance(full_school_data, dict) else school_data.get("city", "")
#             region = full_school_data.get("region", "") if isinstance(full_school_data, dict) else school_data.get("region", "")
#             county = full_school_data.get("county", "") if isinstance(full_school_data, dict) else school_data.get("county", "")
            
#             address = f"{town}, {city}".strip(", ")
#             if not address or address == ", ":
#                 address = city if city else "Unknown"
            
#             cursor.execute("""
#                 INSERT INTO recovered_school_info 
#                 (school_name, email, phone, address, city, state, country, 
#                  created_at, updated_at, recovered_at, cloud_school_id, original_cloud_data)
#                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
#             """, (
#                 school_name,
#                 school_email,
#                 school_contact,
#                 address,
#                 city,
#                 region,
#                 county,
#                 datetime.now().isoformat(),
#                 datetime.now().isoformat(),
#                 datetime.now().isoformat(),
#                 school_id,
#                 json.dumps(full_school_data if isinstance(full_school_data, dict) else {}, default=str)
#             ))
            
#             # Save admins
#             saved_admin_ids = []
#             for admin in admins:
#                 unique_id = str(uuid.uuid4())
#                 email = admin.get("email", "").strip()
#                 contact = admin.get("contact", "").strip()
                
#                 username = email or contact or f"admin_{unique_id[:8]}"
#                 password_hash = admin.get("password_hash") or hash_password("temporary_password")
                
#                 # Prepare original data (without password hash)
#                 original_data = {k: v for k, v in admin.items() if k != 'password_hash'}
                
#                 try:
#                     cursor.execute("""
#                         INSERT INTO recovered_users 
#                         (unique_id, username, email, password_hash, role, status, 
#                          created_at, recovered_at, cloud_user_id, original_cloud_data)
#                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
#                     """, (
#                         unique_id,
#                         username,
#                         email if email else None,
#                         password_hash,
#                         "admin",
#                         "active",
#                         datetime.now().isoformat(),
#                         datetime.now().isoformat(),
#                         admin.get("id"),
#                         json.dumps(original_data, default=str)
#                     ))
#                     saved_admin_ids.append(cursor.lastrowid)
#                 except sqlite3.IntegrityError:
#                     # Try with different username
#                     username = f"admin_{uuid.uuid4().hex[:8]}"
#                     cursor.execute("""
#                         INSERT INTO recovered_users 
#                         (unique_id, username, email, password_hash, role, status, 
#                          created_at, recovered_at, cloud_user_id, original_cloud_data)
#                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
#                     """, (
#                         unique_id,
#                         username,
#                         email if email else None,
#                         password_hash,
#                         "admin",
#                         "active",
#                         datetime.now().isoformat(),
#                         datetime.now().isoformat(),
#                         admin.get("id"),
#                         json.dumps(original_data, default=str)
#                     ))
#                     saved_admin_ids.append(cursor.lastrowid)
            
#             # Update activation state
#             cursor.execute("""
#                 UPDATE recovery_activation_state 
#                 SET activated = FALSE,
#                     activation_code = NULL,
#                     machine_fingerprint = NULL,
#                     school_name = ?,
#                     activated_at = NULL,
#                     updated_at = ?,
#                     recovered_at = ?
#                 WHERE id = 1
#             """, (
#                 school_name,
#                 datetime.now().isoformat(),
#                 datetime.now().isoformat()
#             ))
            
#             conn.commit()
            
#         except Exception as db_error:
#             if conn:
#                 conn.rollback()
#             logger.error(f"Database error: {db_error}")
#             raise HTTPException(status_code=500, detail=f"Database error during recovery: {str(db_error)}")
#         finally:
#             if conn:
#                 conn.close()
        
#         # Log success
#         log_recovery_attempt(req.email, "perform_recovery", "success", 
#                            f"Recovered {len(saved_admin_ids)} admins", client_ip)
        
#         # Prepare response
#         response_data = {
#             "success": True,
#             "message": "School recovery completed successfully!",
#             "data": {
#                 "school_name": school_name,
#                 "school_email": school_email,
#                 "admins_recovered": len(saved_admin_ids),
#                 "recovery_timestamp": datetime.now().isoformat(),
#                 "encrypted_blob_available": encrypted_blob is not None
#             }
#         }
        
#         if encrypted_blob:
#             response_data["data"]["encrypted_blob_length"] = len(encrypted_blob)
#             response_data["data"]["next_steps"] = [
#                 "Use /recovery/import-blob to send to main app",
#                 "Use /recovery/auto-import for automatic transfer"
#             ]
#             response_data["encrypted_blob"] = encrypted_blob
        
#         logger.info(f"✅ Recovery completed for {school_name}")
#         return response_data
                
#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.error(f"Unhandled exception: {e}")
#         import traceback
#         traceback.print_exc()
#         log_recovery_attempt(req.email, "perform_recovery", "error", f"Unhandled: {str(e)}", client_ip)
#         raise HTTPException(status_code=500, detail=f"Recovery failed: {str(e)}")

@app.post("/recovery/import-blob")
async def import_recovery_blob(req: RecoveryImportRequest, request: Request):
    """Import an encrypted recovery blob directly to main app"""
    client_ip = request.client.host
    
    try:
        # Test main app connection
        try:
            response = requests.get("http://localhost:8000/health/test", timeout=5)
            if response.status_code != 200:
                raise HTTPException(status_code=503, detail="Main app is not responding")
        except requests.exceptions.ConnectionError:
            raise HTTPException(status_code=503, detail="Cannot connect to main app")
        
        # Send to main app's import endpoint
        main_app_url = "http://localhost:8000/recovery/import"
        
        response = requests.post(
            main_app_url,
            json={
                "school_email": req.school_email,
                "encrypted_backup": req.encrypted_backup
            },
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            
            log_recovery_attempt(
                req.school_email, "import_blob", "success", 
                f"Imported to main app: {result.get('admins_imported', 0)} admins",
                client_ip
            )
            
            return {
                "success": True,
                "message": "Recovery blob successfully imported to main app",
                "main_app_response": result
            }
        else:
            error_msg = f"Main app returned {response.status_code}: {response.text}"
            log_recovery_attempt(req.school_email, "import_blob", "failed", error_msg, client_ip)
            
            return {
                "success": False,
                "message": "Failed to import to main app",
                "status_code": response.status_code,
                "error": response.text
            }
            
    except HTTPException:
        raise
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="Request to main app timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")


@app.post("/perform-recovery")
async def perform_school_recovery(req: SchoolRecoveryRequest, request: Request):
    """Perform the complete school recovery process"""
    client_ip = request.client.host
    logger.info(f"🔍 /perform-recovery called for email: {req.email} from IP: {client_ip}")
    
    try:
        # Input validation
        if not req.confirm_deactivation:
            raise HTTPException(
                status_code=400, 
                detail="You must confirm device deactivation to proceed with recovery."
            )
        
        if not cloud_client.check_connection():
            raise HTTPException(status_code=503, detail="Cannot connect to cloud database")
        
        # Step 1: Verify school details
        verify_result = await verify_school_recovery(req, request)
        
        if not verify_result.get("verified"):
            return {
                "success": False,
                "message": verify_result.get("message", "Verification failed")
            }
        
        school_data = verify_result["data"]["school"]
        school_id = school_data.get("id")
        
        if not school_id:
            raise HTTPException(status_code=500, detail="School ID not found in verification data")
        
        # Step 2: Get full admin data (including password hashes)
        admin_query = """
            SELECT id, first_name, middle_name, last_name, 
                   contact, email, password_hash, created_at
            FROM admin_table 
            WHERE school_id = ? 
            ORDER BY created_at DESC
        """
        
        admin_result = execute_cloud_query(admin_query, (school_id,))
        
        if not admin_result.get("success"):
            raise HTTPException(status_code=503, detail="Failed to query admin data")
        
        admins = admin_result.get("rows", [])
        
        if not admins:
            raise HTTPException(status_code=404, detail="No admin accounts found for this school.")
        
        logger.info(f"Found {len(admins)} admins to recover")
        
        # Step 3: Get full school data for blob creation
        full_school_query = """
            SELECT * FROM school_installations WHERE id = ?
        """
        full_school_result = execute_cloud_query(full_school_query, (school_id,))
        
        if full_school_result.get("success") and full_school_result.get("rows"):
            full_school_data = full_school_result["rows"][0]
        else:
            full_school_data = school_data
        
        # Step 4: Create encrypted recovery blob
        try:
            encrypted_blob = create_recovery_blob(full_school_data, admins)
            logger.info(f"✅ Created blob of length {len(encrypted_blob)}")
        except Exception as blob_error:
            logger.error(f"Failed to create blob: {blob_error}")
            encrypted_blob = None
        
        # Step 5: Save to local database with SIMPLE approach
        conn = None
        try:
            conn = get_local_db_connection()
            cursor = conn.cursor()
            
            # Start transaction
            cursor.execute("BEGIN TRANSACTION")
            
            # SIMPLE FIX: First, delete ONLY the conflicting records
            admin_emails = []
            for admin in admins:
                email = admin.get("email", "").strip()
                if email:
                    admin_emails.append(email)
                    # Delete any existing record with this email
                    cursor.execute("DELETE FROM recovered_users WHERE email = ?", (email,))
                    logger.info(f"Deleted existing record for email: {email}")
            
            logger.info(f"Cleared {len(admin_emails)} potential conflicts")
            
            # Save school info (use INSERT OR REPLACE)
            school_name = school_data.get("school_name", "").strip()
            school_email = school_data.get("school_email", "").strip()
            school_contact = school_data.get("school_contact", "").strip()
            
            # Prepare address
            town = full_school_data.get("town", "") if isinstance(full_school_data, dict) else ""
            city = full_school_data.get("city", "") if isinstance(full_school_data, dict) else school_data.get("city", "")
            region = full_school_data.get("region", "") if isinstance(full_school_data, dict) else school_data.get("region", "")
            county = full_school_data.get("county", "") if isinstance(full_school_data, dict) else school_data.get("county", "")
            
            address = f"{town}, {city}".strip(", ")
            if not address or address == ", ":
                address = city if city else "Unknown"
            
            # Use INSERT OR REPLACE for school
            cursor.execute("""
                INSERT OR REPLACE INTO recovered_school_info 
                (id, school_name, email, phone, address, city, state, country, 
                 created_at, updated_at, recovered_at, cloud_school_id, original_cloud_data)
                VALUES (
                    COALESCE((SELECT id FROM recovered_school_info WHERE email = ?), NULL),
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                school_email,  # for subquery
                school_name,
                school_email,
                school_contact,
                address,
                city,
                region,
                county,
                datetime.now().isoformat(),
                datetime.now().isoformat(),
                datetime.now().isoformat(),
                school_id,
                json.dumps(full_school_data if isinstance(full_school_data, dict) else {}, default=str)
            ))
            
            logger.info(f"Saved school info")
            
            # Save admins - NOW with clean slate
            saved_admin_ids = []
            failed_admins = []
            
            for i, admin in enumerate(admins):
                try:
                    unique_id = str(uuid.uuid4())
                    email = admin.get("email", "").strip()
                    contact = admin.get("contact", "").strip()
                    
                    logger.info(f"Processing admin {i+1}: email='{email}'")
                    
                    password_hash = admin.get("password_hash")
                    if not password_hash:
                        password_hash = hash_password("temporary_password")
                    
                    # Prepare original data
                    original_data = {k: v for k, v in admin.items() if k != 'password_hash'}
                    
                    if email:
                        # Simple insert - we already deleted conflicts
                        cursor.execute("""
                            INSERT INTO recovered_users 
                            (unique_id, username, email, password_hash, role, status, 
                             created_at, recovered_at, cloud_user_id, original_cloud_data)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            unique_id,
                            email,
                            email,
                            password_hash,
                            "admin",
                            "active",
                            datetime.now().isoformat(),
                            datetime.now().isoformat(),
                            admin.get("id"),
                            json.dumps(original_data, default=str)
                        ))
                    else:
                        # Admin without email
                        username = f"admin_{contact}_{unique_id[:8]}" if contact else f"admin_{unique_id[:8]}"
                        cursor.execute("""
                            INSERT INTO recovered_users 
                            (unique_id, username, email, password_hash, role, status, 
                             created_at, recovered_at, cloud_user_id, original_cloud_data)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            unique_id,
                            username,
                            None,
                            password_hash,
                            "admin",
                            "active",
                            datetime.now().isoformat(),
                            datetime.now().isoformat(),
                            admin.get("id"),
                            json.dumps(original_data, default=str)
                        ))
                    
                    saved_admin_ids.append(cursor.lastrowid)
                    logger.info(f"✅ Admin {i+1} saved successfully")
                    
                except Exception as admin_error:
                    logger.error(f"❌ Error for admin {i+1}: {admin_error}")
                    failed_admins.append({
                        "admin": {
                            "id": admin.get("id"),
                            "email": admin.get("email", ""),
                            "contact": admin.get("contact", "")
                        }, 
                        "error": str(admin_error)
                    })
            
            # Update activation state
            cursor.execute("""
                INSERT OR REPLACE INTO recovery_activation_state 
                (id, activated, school_name, updated_at, recovered_at)
                VALUES (1, FALSE, ?, ?, ?)
            """, (
                school_name,
                datetime.now().isoformat(),
                datetime.now().isoformat()
            ))
            
            # Commit transaction
            conn.commit()
            logger.info(f"✅ Transaction committed. Saved {len(saved_admin_ids)} admins, Failed: {len(failed_admins)}")
            
        except Exception as db_error:
            if conn:
                conn.rollback()
                logger.error(f"Database error, rolled back: {db_error}")
            raise HTTPException(status_code=500, detail=f"Database error during recovery: {str(db_error)}")
        finally:
            if conn:
                conn.close()
        
        # Log success/failure
        log_recovery_attempt(
            req.email, "perform_recovery", 
            "success" if saved_admin_ids else "failed", 
            f"Recovered {len(saved_admin_ids)} admins, Failed: {len(failed_admins)}", 
            client_ip
        )
        
        # Prepare response
        response_data = {
            "success": True if saved_admin_ids else False,
            "message": f"Recovery completed. Recovered {len(saved_admin_ids)} out of {len(admins)} admins.",
            "data": {
                "school_name": school_name,
                "school_email": school_email,
                "admins_recovered": len(saved_admin_ids),
                "admins_failed": len(failed_admins),
                "total_admins": len(admins),
                "recovery_timestamp": datetime.now().isoformat(),
                "encrypted_blob_available": encrypted_blob is not None
            }
        }
        
        if failed_admins:
            response_data["warning"] = f"Failed to recover {len(failed_admins)} admin(s)."
        
        if encrypted_blob:
            response_data["encrypted_blob"] = encrypted_blob
        
        return response_data
                
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unhandled exception: {e}")
        log_recovery_attempt(req.email, "perform_recovery", "error", str(e), client_ip)
        raise HTTPException(status_code=500, detail=f"Recovery failed: {str(e)}")



@app.post("/recovery/auto-import/{school_email}")
async def auto_import_recovery(school_email: str, request: Request):
    """Automatically recover and import to main app in one step"""
    client_ip = request.client.host
    
    try:
        # First, get school data
        check_result = await check_school_exists(SchoolCheckRequest(email=school_email), request)
        
        if not check_result.get("exists"):
            raise HTTPException(status_code=404, detail="School not found")
        
        school_data = check_result["school"]
        
        # Get full school data
        query = """
            SELECT * FROM school_installations WHERE school_email = ? LIMIT 1
        """
        result = execute_cloud_query(query, (school_email,))
        
        if not result.get("success") or not result.get("rows"):
            raise HTTPException(status_code=404, detail="School data not found")
        
        full_school = result["rows"][0]
        
        # Get admins
        admin_query = """
            SELECT * FROM admin_table WHERE school_id = ?
        """
        admin_result = execute_cloud_query(admin_query, (school_data["id"],))
        
        if not admin_result.get("success") or not admin_result.get("rows"):
            raise HTTPException(status_code=404, detail="No admin accounts found")
        
        admins = admin_result["rows"]
        
        # Create encrypted blob
        encrypted_blob = create_recovery_blob(full_school, admins)
        
        # Import to main app
        import_result = await import_recovery_blob(
            RecoveryImportRequest(
                school_email=school_email,
                encrypted_backup=encrypted_blob
            ), 
            request
        )
        
        # Also save to local database
        conn = get_local_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO recovered_school_info 
            (school_name, email, phone, address, city, state, country, 
             created_at, updated_at, recovered_at, cloud_school_id, original_cloud_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            full_school.get("school_name"),
            school_email,
            full_school.get("school_contact"),
            f"{full_school.get('town', '')}, {full_school.get('city', '')}",
            full_school.get("city"),
            full_school.get("region"),
            full_school.get("county"),
            datetime.now().isoformat(),
            datetime.now().isoformat(),
            datetime.now().isoformat(),
            school_data["id"],
            json.dumps(full_school, default=str)
        ))
        conn.commit()
        conn.close()
        
        log_recovery_attempt(school_email, "auto_import", "success", 
                           f"Auto-imported {len(admins)} admins", client_ip)
        
        return {
            "success": True,
            "message": "Auto-import completed successfully",
            "data": {
                "school_name": full_school.get("school_name"),
                "admins_recovered": len(admins),
                "main_app_imported": import_result.get("success", False),
                "recovery_timestamp": datetime.now().isoformat()
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        log_recovery_attempt(school_email, "auto_import", "error", str(e), client_ip)
        raise HTTPException(status_code=500, detail=f"Auto-import failed: {str(e)}")

@app.get("/recovery-status")
async def get_recovery_status():
    """Get the current recovery status from local recovery database"""
    try:
        conn = get_local_db_connection()
        cursor = conn.cursor()
        
        # Check recovered school info
        cursor.execute("SELECT COUNT(*) FROM recovered_school_info")
        school_count = cursor.fetchone()[0]
        
        # Get school details
        school_details = None
        if school_count > 0:
            cursor.execute("""
                SELECT school_name, email, recovered_at 
                FROM recovered_school_info 
                ORDER BY recovered_at DESC LIMIT 1
            """)
            row = cursor.fetchone()
            if row:
                school_details = {
                    "name": row[0],
                    "email": row[1],
                    "recovered_at": row[2]
                }
        
        # Check recovered admins
        cursor.execute("SELECT COUNT(*) FROM recovered_users WHERE role = 'admin'")
        admin_count = cursor.fetchone()[0]
        
        # Check activation state
        cursor.execute("SELECT activated, school_name FROM recovery_activation_state WHERE id = 1")
        activation_row = cursor.fetchone()
        activation_state = bool(activation_row[0]) if activation_row else False
        activated_school = activation_row[1] if activation_row else None
        
        # Get latest recovery attempt
        cursor.execute("""
            SELECT email, recovery_type, success, timestamp 
            FROM recovery_attempts 
            ORDER BY timestamp DESC 
            LIMIT 1
        """)
        latest_log = cursor.fetchone()
        
        conn.close()
        
        status = {
            "recovery_database": "online",
            "school_recovered": school_count > 0,
            "admins_recovered": admin_count > 0,
            "system_activated": activation_state,
            "school_count": school_count,
            "admin_count": admin_count,
            "activated_school": activated_school,
            "last_recovery_attempt": None
        }
        
        if school_details:
            status["recovered_school"] = school_details
        
        if latest_log:
            status["last_recovery_attempt"] = {
                "email": latest_log[0],
                "type": latest_log[1],
                "success": bool(latest_log[2]),
                "timestamp": latest_log[3]
            }
        
        return status
        
    except Exception as e:
        return {
            "recovery_database": "offline",
            "error": str(e),
            "school_recovered": False,
            "admins_recovered": False,
            "system_activated": False
        }

@app.get("/recovery/blob/{school_email}")
async def get_recovery_blob(school_email: str, request: Request):
    """Get encrypted recovery blob for a specific school"""
    client_ip = request.client.host
    
    try:
        if not cloud_client.check_connection():
            raise HTTPException(status_code=503, detail="Cannot connect to cloud database")
        
        # Get school data
        query = """
            SELECT * FROM school_installations WHERE school_email = ? LIMIT 1
        """
        result = execute_cloud_query(query, (school_email,))
        
        if not result.get("success") or not result.get("rows"):
            raise HTTPException(status_code=404, detail="School not found")
        
        school_data = result["rows"][0]
        
        # Get admins
        admin_query = """
            SELECT * FROM admin_table WHERE school_id = ?
        """
        admin_result = execute_cloud_query(admin_query, (school_data.get("id"),))
        
        if not admin_result.get("success") or not admin_result.get("rows"):
            raise HTTPException(status_code=404, detail="No admin accounts found")
        
        admins = admin_result["rows"]
        
        # Create encrypted blob
        encrypted_blob = create_recovery_blob(school_data, admins)
        
        log_recovery_attempt(school_email, "get_blob", "success", 
                           f"Generated blob for {school_data.get('school_name')}", client_ip)
        
        return {
            "success": True,
            "school_email": school_email,
            "school_name": school_data.get("school_name"),
            "encrypted_backup": encrypted_blob,
            "admins_count": len(admins),
            "blob_length": len(encrypted_blob),
            "issued_at": datetime.now().isoformat()
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create recovery blob: {str(e)}")

@app.post("/transfer-to-main")
async def transfer_to_main_database(request: Request):
    """Transfer recovered data to the main school database"""
    client_ip = request.client.host
    
    try:
        # Check if main app is running
        try:
            response = requests.get("http://localhost:8000/health/test", timeout=5)
            if response.status_code != 200:
                return {
                    "success": False,
                    "message": "Main app is not running",
                    "alternative": "Use /recovery/auto-import instead"
                }
        except Exception:
            return {
                "success": False,
                "message": "Cannot connect to main app",
                "alternative": "Start main app on port 8000"
            }
        
        # Get recovered school data
        conn = get_local_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM recovered_school_info LIMIT 1")
        school_row = cursor.fetchone()
        
        if not school_row:
            conn.close()
            return {
                "success": False,
                "message": "No recovered school data found. Perform recovery first."
            }
        
        # Convert row to dict
        school_data = dict(school_row)
        
        # Get admins
        cursor.execute("SELECT * FROM recovered_users WHERE role = 'admin'")
        admin_rows = cursor.fetchall()
        
        if not admin_rows:
            conn.close()
            return {
                "success": False,
                "message": "No recovered admin data found"
            }
        
        admins = [dict(row) for row in admin_rows]
        conn.close()
        
        # Create encrypted blob for transfer
        formatted_school_data = {
            "school_name": school_data.get("school_name"),
            "school_email": school_data.get("email"),
            "school_contact": school_data.get("phone"),
            "county": school_data.get("country", ""),
            "region": school_data.get("state", ""),
            "city": school_data.get("city", ""),
            "town": school_data.get("address", "").split(",")[0] if school_data.get("address") else "",
            "gps_address": "",
            "manufacture_code": "",
            "created_at": school_data.get("created_at")
        }
        
        formatted_admins = []
        for admin in admins:
            original_data = {}
            if admin.get("original_cloud_data"):
                try:
                    original_data = json.loads(admin.get("original_cloud_data"))
                except (json.JSONDecodeError, TypeError):
                    pass
            
            formatted_admins.append({
                "first_name": original_data.get("first_name", "Recovered"),
                "middle_name": original_data.get("middle_name", ""),
                "last_name": original_data.get("last_name", "Admin"),
                "contact": admin.get("email", ""),
                "email": admin.get("email"),
                "password_hash": admin.get("password_hash"),
                "created_at": admin.get("created_at")
            })
        
        encrypted_blob = create_recovery_blob(formatted_school_data, formatted_admins)
        
        # Import to main app
        import_result = await import_recovery_blob(
            RecoveryImportRequest(
                school_email=school_data.get("email"),
                encrypted_backup=encrypted_blob
            ), 
            request
        )
        
        return {
            "success": import_result.get("success", False),
            "message": "Transfer attempted using encrypted blob",
            "details": {
                "school": school_data.get("school_name"),
                "admins_transferred": len(formatted_admins),
                "method": "encrypted_blob",
                "main_app_response": import_result
            }
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transfer failed: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    print("🚀 Starting School Recovery Server on http://localhost:8001")
    print("📊 Version 2.1.0 - Using SQLiteCloud Client")
    print("📋 Endpoints:")
    print("  - GET  /                          Server status")
    print("  - GET  /health                    Health check")
    print("  - POST /check-school              Check if school exists")
    print("  - POST /verify-recovery           Verify recovery details")
    print("  - POST /perform-recovery          Complete recovery")
    print("  - GET  /recovery-status           Get recovery status")
    print("  - GET  /recovery/blob/{email}     Get recovery blob")
    print("  - POST /recovery/import-blob      Import blob to main app")
    print("  - POST /recovery/auto-import/{email}  Auto recover & import")
    print("  - POST /transfer-to-main          Legacy transfer method")
    
    port = int(os.getenv("PORT", 8001))
    host = os.getenv("HOST", "0.0.0.0")
    
    uvicorn.run(
        "cloud_backend:app",
        host=host, 
        port=port,
        log_level="info",
        reload=False
    )