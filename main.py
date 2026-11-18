import os
import io
import tempfile
import pickle
import asyncio
import pandas as pd
from fastapi import FastAPI, APIRouter, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any

from data_processor import DataProcessor
from sample_data import get_retail_sample_df, get_saas_sample_df, get_fnb_sample_df
from report_generator import create_docx_report
from ai_advisor import answer_customer_inquiry, FAQ_KNOWLEDGE_BASE

app = FastAPI(title="PHAMHAANHNGOC - Enterprise Data Intelligence Agent", version="2.8")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def vercel_path_rewrite_middleware(request, call_next):
    path = request.scope.get("path", "")
    if path in ("/api/index.py", "/index.py", "/api"):
        matched = request.headers.get("x-matched-path") or request.headers.get("x-invoke-path")
        if matched:
            request.scope["path"] = matched
    return await call_next(request)

processor = DataProcessor()
current_session = {
    "sample_type": None,
    "business_type": "",
    "problem_statement": "",
    "additional_info": "",
    "cleaning_result": None,
    "analytics_result": None,
    "last_custom_instruction": ""
}

# Cross-invocation serverless session persistence
SESSION_FILE = os.path.join(tempfile.gettempdir(), "phamhaanhngoc_session.pkl")

def persist_session():
    try:
        data = {
            "processor": processor,
            "session": current_session
        }
        tmp_file = SESSION_FILE + f".{os.getpid()}.tmp"
        with open(tmp_file, "wb") as f:
            pickle.dump(data, f)
        if os.path.exists(tmp_file):
            os.replace(tmp_file, SESSION_FILE)
    except Exception as e:
        print(f"[WARN] Failed to persist session: {e}")

def restore_session():
    global processor, current_session
    try:
        if os.path.exists(SESSION_FILE):
            with open(SESSION_FILE, "rb") as f:
                data = pickle.load(f)
                p = data.get("processor")
                s = data.get("session")
                if p is not None:
                    processor = p
                if s is not None:
                    current_session = s
    except Exception as e:
        print(f"[WARN] Failed to restore session: {e}")

class RecleanRequest(BaseModel):
    custom_instructions: Optional[str] = ""
    config_overrides: Optional[Dict[str, Any]] = None

class SampleRequest(BaseModel):
    sample_type: str # 'fnb', 'retail', 'saas'
    business_type: Optional[str] = ""
    problem_statement: Optional[str] = ""
    additional_info: Optional[str] = ""

class DashboardFilterRequest(BaseModel):
    metric: Optional[str] = None
    group_by: Optional[str] = None
    category: Optional[str] = "all"
    region: Optional[str] = "all"
    status: Optional[str] = "all"

api_router = APIRouter()

@api_router.get("/health")
async def health_check():
    restore_session()
    return {
        "status": "healthy",
        "service": "PHAMHAANHNGOC Enterprise Data Intelligence",
        "version": "2.8",
        "has_active_session": processor.cleaned_df is not None and not processor.cleaned_df.empty
    }

@api_router.post("/reset")
async def reset_session():
    """Resets the entire session so the user can start a new dataset."""
    global processor, current_session
    processor.reset()
    current_session = {
        "sample_type": None,
        "business_type": "",
        "problem_statement": "",
        "additional_info": "",
        "cleaning_result": None,
        "analytics_result": None,
        "last_custom_instruction": ""
    }
    if os.path.exists(SESSION_FILE):
        try:
            os.remove(SESSION_FILE)
        except Exception:
            pass
    return {"success": True, "message": "Phiên làm việc đã được làm mới thành công."}

