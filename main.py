import os
import uuid
import logging
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from pytube import YouTube
import ffmpeg

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Efficient YouTube Video Downloader and Converter")

# Constants
DOWNLOAD_DIR = "downloaded_videos"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

RESOLUTIONS = {
    "240p": "426x240",
    "360p": "640x360",
    "480p": "854x480",
    "720p": "1280x720",
    "1080p": "1920x1080",
    "1440p": "2560x1440",
    "2160p": "3840x2160",
}

# Models
class DownloadRequest(BaseModel):
    url: str
    resolution: str

class DownloadStatus(BaseModel):
    task_id: str
    status: str
    filename: str = None
    error: str = None

# In-memory storage for download tasks
download_tasks = {}

# Helper functions
def safe_filename(filename):
    return "".join([c for c in filename if c.isalpha() or c.isdigit() or c==' ']).rstrip()

async def download_and_convert_video(task_id: str, url: str, resolution: str):
    task = download_tasks[task_id]
    task.status = "Downloading"
    
    try:
        logger.info(f"Starting download for task {task_id}: {url}")
        # Download video
        yt = YouTube(url)
        stream = yt.streams.filter(progressive=True, file_extension="mp4").order_by("resolution").desc().first()
        
        if not stream:
            raise Exception("No suitable video stream found")
        
        safe_title = safe_filename(yt.title)
        original_file = os.path.join(DOWNLOAD_DIR, f"{task_id}_{safe_title}_original.mp4")
        stream.download(output_path=DOWNLOAD_DIR, filename=original_file)
        
        logger.info(f"Download completed for task {task_id}. Starting conversion.")
        task.status = "Converting"
        
        # Convert video
        if resolution not in RESOLUTIONS:
            raise Exception(f"Invalid resolution: {resolution}")
        
        output_file = os.path.join(DOWNLOAD_DIR, f"{task_id}_{safe_title}_{resolution}.mp4")
        
        (
            ffmpeg
            .input(original_file)
            .filter("scale", size=RESOLUTIONS[resolution], force_original_aspect_ratio="decrease")
            .output(output_file, vcodec="libx264", acodec="aac", video_bitrate="1000k", audio_bitrate="128k")
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True)
        )
        
        # Clean up original file
        os.remove(original_file)
        
        logger.info(f"Conversion completed for task {task_id}.")
        task.status = "Completed"
        task.filename = os.path.basename(output_file)
    except Exception as e:
        logger.error(f"Error in task {task_id}: {str(e)}")
        task.status = "Failed"
        task.error = str(e)

# API endpoints
@app.post("/download")
async def request_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    try:
        task_id = str(uuid.uuid4())
        download_tasks[task_id] = DownloadStatus(task_id=task_id, status="Queued")
        
        logger.info(f"New download request: {request.url}, Resolution: {request.resolution}")
        background_tasks.add_task(download_and_convert_video, task_id, request.url, request.resolution)
        
        return JSONResponse(content={"task_id": task_id, "message": "Download request accepted"})
    except Exception as e:
        logger.error(f"Error in request_download: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@app.get("/status/{task_id}")
async def get_status(task_id: str):
    task = download_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task

@app.get("/download/{task_id}")
async def download_file(task_id: str):
    task = download_tasks.get(task_id)
    if not task or task.status != "Completed":
        raise HTTPException(status_code=404, detail="Download not ready or doesn't exist")
    
    file_path = os.path.join(DOWNLOAD_DIR, task.filename)
    return FileResponse(file_path, media_type="video/mp4", filename=task.filename)

@app.on_event("shutdown")
async def shutdown_event():
    for file in os.listdir(DOWNLOAD_DIR):
        os.remove(os.path.join(DOWNLOAD_DIR, file))
    os.rmdir(DOWNLOAD_DIR)