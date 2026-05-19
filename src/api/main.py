from fastapi import FastAPI, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
import os
import shutil
import uuid
from src.pipeline.fan_out_orchestrator import FanOutOrchestrator

app = FastAPI(title="Blueprint-AI Extraction Engine API")

# Enable CORS for Next.js frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "data/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.get("/")
async def root():
    return {"status": "online", "message": "Blueprint-AI API is running"}

@app.post("/extract")
async def extract_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...)
):
    """
    Endpoint to upload a PDF and trigger the Fan-Out extraction pipeline.
    Runs in the background to prevent timeout.
    """
    project_id = f"PROJ_{uuid.uuid4().hex[:8].upper()}"
    file_path = os.path.join(UPLOAD_DIR, f"{project_id}_{file.filename}")
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    # Trigger the orchestrator in the background
    background_tasks.add_task(run_orchestrator, file_path, project_id)
    
    return {
        "message": "Extraction started in background",
        "project_id": project_id,
        "filename": file.filename
    }

def run_orchestrator(pdf_path: str, project_id: str):
    """Wrapper function to run the existing Fan-Out pipeline."""
    orchestrator = FanOutOrchestrator(
        pdf_path=pdf_path,
        project_id=project_id,
        openai_api_key=os.environ.get("OPENAI_API_KEY")
    )
    orchestrator.run()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