@api_router.post("/load-sample")
async def load_sample(req: SampleRequest):
    global processor, current_session
    current_session["sample_type"] = req.sample_type
    if req.sample_type == "fnb":
        df = get_fnb_sample_df()
        current_session["business_type"] = req.business_type or "Chuỗi Nhà hàng & F&B (PHAMHAANHNGOC Hospitality)"
        current_session["problem_statement"] = req.problem_statement or "Chi phí nguyên vật liệu và tỷ lệ hao hụt vượt định mức an toàn 12% tại các cơ sở"
        current_session["additional_info"] = req.additional_info or "Chuỗi 7 chi nhánh tại các thành phố lớn (Hà Nội, TP.HCM, Đà Nẵng)"
    elif req.sample_type == "retail":
        df = get_retail_sample_df()
        current_session["business_type"] = req.business_type or "Bán lẻ đa kênh & Thương mại điện tử (Omnichannel Retail)"
        current_session["problem_statement"] = req.problem_statement or "Tỷ lệ hủy đơn hàng cao và chi phí hoàn đơn kênh online tăng trong 2 quý gần nhất"
        current_session["additional_info"] = req.additional_info or "5 kênh bán hàng: Website, Shopee, TikTok Shop, Showroom, Lazada"
    elif req.sample_type == "saas":
        df = get_saas_sample_df()
        current_session["business_type"] = req.business_type or "Công nghệ & Phần mềm Dịch vụ (B2B SaaS)"
        current_session["problem_statement"] = req.problem_statement or "Tỷ lệ khách hàng rời bỏ dịch vụ (Churn rate) gia tăng và chi phí chăm sóc sau bán chưa tối ưu"
        current_session["additional_info"] = req.additional_info or "Mô hình SaaS subscription 4 gói dịch vụ (Starter, Pro, Enterprise, Custom)"
    else:
        raise HTTPException(status_code=400, detail="Mẫu dữ liệu không hợp lệ")

    processor = DataProcessor()
    processor.load_from_dataframe(df)
    processor.business_context = {
        "business_type": current_session["business_type"],
        "problem_statement": current_session["problem_statement"],
        "additional_info": current_session["additional_info"]
    }
    clean_res = processor.clean_data()
    current_session["cleaning_result"] = clean_res
    current_session["analytics_result"] = None
    current_session["last_custom_instruction"] = ""

    persist_session()

    return {
        "success": True,
        "business_type": current_session["business_type"],
        "problem_statement": current_session["problem_statement"],
        "additional_info": current_session["additional_info"],
        "cleaning": clean_res
    }

@api_router.post("/upload")
async def upload_dataset(
    file: UploadFile = File(...),
    business_type: str = Form(""),
    problem_statement: str = Form(""),
    additional_info: str = Form("")
):
    global processor, current_session
    try:
        content = await file.read()
        if len(content) == 0:
            raise HTTPException(status_code=400, detail="File tải lên rỗng.")

        processor = DataProcessor()
        processor.load_from_bytes(content, file.filename)
        
        current_session["sample_type"] = None
        current_session["business_type"] = business_type or "Doanh nghiệp chưa xác định"
        current_session["problem_statement"] = problem_statement or "Tối ưu hóa dữ liệu & vận hành"
        current_session["additional_info"] = additional_info
        
        processor.business_context = {
            "business_type": current_session["business_type"],
            "problem_statement": current_session["problem_statement"],
            "additional_info": current_session["additional_info"]
        }
        
        clean_res = processor.clean_data()
        current_session["cleaning_result"] = clean_res
        current_session["analytics_result"] = None
        current_session["last_custom_instruction"] = ""

        persist_session()

        return {
            "success": True,
            "filename": file.filename,
            "business_type": current_session["business_type"],
            "problem_statement": current_session["problem_statement"],
            "additional_info": current_session["additional_info"],
            "cleaning": clean_res
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Lỗi khi đọc file: {str(e)}")

@api_router.post("/reclean")
async def reclean_dataset(req: RecleanRequest):
    global processor, current_session
    if processor.raw_df is None or processor.raw_df.empty:
        restore_session()

    if processor.raw_df is None or processor.raw_df.empty:
        raise HTTPException(status_code=400, detail="Chưa có dữ liệu thô để xử lý lại.")

    try:
        current_session["last_custom_instruction"] = req.custom_instructions or ""
        clean_res = processor.clean_data(
            custom_instructions=req.custom_instructions,
            config_overrides=req.config_overrides
        )
        current_session["cleaning_result"] = clean_res
        current_session["analytics_result"] = None

        persist_session()

        return {
            "success": True,
            "cleaning": clean_res,
            "custom_instruction": req.custom_instructions
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Lỗi khi tái xử lý dữ liệu: {str(e)}")

def _ensure_serverless_dataset_loaded(fallback_sample: str = "retail"):
    """Ensures a valid cleaned_df exists even if Vercel Serverless routed to a cold container."""
    global processor, current_session
    if processor.cleaned_df is not None and not processor.cleaned_df.empty:
        return
    restore_session()
    if processor.cleaned_df is not None and not processor.cleaned_df.empty:
        return
    st = current_session.get("sample_type") or fallback_sample
    current_session["sample_type"] = st
    if st == "fnb":
        df = get_fnb_sample_df()
        current_session["business_type"] = current_session.get("business_type") or "Chuỗi Nhà hàng & F&B (PHAMHAANHNGOC Hospitality)"
        current_session["problem_statement"] = current_session.get("problem_statement") or "Chi phí nguyên vật liệu và tỷ lệ hao hụt vượt định mức an toàn 12% tại các cơ sở"
        current_session["additional_info"] = current_session.get("additional_info") or "Chuỗi 7 chi nhánh tại các thành phố lớn (Hà Nội, TP.HCM, Đà Nẵng)"
    elif st == "saas":
        df = get_saas_sample_df()
        current_session["business_type"] = current_session.get("business_type") or "Công nghệ & Phần mềm Dịch vụ (B2B SaaS)"
        current_session["problem_statement"] = current_session.get("problem_statement") or "Tỷ lệ khách hàng rời bỏ dịch vụ (Churn rate) gia tăng và chi phí chăm sóc sau bán chưa tối ưu"
        current_session["additional_info"] = current_session.get("additional_info") or "Mô hình SaaS subscription 4 gói dịch vụ (Starter, Pro, Enterprise, Custom)"
    else:
        df = get_retail_sample_df()
        current_session["business_type"] = current_session.get("business_type") or "Bán lẻ đa kênh & Thương mại điện tử (Omnichannel Retail)"
        current_session["problem_statement"] = current_session.get("problem_statement") or "Tỷ lệ hủy đơn hàng cao và chi phí hoàn đơn kênh online tăng trong 2 quý gần nhất"
        current_session["additional_info"] = current_session.get("additional_info") or "5 kênh bán hàng: Website, Shopee, TikTok Shop, Showroom, Lazada"
    processor.load_from_dataframe(df)
    processor.business_context = {
        "business_type": current_session["business_type"],
        "problem_statement": current_session["problem_statement"],
        "additional_info": current_session["additional_info"]
    }
    processor.clean_data()

@api_router.post("/approve")
async def approve_and_generate_dashboard():
    global processor, current_session
    await asyncio.to_thread(_ensure_serverless_dataset_loaded, "retail")

    try:
        analytics = await asyncio.to_thread(
            processor.generate_analytics,
            business_type=current_session["business_type"],
            problem_statement=current_session["problem_statement"],
            generate_strategy=True
        )
        current_session["analytics_result"] = analytics

        persist_session()

        return {
            "success": True,
            "business_type": current_session["business_type"],
            "problem_statement": current_session["problem_statement"],
            "additional_info": current_session["additional_info"],
            "metadata": processor.metadata,
            "analytics": analytics
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi phân tích: {str(e)}")

@api_router.post("/filter-dashboard")
async def filter_dashboard(req: DashboardFilterRequest):
    global processor, current_session
    await asyncio.to_thread(_ensure_serverless_dataset_loaded, "retail")

    try:
        analytics = await asyncio.to_thread(
            processor.generate_analytics,
            business_type=current_session["business_type"],
            problem_statement=current_session["problem_statement"],
            metric_choice=req.metric,
            group_by_choice=req.group_by,
            filter_category=req.category,
            filter_region=req.region,
            filter_status=req.status,
            generate_strategy=False
        )
        current_session["analytics_result"] = analytics

        persist_session()

        return {
            "success": True,
            "analytics": analytics
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lỗi khi lọc dữ liệu: {str(e)}")

@api_router.get("/export-docx")
async def export_docx():
    global processor, current_session
    await asyncio.to_thread(_ensure_serverless_dataset_loaded, "retail")

    analytics = current_session.get("analytics_result")
    if not analytics:
        analytics = processor.generate_analytics(
            business_type=current_session["business_type"],
            problem_statement=current_session["problem_statement"]
        )
        current_session["analytics_result"] = analytics

    try:
        buf = await asyncio.to_thread(
            create_docx_report,
            business_type=current_session["business_type"],
            problem_statement=current_session["problem_statement"],
            additional_info=current_session["additional_info"],
            metadata=processor.metadata,
            audit_log=processor.audit_log,
            analytics_data=analytics,
            df=processor.cleaned_df
        )
        safe_filename = "PHAMHAANHNGOC_Bao_Cao_Chien_Luoc_Doanh_Nghiep.docx"
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{safe_filename}"'}
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Lỗi tạo file Word: {str(e)}")

@api_router.get("/export-data")
async def export_cleaned_data(format: str = "csv"):
    global processor
    if processor.cleaned_df is None or processor.cleaned_df.empty:
        restore_session()

    if processor.cleaned_df is None or processor.cleaned_df.empty:
        raise HTTPException(status_code=400, detail="Chưa có dữ liệu sạch.")

    if format == "xlsx":
        buf = io.BytesIO()
        processor.cleaned_df.to_excel(buf, index=False, engine='openpyxl')
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="PHAMHAANHNGOC_Du_Lieu_Da_Lam_Sach.xlsx"'}
        )
    else:
        buf = io.StringIO()
        processor.cleaned_df.to_csv(buf, index=False, encoding="utf-8-sig")
        mem = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
        return StreamingResponse(
            mem,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="PHAMHAANHNGOC_Du_Lieu_Da_Lam_Sach.csv"'}
        )

# ==========================================
# CUSTOMER AI ASSISTANT & FEEDBACK MODELS
# ==========================================

class ChatRequest(BaseModel):
    question: str

class FeedbackRequest(BaseModel):
    name: Optional[str] = ""
    author: Optional[str] = ""
    company: Optional[str] = ""
    rating: int = 5
    comment: str
    role: Optional[str] = "Quản lý / Chuyên viên"

CUSTOMER_REVIEWS = [
    {
        "id": 1,
        "name": "Anh Trần Minh Đức",
        "author": "Anh Trần Minh Đức",
        "role": "Giám đốc Vận hành",
        "company": "Chuỗi F&B Gourmet Delight",
        "avatar": "Đ",
        "avatar_bg": "#EF4444",
        "rating": 5,
        "date": "15/11/2026",
        "comment": "Trước đây việc đối chiếu dữ liệu thất thoát chi phí giữa các cơ sở mất cả tuần. Với PHAMHAANHNGOC, hệ thống tự động phát hiện 15% hóa đơn trùng lặp và xuất Dashboard ngay lập tức. Đội ngũ lãnh đạo cực kỳ ấn tượng!"
    },
    {
        "id": 2,
        "name": "Chị Lê Hoàng Mai Lan",
        "author": "Chị Lê Hoàng Mai Lan",
        "role": "Trưởng phòng BI & Data Analytics",
        "company": "Tập đoàn NovaRetail",
        "avatar": "L",
        "avatar_bg": "#3B82F6",
        "rating": 5,
        "date": "10/11/2026",
        "comment": "Khả năng làm sạch tự động, chuẩn hóa dữ liệu và đối chiếu mâu thuẫn nhiều nguồn của PHAMHAANHNGOC vượt trội hơn hẳn các công cụ thông thường. Đây là giải pháp không thể thiếu cho đội ngũ phân tích kinh doanh."
    },
    {
        "id": 3,
        "name": "Anh Nguyễn Quốc Huy",
        "author": "Anh Nguyễn Quốc Huy",
        "role": "Giám đốc Tài chính (CFO)",
        "company": "TechVanguard Solutions",
        "avatar": "H",
        "avatar_bg": "#10B981",
        "rating": 5,
        "date": "08/11/2026",
        "comment": "Báo cáo Word quản trị xuất ra từ PHAMHAANHNGOC đầy đủ phân tích Ma trận Ưu - Nhược điểm (Pros & Cons), chỉ số ROI và lộ trình hành động 90 ngày. Chúng tôi dùng trực tiếp để báo cáo Hội đồng Quản trị mà không cần chỉnh sửa gì thêm."
    },
    {
        "id": 4,
        "name": "Chị Phạm Thu Hà",
        "author": "Chị Phạm Thu Hà",
        "role": "Phó Giám đốc Vận hành",
        "company": "LogiTrans Express",
        "avatar": "H",
        "avatar_bg": "#F97316",
        "rating": 5,
        "date": "02/11/2026",
        "comment": "Cơ chế kiểm duyệt HITL giúp chúng tôi chủ động kiểm soát các ranh giới ngoại lệ. Nền tảng chuyên nghiệp, bảo mật dữ liệu tuyệt đối và trải nghiệm người dùng cực kỳ mượt mà."
    }
]

@api_router.get("/faqs")
async def get_faqs():
    faqs_list = []
    for k, v in FAQ_KNOWLEDGE_BASE.items():
        faqs_list.append({
            "key": k,
            "title": v["title"],
            "answer": v["answer"]
        })
    return {"success": True, "faqs": faqs_list}

@api_router.post("/chat-assistant")
async def chat_with_assistant(req: ChatRequest):
    restore_session()
    ans = await asyncio.to_thread(answer_customer_inquiry, req.question, current_session)
    return {"success": True, "question": req.question, "answer": ans}

@api_router.get("/feedback")
async def get_feedback():
    return {"success": True, "reviews": CUSTOMER_REVIEWS}

@api_router.post("/feedback")
async def add_feedback(req: FeedbackRequest):
    author_name = (req.author or req.name or "Khách hàng Doanh nghiệp").strip() or "Khách hàng Doanh nghiệp"
    new_rev = {
        "id": len(CUSTOMER_REVIEWS) + 1,
        "name": author_name,
        "author": author_name,
        "role": (req.role or "Quản lý dữ liệu").strip(),
        "company": (req.company or "Doanh nghiệp đối tác").strip(),
        "avatar": (author_name[:1] or "K").upper(),
        "avatar_bg": "#FF6B6B",
        "rating": min(5, max(1, req.rating)),
        "date": "Hôm nay",
        "comment": req.comment.strip()
    }
    CUSTOMER_REVIEWS.insert(0, new_rev)
    return {"success": True, "message": "Cảm ơn bạn đã gửi đánh giá quý báu cho PHAMHAANHNGOC!", "review": new_rev}

# Register both with prefix="/api" AND without prefix for maximum compatibility with Vercel rewrites
app.include_router(api_router, prefix="/api")
app.include_router(api_router)

# Safe static file serving & fallback index handler
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

@app.get("/", response_class=HTMLResponse)
async def serve_root():
    for candidate in [
        os.path.join(BASE_DIR, "index.html"),
        os.path.join(STATIC_DIR, "index.html"),
        os.path.join(BASE_DIR, "public", "index.html")
    ]:
        if os.path.exists(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                return HTMLResponse(f.read())
    return HTMLResponse("<h1>PHAMHAANHNGOC Enterprise Data Intelligence</h1>")

@app.get("/presentation", response_class=HTMLResponse)
@app.get("/presentation.html", response_class=HTMLResponse)
async def serve_presentation():
    for candidate in [
        os.path.join(BASE_DIR, "presentation.html"),
        os.path.join(STATIC_DIR, "presentation.html"),
        os.path.join(BASE_DIR, "public", "presentation.html")
    ]:
        if os.path.exists(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                return HTMLResponse(f.read())
    return HTMLResponse("<h1>Presentation not found</h1>", status_code=404)

# Mount static folders for local execution and asset requests
for folder in ["static", "public", "css", "js"]:
    p = os.path.join(BASE_DIR, folder)
    if os.path.exists(p):
        app.mount(f"/{folder}", StaticFiles(directory=p), name=f"dir_{folder}")

if os.path.exists(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static_root")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
